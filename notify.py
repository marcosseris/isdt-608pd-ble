"""Alerts: Windows toast, repeating sound, email via your own account, and
phone push via Home Assistant's companion app.

Email settings live in email_config.json; the password is stored in Windows
Credential Manager (via keyring), never in a file. Use an *app password*:
  Gmail:   myaccount.google.com/apppasswords (needs 2-step verification)
  Outlook: account.microsoft.com/security -> App passwords
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.request
import winsound
from email.message import EmailMessage

import certifi
import keyring
from winotify import Notification, audio

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "email_config.json")
KEYRING_SERVICE = "isdt-charger-monitor"

# Verify TLS against certifi's CA bundle: this PC's Windows certificate store
# has a stale Let's Encrypt chain that makes Python reject valid certificates.
TLS = ssl.create_default_context(cafile=certifi.where())

SMTP_PRESETS = {
    "gmail.com": ("smtp.gmail.com", 587),
    "googlemail.com": ("smtp.gmail.com", 587),
    "outlook.com": ("smtp-mail.outlook.com", 587),
    "hotmail.com": ("smtp-mail.outlook.com", 587),
    "live.com": ("smtp-mail.outlook.com", 587),
    "yahoo.com": ("smtp.mail.yahoo.com", 587),
    "icloud.com": ("smtp.mail.me.com", 587),
}


def smtp_for(address: str) -> tuple[str, int]:
    domain = address.rsplit("@", 1)[-1].lower()
    return SMTP_PRESETS.get(domain, ("smtp.office365.com", 587))


def load_email_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"enabled": False, "address": "", "to": "", "host": "", "port": 587}


def save_email_config(cfg: dict, password: str | None = None) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    if password:
        keyring.set_password(KEYRING_SERVICE, cfg["address"], password)


def send_email(subject: str, body: str, cfg: dict | None = None) -> None:
    """Send from the configured account to itself (or cfg['to']). Raises on failure."""
    cfg = cfg or load_email_config()
    sender = cfg["address"]
    password = keyring.get_password(KEYRING_SERVICE, sender)
    if not sender or not password:
        raise RuntimeError("Email not set up - open Email settings first")
    host, port = cfg.get("host") or smtp_for(sender)[0], int(cfg.get("port") or 587)

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = cfg.get("to") or sender
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=20) as s:
        s.starttls(context=TLS)
        s.login(sender, password)
        s.send_message(msg)


HA_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "homeassistant_config.json")
HA_KEYRING = "isdt-charger-monitor-homeassistant"


def load_ha_config() -> dict:
    try:
        with open(HA_CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"enabled": False, "url": "", "service": ""}


def save_ha_config(cfg: dict, token: str | None = None) -> None:
    with open(HA_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    if token:
        keyring.set_password(HA_KEYRING, cfg["url"], token)


def _ha_request(url: str, token: str, path: str, payload: dict | None = None):
    req = urllib.request.Request(
        url.rstrip("/") + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=20, context=TLS) as r:
        return json.loads(r.read() or b"null")


def ha_notify_services(url: str, token: str) -> list[str]:
    """List notify services (phones first), e.g. ['mobile_app_pixel_8', ...]."""
    for domain in _ha_request(url, token, "/api/services"):
        if domain["domain"] == "notify":
            names = sorted(domain["services"])
            return sorted(names, key=lambda n: not n.startswith("mobile_app_"))
    return []


def send_homeassistant(title: str, message: str, cfg: dict | None = None) -> None:
    """Push to your phone via the Home Assistant companion app. Raises on failure."""
    cfg = cfg or load_ha_config()
    url, service = cfg.get("url", ""), cfg.get("service", "")
    token = keyring.get_password(HA_KEYRING, url) if url else None
    if not (url and service and token):
        raise RuntimeError("Home Assistant not set up - open Home Assistant settings first")
    if re.fullmatch(r"[0-9a-f]{32}", service):
        # A device ID, as used by the notify.send_message action in the HA UI.
        _ha_request(url, token, "/api/services/notify/send_message",
                    {"device_id": service, "title": title, "message": message})
        return
    _ha_request(url, token, f"/api/services/notify/{service}", {
        "title": title,
        "message": message,
        "data": {
            "tag": "isdt-charger",                              # replaces the previous alert
            "priority": "high", "ttl": 0, "channel": "Charger",  # Android: deliver immediately
            "push": {"interruption-level": "time-sensitive", "sound": "default"},  # iOS
        },
    })


def describe_error(e: Exception) -> str:
    """Readable message for the GUI (the exception itself is gone once the except block ends)."""
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 401:
            return ("401 Unauthorized - Home Assistant rejected the access token. Create a new "
                    "long-lived token (about 180 characters, starts with 'eyJ') and paste all of it.")
        if e.code == 403:
            return ("403 Forbidden - Home Assistant has probably IP-banned this network after failed "
                    "logins. Remove the entry from /config/ip_bans.yaml and restart Home Assistant.")
        return f"HTTP {e.code} {e.reason}"
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return "Login rejected - use an app password, not your normal password."
    return f"{type(e).__name__}: {e}"


def toast(title: str, msg: str) -> None:
    n = Notification(app_id="ISDT Monitor", title=title, msg=msg, duration="long")
    n.set_audio(audio.LoopingAlarm, loop=True)
    n.show()


class Alarm:
    """Repeating beep in a background thread until stop() or timeout."""

    def __init__(self, timeout_s: float = 120):
        self.timeout_s = timeout_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def active(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.active:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        end = time.time() + self.timeout_s
        while not self._stop.is_set() and time.time() < end:
            for f in (1800, 2400, 1800):
                winsound.Beep(f, 180)
            self._stop.wait(0.8)
