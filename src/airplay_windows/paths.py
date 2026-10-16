"""Where settings and logs live."""

from __future__ import annotations

import os

from airplay_windows import APP_NAME


def _base(variable: str) -> str:
    return os.environ.get(variable) or os.path.expanduser("~")


def settings_file() -> str:
    """%APPDATA%\\airplay-windows\\settings.json (roams with the user profile)."""
    return os.path.join(_base("APPDATA"), APP_NAME, "settings.json")


def log_file() -> str:
    """%LOCALAPPDATA%\\airplay-windows\\airplay-windows.log"""
    return os.path.join(_base("LOCALAPPDATA"), APP_NAME, f"{APP_NAME}.log")
