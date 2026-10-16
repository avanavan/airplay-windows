"""PyInstaller entry point for airplay-windows-cli.exe (console)."""

import sys

from airplay_windows.cli import main

sys.exit(main())
