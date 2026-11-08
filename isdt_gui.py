"""ISDT 608PD charge monitor - GUI.

    python isdt_gui.py

Close the ISD Link app (or phone Bluetooth) first: the charger only accepts
one connection.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import isdt
import notify
from estimator import ChargeEstimator
from ha_live import HALive

BG = "#16181d"
CARD = "#20232a"
FG = "#e8e8ea"
MUTED = "#8b8f98"
TRACK = "#2c3038"
STATUS_COLOURS = {0: "#8b8f98", 1: "#3b82f6", 2: "#22c55e", 3: "#f59e0b", 4: "#f59e0b"}


class BleWorker(threading.Thread):
    def __init__(self, out: queue.Queue):
        super().__init__(daemon=True)
        self.out = out

    def run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        async for rt in isdt.stream(on_state=lambda s: self.out.put(("state", s))):
            self.out.put(("data", rt))


class EmailDialog(tk.Toplevel):
    def __init__(self, master: "App"):
        super().__init__(master)
        self.title("Email alerts")
        self.configure(bg=BG, padx=16, pady=16)
        self.resizable(False, False)
        self.transient(master)
        cfg = notify.load_email_config()

        self.enabled = tk.BooleanVar(value=cfg.get("enabled", False))
        self.address = tk.StringVar(value=cfg.get("address", ""))
        self.password = tk.StringVar()
        self.to = tk.StringVar(value=cfg.get("to", ""))
        self.host = tk.StringVar(value=cfg.get("host", ""))
        self.port = tk.StringVar(value=str(cfg.get("port", 587)))
        has_pw = bool(cfg.get("address") and notify.keyring.get_password(notify.KEYRING_SERVICE, cfg["address"]))

        rows = [
            ("Your email", ttk.Entry(self, textvariable=self.address, width=34)),
            ("App password", ttk.Entry(self, textvariable=self.password, width=34, show="•")),
            ("Send to (optional)", ttk.Entry(self, textvariable=self.to, width=34)),
            ("SMTP server", ttk.Entry(self, textvariable=self.host, width=34)),
            ("Port", ttk.Entry(self, textvariable=self.port, width=8)),
        ]
        for r, (label, widget) in enumerate(rows):
            ttk.Label(self, text=label).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 10))
            widget.grid(row=r, column=1, sticky="w", pady=3)
        rows[0][1].bind("<FocusOut>", self._autofill)

        hint = ("Password saved in Windows Credential Manager - leave blank to keep it."
                if has_pw else "Use an app password, not your normal one\n"
                               "(Gmail: myaccount.google.com/apppasswords).")
        ttk.Label(self, text=hint, style="Muted.TLabel", justify="left").grid(
            row=len(rows), column=0, columnspan=2, sticky="w", pady=(6, 6))
        ttk.Checkbutton(self, text="Email me when a charge finishes", variable=self.enabled).grid(
            row=len(rows) + 1, column=0, columnspan=2, sticky="w", pady=(0, 10))

        btns = ttk.Frame(self)
        btns.grid(row=len(rows) + 2, column=0, columnspan=2, sticky="e")
        ttk.Button(btns, text="Send test email", command=self._test).pack(side="left", padx=4)
        ttk.Button(btns, text="Save", command=self._save).pack(side="left", padx=4)
        self.grab_set()

    def _autofill(self, _e=None) -> None:
        if "@" in self.address.get() and not self.host.get():
            host, port = notify.smtp_for(self.address.get())
            self.host.set(host)
            self.port.set(str(port))

    def _cfg(self) -> dict:
        self._autofill()
        return {"enabled": self.enabled.get(), "address": self.address.get().strip(),
                "to": self.to.get().strip(), "host": self.host.get().strip(),
                "port": int(self.port.get() or 587)}

    def _store(self) -> dict | None:
        cfg = self._cfg()
        if "@" not in cfg["address"]:
            messagebox.showerror("Email", "Enter your email address", parent=self)
            return None
        notify.save_email_config(cfg, self.password.get() or None)
        return cfg

    def _save(self) -> None:
        if self._store():
            self.master.email_enabled.set(self.enabled.get())
            self.destroy()

    def _test(self) -> None:
        cfg = self._store()
        if not cfg:
            return
        self.config(cursor="watch")

        def work():
            try:
                notify.send_email("ISDT monitor test", "Email alerts are working.", cfg)
                self.after(0, lambda: messagebox.showinfo("Email", "Test email sent.", parent=self))
            except Exception as e:
                err = notify.describe_error(e)
                self.after(0, lambda: messagebox.showerror("Email", f"Failed: {err}", parent=self))
            finally:
                self.after(0, lambda: self.config(cursor=""))

        threading.Thread(target=work, daemon=True).start()


class HomeAssistantDialog(tk.Toplevel):
    def __init__(self, master: "App"):
        super().__init__(master)
        self.title("Home Assistant alerts")
        self.configure(bg=BG, padx=16, pady=16)
        self.resizable(False, False)
        self.transient(master)
        cfg = notify.load_ha_config()

        self.enabled = tk.BooleanVar(value=cfg.get("enabled", False))
        self.url = tk.StringVar(value=cfg.get("url", "") or "http://homeassistant.local:8123")
        self.token = tk.StringVar()
        self.service = tk.StringVar(value=cfg.get("service", ""))
        self.live = tk.BooleanVar(value=cfg.get("live", True))
        has_token = bool(cfg.get("url") and notify.keyring.get_password(notify.HA_KEYRING, cfg["url"]))

        ttk.Label(self, text=(
            "Sends a push notification through the Home Assistant companion app.\n"
            "Token: Home Assistant > your profile > Security > Long-lived access tokens."),
            style="Muted.TLabel", justify="left").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        ttk.Label(self, text="URL").grid(row=1, column=0, sticky="w", pady=3, padx=(0, 10))
        ttk.Entry(self, textvariable=self.url, width=38).grid(row=1, column=1, columnspan=2, sticky="w", pady=3)
        ttk.Label(self, text="Access token").grid(row=2, column=0, sticky="w", pady=3, padx=(0, 10))
        ttk.Entry(self, textvariable=self.token, width=38, show="•").grid(row=2, column=1, columnspan=2, sticky="w", pady=3)
        ttk.Label(self, text="Phone").grid(row=3, column=0, sticky="w", pady=3, padx=(0, 10))
        self.combo = ttk.Combobox(self, textvariable=self.service, width=28)
        self.combo.grid(row=3, column=1, sticky="w", pady=3)
        ttk.Button(self, text="Find", command=self._find).grid(row=3, column=2, sticky="w", padx=(6, 0))
        ttk.Label(self, text=("Token saved - leave blank to keep it." if has_token
                              else "Press Find to list phones, or paste a device ID from an automation."),
                  style="Muted.TLabel").grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 6))
        ttk.Checkbutton(self, text="Notify my phone when a charge finishes", variable=self.enabled).grid(
            row=5, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(self, text="Show live charger data in Home Assistant", variable=self.live).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(0, 10))

        btns = ttk.Frame(self)
        btns.grid(row=7, column=0, columnspan=3, sticky="e")
        ttk.Button(btns, text="Send test", command=self._test).pack(side="left", padx=4)
        ttk.Button(btns, text="Save", command=self._save).pack(side="left", padx=4)
        self.grab_set()

    def _creds(self) -> tuple[str, str | None]:
        url = self.url.get().strip().rstrip("/")
        return url, self.token.get().strip() or notify.keyring.get_password(notify.HA_KEYRING, url)

    def _run(self, work, on_ok) -> None:
        self.config(cursor="watch")

        def wrapper():
            try:
                result = work()
                self.after(0, lambda: on_ok(result))
            except Exception as e:
                err = notify.describe_error(e)
                self.after(0, lambda: messagebox.showerror("Home Assistant", f"Failed: {err}", parent=self))
            finally:
                self.after(0, lambda: self.config(cursor=""))

        threading.Thread(target=wrapper, daemon=True).start()

    def _find(self) -> None:
        url, token = self._creds()
        if not token:
            messagebox.showerror("Home Assistant", "Enter an access token first", parent=self)
            return

        def done(services):
            self.combo["values"] = services
            if services and self.service.get() not in services:
                self.service.set(services[0])
            if not services:
                messagebox.showwarning("Home Assistant", "No notify services found - is the companion "
                                       "app installed and logged in on your phone?", parent=self)

        self._run(lambda: notify.ha_notify_services(url, token), done)

    def _store(self) -> dict | None:
        url, _ = self._creds()
        cfg = {"enabled": self.enabled.get(), "live": self.live.get(), "url": url,
               "service": self.service.get().strip().removeprefix("notify.")}
        if not url.startswith("http") or not cfg["service"]:
            messagebox.showerror("Home Assistant", "Enter the URL and pick a phone", parent=self)
            return None
        token = self.token.get().strip()
        if token and len(token) < 100:
            messagebox.showerror("Home Assistant", f"That token is only {len(token)} characters. "
                                 "Long-lived access tokens are ~180 characters and start with 'eyJ' - "
                                 "make sure you copied the whole thing.", parent=self)
            return None
        notify.save_ha_config(cfg, self.token.get().strip() or None)
        return cfg

    def _save(self) -> None:
        if self._store():
            self.master.ha_enabled.set(self.enabled.get())
            self.destroy()

    def _test(self) -> None:
        cfg = self._store()
        if cfg:
            self._run(lambda: notify.send_homeassistant("ISDT monitor test", "Phone alerts are working.", cfg),
                      lambda _: messagebox.showinfo("Home Assistant", "Test notification sent.", parent=self))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ISDT 608PD Monitor")
        self.configure(bg=BG, padx=18, pady=16)
        self.minsize(460, 0)
        self._style()

        self.events: queue.Queue = queue.Queue()
        self.alarm = notify.Alarm()
        self.estimator = ChargeEstimator()
        self.last_status: int | None = None
        self.last_rt: isdt.RealTime | None = None
        self.email_enabled = tk.BooleanVar(value=notify.load_email_config().get("enabled", False))
        self.email_enabled.trace_add("write", self._toggle_email)
        self.ha_enabled = tk.BooleanVar(value=notify.load_ha_config().get("enabled", False))
        self.ha_enabled.trace_add("write", self._toggle_ha)

        self._build()
        self.ha_live = HALive()
        self.conn_text = ""
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        BleWorker(self.events).start()
        self.after(100, self._pump)

    # ---- layout -------------------------------------------------------
    def _style(self) -> None:
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, font=("Segoe UI", 10))
        s.configure("TFrame", background=BG)
        s.configure("Card.TFrame", background=CARD)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        s.configure("Card.TLabel", background=CARD, foreground=FG)
        s.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        s.configure("Value.TLabel", background=CARD, foreground=FG, font=("Segoe UI Semibold", 14))
        s.configure("TCheckbutton", background=BG, foreground=FG)
        s.map("TCheckbutton", background=[("active", BG)])
        s.configure("TButton", background=CARD, foreground=FG, borderwidth=0, padding=(10, 5))
        s.map("TButton", background=[("active", TRACK)])
        s.configure("TEntry", fieldbackground=CARD, foreground=FG, insertcolor=FG)

    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x")
        self.status_lbl = tk.Label(top, text="Starting...", bg=BG, fg=MUTED, font=("Segoe UI Semibold", 20))
        self.status_lbl.pack(side="left")
        self.chem_lbl = ttk.Label(top, text="", style="Muted.TLabel")
        self.chem_lbl.pack(side="right", pady=(10, 0))

        self.bar = tk.Canvas(self, height=46, bg=BG, highlightthickness=0)
        self.bar.pack(fill="x", pady=(10, 4))
        self.bar.bind("<Configure>", lambda e: self._draw_bar())

        mid = ttk.Frame(self)
        mid.pack(fill="x")
        self.remaining_lbl = ttk.Label(mid, text="Time left: --:--:--", font=("Segoe UI Semibold", 12))
        self.remaining_lbl.pack(side="left")
        self.eta_lbl = ttk.Label(mid, text="", style="Muted.TLabel")
        self.eta_lbl.pack(side="right")

        grid = ttk.Frame(self)
        grid.pack(fill="x", pady=(12, 0))
        self.values: dict[str, ttk.Label] = {}
        for i, key in enumerate(["Pack", "Current", "Power", "Elapsed", "Added", "Input"]):
            card = ttk.Frame(grid, style="Card.TFrame", padding=(10, 6))
            card.grid(row=i // 3, column=i % 3, sticky="nsew", padx=3, pady=3)
            ttk.Label(card, text=key, style="CardMuted.TLabel").pack(anchor="w")
            self.values[key] = ttk.Label(card, text="-", style="Value.TLabel")
            self.values[key].pack(anchor="w")
        for c in range(3):
            grid.columnconfigure(c, weight=1)

        self.cells_frame = ttk.Frame(self)
        self.cells_frame.pack(fill="x", pady=(8, 0))
        self.cell_lbls: list[ttk.Label] = []

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", pady=(14, 0))
        ttk.Checkbutton(bottom, text="Email when done", variable=self.email_enabled).pack(side="left")
        ttk.Button(bottom, text="Email settings...", command=lambda: EmailDialog(self)).pack(side="left", padx=6)
        self.silence_btn = ttk.Button(bottom, text="Test alert", command=self._silence_or_test)
        self.silence_btn.pack(side="right")

        bottom2 = ttk.Frame(self)
        bottom2.pack(fill="x", pady=(6, 0))
        ttk.Checkbutton(bottom2, text="Phone when done", variable=self.ha_enabled).pack(side="left")
        ttk.Button(bottom2, text="Home Assistant...", command=lambda: HomeAssistantDialog(self)).pack(side="left", padx=6)

        self.conn_lbl = ttk.Label(self, text="", style="Muted.TLabel")
        self.conn_lbl.pack(fill="x", pady=(10, 0))

    def _draw_bar(self) -> None:
        c = self.bar
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        rt = self.last_rt
        pct = rt.percent if rt else 0
        colour = STATUS_COLOURS.get(rt.status, MUTED) if rt else MUTED
        r = 10
        self._round_rect(0, 0, w, h, r, TRACK)
        if pct > 0:
            self._round_rect(0, 0, max(2 * r, w * min(pct, 100) / 100), h, r, colour)
        c.create_text(w / 2, h / 2, text=f"{pct}%" if rt else "--", fill="white",
                      font=("Segoe UI Semibold", 16))

    def _round_rect(self, x0, y0, x1, y1, r, fill) -> None:
        c = self.bar
        c.create_rectangle(x0 + r, y0, x1 - r, y1, fill=fill, width=0)
        c.create_rectangle(x0, y0 + r, x1, y1 - r, fill=fill, width=0)
        for x, y in ((x0, y0), (x1 - 2 * r, y0), (x0, y1 - 2 * r), (x1 - 2 * r, y1 - 2 * r)):
            c.create_oval(x, y, x + 2 * r, y + 2 * r, fill=fill, width=0)

    # ---- data ---------------------------------------------------------
    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "state":
                    self.conn_text = payload
                    if not payload.startswith("Connected"):
                        self.ha_live.update(None, connected=False, urgent=True)
                else:
                    self._update(payload)
        except queue.Empty:
            pass
        self.silence_btn.config(text="Silence alarm" if self.alarm.active else "Test alert")
        err = self.ha_live.last_error
        self.conn_lbl.config(text=self.conn_text + (f"  ·  HA: {err}" if err else ""))
        self.after(200, self._pump)

    def _update(self, rt: isdt.RealTime) -> None:
        self.last_rt = rt
        self.estimator.add(rt)
        colour = STATUS_COLOURS.get(rt.status, MUTED)
        self.status_lbl.config(text=rt.status_name, fg=colour)
        self.chem_lbl.config(text=f"{rt.chemistry_name} {len(rt.cells)}S" if rt.status else "")
        self._draw_bar()

        if rt.status == 1:
            rem = self.estimator.remaining_s()
            phase = {"CC": "Constant current", "CV": "Topping off"}.get(self.estimator.phase, "")
            self.remaining_lbl.config(text=f"Time left: {isdt.fmt_hms(rem) if rem is not None else 'estimating...'}")
            eta = f" · done ~{time.strftime('%H:%M', time.localtime(self.estimator.eta()))}" if rem is not None else ""
            self.eta_lbl.config(text=phase + eta)
        elif rt.status == 2:
            self.remaining_lbl.config(text="Charged - swap the battery")
            self.eta_lbl.config(text="")
        else:
            self.remaining_lbl.config(text="Time left: --:--:--")
            self.eta_lbl.config(text="")

        active = rt.status != 0
        self.values["Pack"].config(text=f"{rt.pack_mv / 1000:.2f} V" if active else "-")
        self.values["Current"].config(text=f"{rt.current_ma / 1000:.2f} A" if active else "-")
        self.values["Power"].config(text=f"{rt.output_w} W" if active else "-")
        self.values["Elapsed"].config(text=isdt.fmt_hms(rt.elapsed_s) if active else "-")
        self.values["Added"].config(text=f"{rt.capacity_mah} mAh" if active else "-")
        self.values["Input"].config(text=f"{rt.output_mv / 1000:.1f} V")
        self._update_cells(rt)
        self.ha_live.update(rt, self.estimator.phase, self.estimator.remaining_s(), self.estimator.eta(),
                            urgent=rt.status != self.last_status)

        if self.last_status == 1 and rt.status == 2:
            self._charge_done(rt)
        self.last_status = rt.status

    def _update_cells(self, rt: isdt.RealTime) -> None:
        if len(self.cell_lbls) != len(rt.cells):
            for l in self.cell_lbls:
                l.destroy()
            self.cell_lbls = []
            for i in range(len(rt.cells)):
                l = ttk.Label(self.cells_frame, style="Card.TLabel", padding=(8, 4), anchor="center")
                l.grid(row=0, column=i, sticky="ew", padx=3)
                self.cells_frame.columnconfigure(i, weight=1)
                self.cell_lbls.append(l)
        for i, (l, cell) in enumerate(zip(self.cell_lbls, rt.cells)):
            ir = f"\n{cell.ir / 10:.1f} mΩ" if cell.ir else ""
            l.config(text=f"C{i + 1}  {cell.mv / 1000:.3f} V{ir}")

    # ---- alerts -------------------------------------------------------
    def _charge_done(self, rt: isdt.RealTime) -> None:
        msg = (f"{rt.chemistry_name} {len(rt.cells)}S pack charged to {rt.pack_mv / 1000:.2f} V "
               f"({rt.capacity_mah} mAh in {isdt.fmt_hms(rt.elapsed_s)}). Swap the battery!")
        notify.toast("Battery charged", msg)
        self.alarm.start()
        self.lift()
        self.attributes("-topmost", True)
        self.after(500, lambda: self.attributes("-topmost", False))
        senders = []
        if self.email_enabled.get():
            senders.append(("Email", lambda: notify.send_email("Battery charged - ISDT 608PD", msg)))
        if self.ha_enabled.get():
            senders.append(("Phone notification", lambda: notify.send_homeassistant("🔋 Battery charged", msg)))
        for name, send in senders:
            def work(name=name, send=send):
                try:
                    send()
                    self.events.put(("state", f"{name} sent"))
                except Exception as e:
                    self.events.put(("state", f"{name} failed: {e}"))
            threading.Thread(target=work, daemon=True).start()

    def _silence_or_test(self) -> None:
        if self.alarm.active:
            self.alarm.stop()
        else:
            notify.toast("Battery charged", "This is a test alert")
            self.alarm.start()

    def _toggle_email(self, *_):
        cfg = notify.load_email_config()
        if self.email_enabled.get() and not cfg.get("address"):
            self.email_enabled.set(False)
            EmailDialog(self)
            return
        cfg["enabled"] = self.email_enabled.get()
        notify.save_email_config(cfg)

    def _toggle_ha(self, *_):
        cfg = notify.load_ha_config()
        if self.ha_enabled.get() and not cfg.get("service"):
            self.ha_enabled.set(False)
            HomeAssistantDialog(self)
            return
        cfg["enabled"] = self.ha_enabled.get()
        notify.save_ha_config(cfg)


    def _on_close(self) -> None:
        self.alarm.stop()
        self.ha_live.flush_offline()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
