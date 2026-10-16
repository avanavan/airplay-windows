"""PyInstaller entry point for airplay-windows.exe (no console window)."""

import sys

from airplay_windows.ui.app import main

sys.exit(main())
