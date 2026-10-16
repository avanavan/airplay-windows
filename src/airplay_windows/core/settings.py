"""Saved state: known speakers, per-speaker volume, passwords (DPAPI), capture and output choices."""

from __future__ import annotations

import base64
import json
import os
import sys

from airplay_windows import paths
from airplay_windows.protocol.receiver import Receiver

DEFAULTS = {"source": None, "switch_output": None, "latency_ms": 2000, "devices": {}}


class Settings:
    def __init__(self, path: str | None = None):
        self.path = path or paths.settings_file()
        self.data: dict = json.loads(json.dumps(DEFAULTS))
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def _entry(self, key: str) -> dict:
        return self.data["devices"].setdefault(key, {})

    # -- speakers --

    def remember(self, receiver: Receiver) -> None:
        self._entry(receiver.key).update(service_name=receiver.service_name, host=receiver.host,
                                         port=receiver.port, txt=receiver.txt, manual=receiver.is_manual)

    def forget(self, key: str) -> None:
        self.data["devices"].pop(key, None)

    def known_receivers(self) -> list[Receiver]:
        return [Receiver(e["service_name"], e["host"], e["port"], dict(e.get("txt") or {}),
                         is_manual=bool(e.get("manual")))
                for e in self.data["devices"].values() if "service_name" in e]

    def volume(self, key: str) -> float:
        return float(self.data["devices"].get(key, {}).get("volume", 35))

    def set_volume(self, key: str, value: float) -> None:
        self._entry(key)["volume"] = round(value)

    def auto_connect(self, key: str) -> bool:
        return bool(self.data["devices"].get(key, {}).get("auto_connect"))

    def set_auto_connect(self, key: str, value: bool) -> None:
        self._entry(key)["auto_connect"] = value

    # -- passwords, encrypted for the current Windows user --

    def password(self, key: str) -> str | None:
        blob = self.data["devices"].get(key, {}).get("password")
        if not blob:
            return None
        try:
            from airplay_windows.windows import credentials

            return credentials.unprotect(base64.b64decode(blob)).decode("utf-8")
        except (OSError, ValueError, ImportError):
            return None

    def set_password(self, key: str, password: str | None) -> None:
        entry = self._entry(key)
        if password and sys.platform == "win32":
            from airplay_windows.windows import credentials

            entry["password"] = base64.b64encode(credentials.protect(password.encode("utf-8"))).decode("ascii")
        else:
            entry.pop("password", None)
