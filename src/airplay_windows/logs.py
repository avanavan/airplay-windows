"""Logging: a rotating file for the app, plain lines on the console for the CLI."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys

from airplay_windows import paths


def setup(console: bool = False, to_file: bool = True, level: int = logging.INFO) -> None:
    root = logging.getLogger("airplay_windows")
    root.setLevel(level)
    if to_file:
        try:
            os.makedirs(os.path.dirname(paths.log_file()), exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(paths.log_file(), maxBytes=1_000_000, backupCount=2,
                                                           encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            root.addHandler(handler)
        except OSError:
            pass
    if console and sys.stdout:
        # A real console gets Unicode either way; redirected output would otherwise use the
        # legacy code page and turn non-Latin speaker names into "?".
        sys.stdout.reconfigure(encoding=None if sys.stdout.isatty() else "utf-8", errors="replace")
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
