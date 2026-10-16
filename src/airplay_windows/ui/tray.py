"""Notification-area icon: left-click for a panel with every speaker, right-click for a menu.

pystray runs the icon on its own thread; every action is handed to the Tk
thread through the app's event queue.
"""

from __future__ import annotations

import ctypes
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import ttk
from typing import TYPE_CHECKING

import pystray

from airplay_windows import DISPLAY_NAME
from airplay_windows.core.connection import CONNECTING, FAILED, IDLE, NEEDS_PASSWORD, PLAYING
from airplay_windows.ui.icon import icon_image
from airplay_windows.ui.widgets import STATUS

if TYPE_CHECKING:
    from airplay_windows.ui.app import App

SHORT_STATUS = {IDLE: "not connected", CONNECTING: "connecting…", PLAYING: "playing",
                FAILED: "failed", NEEDS_PASSWORD: "password needed"}
VOLUME_STEPS = (100, 80, 60, 50, 40, 30, 20, 10, 0)
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205


def cursor_position() -> tuple[int, int]:
    point = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def work_area(x: int, y: int) -> tuple[int, int, int, int]:
    """The usable area (screen minus taskbar) of the monitor containing (x, y)."""
    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    user32 = ctypes.windll.user32
    user32.MonitorFromPoint.restype = wintypes.HMONITOR
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    info = MonitorInfo()
    info.cbSize = ctypes.sizeof(info)
    user32.GetMonitorInfoW(user32.MonitorFromPoint(wintypes.POINT(x, y), 2), ctypes.byref(info))
    r = info.rcWork
    return r.left, r.top, r.right, r.bottom


class TrayIcon(pystray.Icon):
    """pystray's Windows icon, with left-click opening our panel and the menu rebuilt on each right-click."""

    def __init__(self, *args, on_left_click, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_left_click = on_left_click

    def _on_notify(self, wparam, lparam):
        if lparam == WM_LBUTTONUP:
            self.on_left_click(*cursor_position())
            return
        if lparam == WM_RBUTTONUP:
            self._update_menu()   # speakers and states change all the time
        super()._on_notify(wparam, lparam)


class Tray:
    def __init__(self, app: "App"):
        self.app = app
        self.snapshot: list[tuple[str, str, str, int]] = []   # (key, name, state, volume), set by the Tk thread
        self._playing = False
        self._shape: list | None = None
        self.icon = TrayIcon("airplay-windows", icon_image(False), DISPLAY_NAME, menu=pystray.Menu(self._menu_items),
                             on_left_click=self._left_click)
        self.flyout = Flyout(app)
        self.icon.run_detached()

    def _call(self, fn):
        """A zero-argument menu action that runs `fn` on the Tk thread.

        (pystray passes the icon to callbacks that take arguments, so no default-argument lambdas here.)
        """
        return lambda: self.app.events.put(("call", fn))

    def _left_click(self, x: int, y: int) -> None:
        self.app.events.put(("call", lambda: self.flyout.toggle(x, y)))

    def _menu_items(self):
        Item, Menu = pystray.MenuItem, pystray.Menu
        items = [Item(f"Open {DISPLAY_NAME}", self._call(self.app.show_window), default=True), Menu.SEPARATOR]
        for key, name, state, volume in self.snapshot:
            items.append(Item(f"{name}  —  {SHORT_STATUS[state]}", Menu(*self._speaker_items(key, state, volume))))
        if not self.snapshot:
            items.append(Item("No speakers yet", None, enabled=False))
        items += [Menu.SEPARATOR, Item("Exit", self._call(self.app.close))]
        return items

    def _speaker_items(self, key: str, state: str, volume: int):
        Item, Menu = pystray.MenuItem, pystray.Menu
        busy = state in (CONNECTING, PLAYING)
        items = [Item("Disconnect" if busy else "Connect",
                      self._call(lambda: self.app.disconnect(key) if busy else self.app.connect(key))),
                 Menu.SEPARATOR,
                 Item(f"Volume: {volume}%", None, enabled=False),
                 Item("Volume up", self._call(lambda: self.app.set_row_volume(key, volume + 10))),
                 Item("Volume down", self._call(lambda: self.app.set_row_volume(key, volume - 10)))]
        for step in VOLUME_STEPS:
            items.append(Item(f"{step}%", self._call(self._volume_setter(key, step)),
                              checked=self._is_volume(volume, step), radio=True))
        return items

    def _volume_setter(self, key: str, step: int):
        return lambda: self.app.set_row_volume(key, step)

    @staticmethod
    def _is_volume(volume: int, step: int):
        return lambda _item: volume == step

    def update(self, snapshot, tooltip: str, playing: bool) -> None:
        """From the Tk thread, whenever speakers, states or volumes change."""
        self.snapshot = snapshot
        self.icon.title = tooltip[:127]
        if playing != self._playing:
            self._playing = playing
            self.icon.icon = icon_image(playing)
        shape = [(key, name, state) for key, name, state, _ in snapshot]
        # Volume-only changes don't rebuild the panel: that would destroy a slider mid-drag.
        if shape != self._shape:
            self._shape = shape
            if self.flyout.winfo_viewable():
                self.flyout.rebuild()

    def notify(self, message: str) -> None:
        try:
            self.icon.notify(message, DISPLAY_NAME)
        except Exception:
            pass

    def stop(self) -> None:
        self.flyout.hide()
        self.icon.stop()


class Flyout(tk.Toplevel):
    """Borderless panel above the taskbar: one row per speaker with a button and a volume slider."""

    WIDTH = 360

    def __init__(self, app: "App"):
        super().__init__(app.root)
        self.app = app
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(background="#c8c8c8")   # 1 px border
        self.body = ttk.Frame(self, padding=(14, 10, 14, 12))
        self.body.pack(fill="both", expand=True, padx=1, pady=1)
        self._anchor = (0, 0)
        self._hidden_at = 0.0
        self.bind("<FocusOut>", lambda e: self.after(80, self._maybe_hide))
        self.bind("<Escape>", lambda e: self.hide())

    def toggle(self, x: int, y: int) -> None:
        if self.winfo_viewable():
            self.hide()
            return
        if time.monotonic() - self._hidden_at < 0.4:
            return   # this click on the icon is what just closed the panel
        self._anchor = (x, y)
        self.rebuild()
        self.deiconify()
        self.lift()
        self.focus_force()

    def hide(self) -> None:
        if self.winfo_viewable():
            self.withdraw()
            self._hidden_at = time.monotonic()

    def _maybe_hide(self) -> None:
        try:
            focus = self.focus_get()
        except (KeyError, tk.TclError):
            focus = None
        if focus is None or focus.winfo_toplevel() is not self:
            self.hide()

    def rebuild(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        header = ttk.Frame(self.body)
        header.pack(fill="x", pady=(0, 6))
        ttk.Label(header, text="AirPlay speakers", style="Header.TLabel").pack(side="left")
        ttk.Button(header, text="Open app", command=lambda: (self.hide(), self.app.show_window())).pack(side="right")
        rows = sorted(self.app.rows.values(), key=lambda r: r.receiver.name.lower())
        if not rows:
            ttk.Label(self.body, text="No speakers yet. Open the app to search.", style="Hint.TLabel").pack(
                anchor="w", pady=6)
        for row in rows:
            ttk.Separator(self.body).pack(fill="x", pady=(4, 6))
            self._speaker(row)
        self._place()

    def _speaker(self, row) -> None:
        frame = ttk.Frame(self.body)
        frame.pack(fill="x")
        ttk.Label(frame, text=row.receiver.name, style="Name.TLabel").grid(row=0, column=0, sticky="w")
        text, color = STATUS[row.state]
        ttk.Label(frame, text="● " + text, foreground=color, style="Hint.TLabel").grid(row=1, column=0, sticky="w")
        busy = row.state in (CONNECTING, PLAYING)
        ttk.Button(frame, text="Disconnect" if busy else "Connect", width=11,
                   command=lambda: self.app.disconnect(row.key) if busy else self.app.connect(row.key)).grid(
            row=0, column=1, rowspan=2, sticky="e")
        value = ttk.Label(frame, width=4, anchor="e", text=str(round(row.volume.get())))

        def slide(_v=None):
            value.configure(text=str(round(row.volume.get())))
            row.on_slide()   # same debounce and settings path as the main window's slider

        # Shares the main window's variable, so both sliders always agree.
        ttk.Scale(frame, from_=0, to=100, variable=row.volume, command=slide).grid(
            row=2, column=0, sticky="we", pady=(4, 0))
        value.grid(row=2, column=1, sticky="e", pady=(4, 0))
        frame.columnconfigure(0, weight=1)

    def _place(self) -> None:
        self.update_idletasks()
        w, h = self.WIDTH, self.winfo_reqheight()
        x, y = self._anchor
        left, top, right, bottom = work_area(x, y)
        x = min(max(x - w // 2, left + 8), right - w - 8)
        y = min(max(y - h - 12, top + 8), bottom - h - 8)
        self.geometry(f"{w}x{h}+{x}+{y}")
