"""Mirror live charger data into Home Assistant as sensor.isdt_charger_* entities.

Uses the REST API (POST /api/states/<entity_id>), so no integration or YAML is
needed. Entities appear as soon as the monitor runs, are recorded in history,
and show 'unavailable' when the monitor isn't connected to the charger.
Only changed values are sent, at most every PUBLISH_INTERVAL seconds (plus
immediately on a status change), over one kept-alive HTTPS connection.
"""

from __future__ import annotations

import datetime as dt
import http.client
import json
import threading
import time
import urllib.parse

import notify
from isdt import RealTime

PUBLISH_INTERVAL = 5.0
MAX_CELLS = 6
STATUS_ICONS = {0: "mdi:power-sleep", 1: "mdi:battery-charging", 2: "mdi:battery-check",
                3: "mdi:battery-arrow-down", 4: "mdi:battery-check-outline"}


def _sensor(state, name, unit=None, device_class=None, icon=None, state_class="measurement", **extra):
    attrs = {"friendly_name": name, **extra}
    if unit:
        attrs["unit_of_measurement"] = unit
    if device_class:
        attrs["device_class"] = device_class
    if icon:
        attrs["icon"] = icon
    if state_class and unit:
        attrs["state_class"] = state_class
    return {"state": state, "attributes": attrs}


def build_states(rt: RealTime | None, phase: str | None = None, remaining_s: float | None = None,
                 eta: float | None = None, connected: bool = True) -> dict[str, dict]:
    """Entity id -> {state, attributes} for one charger reading."""
    na = "unavailable"
    on = rt is not None and connected
    charging = on and rt.status == 1
    active = on and rt.status != 0
    phase_name = {"CC": "Constant current", "CV": "Topping off"}.get(phase or "", "")

    s = {
        "binary_sensor.isdt_charger_connected": {"state": "on" if on else "off", "attributes": {
            "friendly_name": "Charger connected", "device_class": "connectivity"}},
        "sensor.isdt_charger_status": _sensor(
            rt.status_name if on else na, "Charger status", state_class=None,
            icon=STATUS_ICONS.get(rt.status, "mdi:battery") if on else "mdi:battery-off",
            status_code=rt.status if on else None,
            pack=f"{rt.chemistry_name} {len(rt.cells)}S" if active else "",
            phase=phase_name if charging else ""),
        "sensor.isdt_charger_battery": _sensor(rt.percent if active else na, "Charger battery",
                                               "%", "battery"),
        "sensor.isdt_charger_time_left": _sensor(
            round(remaining_s / 60, 1) if charging and remaining_s is not None else (0 if active else na),
            "Charger time left", "min", "duration", "mdi:timer-sand"),
        "sensor.isdt_charger_done_at": _sensor(
            dt.datetime.fromtimestamp(eta).astimezone().isoformat(timespec="seconds")
            if charging and eta else na, "Charger done at", device_class="timestamp",
            icon="mdi:clock-check-outline", state_class=None),
        "sensor.isdt_charger_pack_voltage": _sensor(round(rt.pack_mv / 1000, 2) if active else na,
                                                    "Charger pack voltage", "V", "voltage"),
        "sensor.isdt_charger_current": _sensor(round(rt.current_ma / 1000, 2) if active else na,
                                               "Charger current", "A", "current"),
        "sensor.isdt_charger_power": _sensor(rt.output_w if active else na, "Charger power", "W", "power"),
        "sensor.isdt_charger_added": _sensor(rt.capacity_mah if active else na, "Charger added",
                                             "mAh", icon="mdi:battery-plus-outline"),
        "sensor.isdt_charger_elapsed": _sensor(round(rt.elapsed_s / 60, 1) if active else na,
                                               "Charger elapsed", "min", "duration", "mdi:timer-outline"),
        "sensor.isdt_charger_input_voltage": _sensor(round(rt.output_mv / 1000, 1) if on else na,
                                                     "Charger input voltage", "V", "voltage"),
    }
    for i in range(MAX_CELLS):
        cell = rt.cells[i] if active and i < len(rt.cells) else None
        s[f"sensor.isdt_charger_cell_{i + 1}"] = _sensor(
            round(cell.mv / 1000, 3) if cell else na, f"Charger cell {i + 1}", "V", "voltage",
            ir_mohm=round(cell.ir / 10, 1) if cell and cell.ir else None)
    return s


class HALive:
    """Background publisher; call update() as often as you like."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: dict[str, dict] | None = None
        self._urgent = threading.Event()
        self._sent: dict[str, str] = {}
        self._conn: http.client.HTTPConnection | None = None
        self._send_lock = threading.Lock()
        self.stopped = False  # set after an auth failure so we never hammer HA into an IP ban
        self.last_error: str | None = None
        threading.Thread(target=self._run, daemon=True).start()

    @staticmethod
    def enabled() -> bool:
        cfg = notify.load_ha_config()
        return bool(cfg.get("url") and cfg.get("live", True))

    def update(self, rt: RealTime | None, phase=None, remaining_s=None, eta=None,
               connected: bool = True, urgent: bool = False) -> None:
        with self._lock:
            self._pending = build_states(rt, phase, remaining_s, eta, connected)
        if urgent:
            self._urgent.set()

    def _run(self) -> None:
        while True:
            self._urgent.wait(PUBLISH_INTERVAL)
            self._urgent.clear()
            with self._lock:
                states, self._pending = self._pending, None
            if states and not self.stopped and self.enabled():
                try:
                    self._send(states)
                    self.last_error = None
                except PermissionError as e:
                    self.stopped = True
                    self.last_error = f"{e} - live updates paused until restart"
                except Exception as e:
                    self.last_error = notify.describe_error(e)
                    self._conn = None

    def _send(self, states: dict[str, dict]) -> None:
        with self._send_lock:
            self._send_locked(states)

    def _send_locked(self, states: dict[str, dict]) -> None:
        cfg = notify.load_ha_config()
        token = notify.keyring.get_password(notify.HA_KEYRING, cfg["url"])
        if not token:
            return
        u = urllib.parse.urlparse(cfg["url"])
        if self._conn is None:
            cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
            kw = {"context": notify.TLS} if u.scheme == "https" else {}
            self._conn = cls(u.hostname, u.port, timeout=15, **kw)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        for entity_id, body in states.items():
            payload = json.dumps(body, sort_keys=True)
            if self._sent.get(entity_id) == payload:
                continue
            self._conn.request("POST", f"/api/states/{entity_id}", payload, headers)
            r = self._conn.getresponse()
            r.read()
            if r.status in (401, 403):
                self._conn = None
                raise PermissionError(f"Home Assistant refused live update (HTTP {r.status})")
            if r.status >= 300:
                raise RuntimeError(f"HTTP {r.status} for {entity_id}")
            self._sent[entity_id] = payload

    def flush_offline(self, timeout: float = 5.0) -> None:
        """Mark everything unavailable (call on exit)."""
        if self.stopped or not self.enabled():
            return
        t = threading.Thread(target=lambda: self._send(build_states(None, connected=False)), daemon=True)
        t.start()
        t.join(timeout)
