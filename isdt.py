"""ISDT 608PD BLE protocol, as implemented by the ISD Link app (v1.4.7).

Handshake (from com.isdt.isdlink.ble.BleMessage / BindReq):
    write AF02: 0x18 <16-byte app id> 0x00 <status>   -> notify AF02: 0x19 <bound>
    bound == 0 means accepted; until then the charger drops us after ~6 s.

Real-time data (c608pd.protocol.RealTimeDataReq / RealTimeDataModel):
    write AF01: 0x12 0xE4                              -> notify AF01: 0x31 0xE5 ...
    all multi-byte fields little-endian.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

from bleak import BleakClient, BleakScanner

AF01 = "0000af01-0000-1000-8000-00805f9b34fb"
AF02 = "0000af02-0000-1000-8000-00805f9b34fb"

CMD_BIND = 0x18
RESP_BIND = 0x19
CMD_REALTIME = bytes([0x12, 0xE4])
RESP_REALTIME = 0xE5

STATUS = {0: "Standby", 1: "Charging", 2: "Charge done", 3: "Discharging", 4: "Discharge done"}
CHEMISTRY = ["LiHV", "LiPo", "LiIon", "LiFe", "PbAc", "NiMH", "ULiHV"]

APP_ID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_id.txt")


@dataclass
class Cell:
    mv: int
    ir: int  # 0.1 mOhm units


@dataclass
class RealTime:
    output_mv: int
    output_w: int
    status: int
    chemistry: int
    current_ma: int
    pack_mv: int
    percent: int
    balance_port: int
    elapsed_s: int
    capacity_mah: int
    cells: list[Cell] = field(default_factory=list)
    received: float = field(default_factory=time.time)

    @property
    def status_name(self) -> str:
        return STATUS.get(self.status, f"Status {self.status}")

    @property
    def chemistry_name(self) -> str:
        return CHEMISTRY[self.chemistry] if 0 <= self.chemistry < len(CHEMISTRY) else "?"

    @classmethod
    def parse(cls, b: bytes) -> "RealTime | None":
        if len(b) < 22 or b[1] != RESP_REALTIME:
            return None
        u16 = lambda i: b[i] | b[i + 1] << 8
        u32 = lambda i: int.from_bytes(b[i:i + 4], "little")
        rt = cls(
            output_mv=u16(2), output_w=u16(4), status=b[6], chemistry=b[7],
            current_ma=u16(8), pack_mv=u16(10), percent=b[12], balance_port=b[13],
            elapsed_s=u32(14), capacity_mah=u32(18),
        )
        i, n = 22, 0
        if rt.balance_port and len(b) > 22:
            n, i = b[22], 23
        for _ in range(n):
            if i + 4 > len(b):
                break
            rt.cells.append(Cell(mv=u16(i), ir=u16(i + 2)))
            i += 4
        return rt


def app_id() -> bytes:
    """Stable random 16-byte id, like the app's per-device UUID."""
    try:
        return bytes.fromhex(open(APP_ID_FILE).read().strip())
    except (OSError, ValueError):
        uid = uuid.uuid4().hex
        with open(APP_ID_FILE, "w") as f:
            f.write(uid)
        return bytes.fromhex(uid)


async def find_charger(timeout: float = 8.0) -> str | None:
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for dev, adv in found.values():
        name = (dev.name or adv.local_name or "")
        if "608" in name or "ISDT" in name.upper() or "0000af00-0000-1000-8000-00805f9b34fb" in adv.service_uuids:
            return dev.address
    return None


async def _bind(client: BleakClient, inbox: asyncio.Queue) -> None:
    uid = app_id()
    for status in (0, 1):
        for _ in range(5):
            await client.write_gatt_char(AF02, bytes([CMD_BIND]) + uid + bytes([0, status]), response=False)
            try:
                while True:
                    d = await asyncio.wait_for(inbox.get(), 1.0)
                    if d and d[0] == RESP_BIND:
                        if len(d) > 1 and d[1] == 0:
                            return
                        break
            except asyncio.TimeoutError:
                pass
    raise ConnectionError("charger refused to bind")


async def stream(address: str | None = None, interval: float = 1.0,
                 on_state: Callable[[str], None] = lambda s: None) -> AsyncIterator[RealTime]:
    """Yield RealTime packets forever, reconnecting as needed."""
    backoff = 2
    while True:
        try:
            if not address:
                on_state("Scanning for charger...")
                address = await find_charger()
                if not address:
                    on_state("Charger not found - is the phone app closed?")
                    await asyncio.sleep(5)
                    continue
            on_state(f"Connecting to {address}...")
            async with BleakClient(address, timeout=20.0) as client:
                af02: asyncio.Queue = asyncio.Queue()
                af01: asyncio.Queue = asyncio.Queue()
                await client.start_notify(AF02, lambda _c, d: af02.put_nowait(bytes(d)))
                await client.start_notify(AF01, lambda _c, d: af01.put_nowait(bytes(d)))
                await _bind(client, af02)
                on_state(f"Connected ({address})")
                backoff = 2
                misses = 0
                while client.is_connected:
                    await client.write_gatt_char(AF01, CMD_REALTIME, response=False)
                    deadline = time.time() + max(interval, 1.5)
                    got = False
                    while time.time() < deadline:
                        try:
                            d = await asyncio.wait_for(af01.get(), deadline - time.time())
                        except asyncio.TimeoutError:
                            break
                        rt = RealTime.parse(d)
                        if rt:
                            got = True
                            yield rt
                            break
                    misses = 0 if got else misses + 1
                    if misses >= 5:
                        raise ConnectionError("no data from charger")
                    await asyncio.sleep(max(0.0, deadline - time.time()) if got else 0)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            on_state(f"Disconnected ({type(e).__name__}: {e}) - retrying in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


def fmt_hms(s: float | int | None) -> str:
    if s is None:
        return "--:--:--"
    s = int(s)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"
