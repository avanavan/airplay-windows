"""The speaker row in the main window, and the password dialog."""

from __future__ import annotations

import tkinter as tk
from tkinter import simpledialog, ttk
from typing import TYPE_CHECKING

from airplay_windows.core.connection import CONNECTING, FAILED, IDLE, NEEDS_PASSWORD, PLAYING
from airplay_windows.protocol.receiver import Receiver

if TYPE_CHECKING:
    from airplay_windows.ui.app import App

GREY, AMBER, GREEN, RED = "#7a7a7a", "#b26a00", "#1b8a3a", "#c62828"
STATUS = {IDLE: ("Not connected", GREY), CONNECTING: ("Connecting…", AMBER), PLAYING: ("Playing", GREEN),
          FAILED: ("Failed", RED), NEEDS_PASSWORD: ("Password needed", AMBER)}


class PasswordDialog(simpledialog.Dialog):
    def __init__(self, parent, speaker: str, message: str):
        self.speaker, self.message = speaker, message
        self.result = None
        super().__init__(parent, title=f"Password for {speaker}")

    def body(self, master):
        ttk.Label(master, text=self.message, wraplength=340, justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(master, text="AirPlay password:").grid(row=1, column=0, sticky="w")
        self.entry = ttk.Entry(master, show="•", width=28)
        self.entry.grid(row=1, column=1, sticky="we", padx=(6, 0))
        self.remember = tk.BooleanVar(value=True)
        ttk.Checkbutton(master, text="Remember on this PC (encrypted for your Windows account)",
                        variable=self.remember).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        return self.entry

    def apply(self):
        self.result = (self.entry.get(), self.remember.get())


class SpeakerRow(ttk.Frame):
    """One speaker: name, model and address, status, Connect/Disconnect, volume, and a ⋯ menu."""

    def __init__(self, parent, app: "App", receiver: Receiver):
        super().__init__(parent, padding=(14, 10, 10, 10))
        self.app = app
        self.key = receiver.key
        self.receiver = receiver
        self.state = IDLE
        self._volume_job = None
        self.separator = ttk.Separator(parent)

        self.title = ttk.Label(self, style="Name.TLabel")
        self.title.grid(row=0, column=0, sticky="w")
        self.detail = ttk.Label(self, style="Detail.TLabel")
        self.detail.grid(row=1, column=0, sticky="w")
        self.button = ttk.Button(self, width=12, command=self.toggle)
        self.button.grid(row=0, column=1, rowspan=2, padx=(8, 0))
        self.menu_button = ttk.Button(self, text="⋯", width=3, command=self._show_menu)
        self.menu_button.grid(row=0, column=2, rowspan=2, padx=(4, 0))
        self.status = ttk.Label(self, wraplength=380, justify="left")
        self.status.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 2))

        volume_row = ttk.Frame(self)
        volume_row.grid(row=3, column=0, columnspan=3, sticky="we")
        ttk.Label(volume_row, text="Volume", style="Detail.TLabel").pack(side="left")
        self.volume = tk.DoubleVar(value=app.settings.volume(self.key))
        ttk.Scale(volume_row, from_=0, to=100, variable=self.volume, command=self.on_slide).pack(
            side="left", fill="x", expand=True, padx=8)
        self.volume_label = ttk.Label(volume_row, width=4, anchor="e")
        self.volume_label.pack(side="left")
        self.columnconfigure(0, weight=1)

        self.auto = tk.BooleanVar(value=app.settings.auto_connect(self.key))
        self.menu = tk.Menu(self, tearoff=False)
        self.menu.add_checkbutton(label="Connect when the app starts", variable=self.auto,
                                  command=lambda: app.set_auto_connect(self.key, self.auto.get()))
        self.menu.add_command(label="Forget saved password", command=lambda: app.forget_password(self.key))
        self.menu.add_command(label="Copy address", command=self._copy_address)
        self.menu.add_separator()
        self.menu.add_command(label="Remove from list", command=lambda: app.remove_speaker(self.key))

        self.update_receiver(receiver)
        self.show_volume()
        self.show_state(IDLE)

    def update_receiver(self, receiver: Receiver) -> None:
        self.receiver = receiver
        self.title.configure(text=receiver.name + ("  🔒" if receiver.needs_password else ""))
        self.detail.configure(text=f"{receiver.model_name}  ·  {receiver.host}:{receiver.port}")

    def show_state(self, state: str, detail: str = "") -> None:
        self.state = state
        text, color = STATUS[state]
        if detail and state in (FAILED, NEEDS_PASSWORD):
            text = detail
        self.status.configure(text="● " + text, foreground=color)
        self.button.configure(text="Disconnect" if state == PLAYING else "Cancel" if state == CONNECTING else "Connect")

    def show_stopping(self) -> None:
        self.status.configure(text="● Disconnecting…", foreground=AMBER)

    def toggle(self) -> None:
        if self.state in (CONNECTING, PLAYING):
            self.app.disconnect(self.key)
        else:
            self.app.connect(self.key)

    def on_slide(self, _value=None) -> None:
        """Slider moved (here or in the tray panel): apply 150 ms after the last change."""
        self.show_volume()
        if self._volume_job:
            self.after_cancel(self._volume_job)
        self._volume_job = self.after(150, self._apply_volume)

    def show_volume(self) -> None:
        self.volume_label.configure(text=str(round(self.volume.get())))

    def _apply_volume(self) -> None:
        self._volume_job = None
        self.app.set_volume(self.key, round(self.volume.get()))

    def _show_menu(self) -> None:
        b = self.menu_button
        self.menu.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def _copy_address(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.receiver.host)
