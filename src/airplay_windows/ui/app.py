"""The desktop app: capture what Windows plays and stream it to the AirPlay speakers you connect."""

from __future__ import annotations

import argparse
import logging
import math
import os
import queue
import signal
import sys
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

from airplay_windows import DISPLAY_NAME, __version__, discovery, logs, paths
from airplay_windows.audio.buffer import PcmFanout
from airplay_windows.audio.capture import LoopbackCapture, list_playback_devices
from airplay_windows.core.connection import CONNECTING, FAILED, IDLE, NEEDS_PASSWORD, PLAYING, SpeakerConnection
from airplay_windows.core.output_switch import OutputSwitcher
from airplay_windows.core.settings import Settings
from airplay_windows.protocol.receiver import Receiver
from airplay_windows.ui.widgets import GREY, RED, PasswordDialog, SpeakerRow
from airplay_windows.windows import firewall

log = logging.getLogger(__name__)

BUFFERS = [("0.25 s (lowest delay, may stutter)", 250), ("0.5 s", 500), ("1 s", 1000), ("2 s (recommended)", 2000)]
DEFAULT_SOURCE = "Windows default output"
NO_SWITCH = "Don't switch"


class QueueHandler(logging.Handler):
    """Log records -> the app's event queue, shown in the log panel."""

    def __init__(self, events: queue.Queue):
        super().__init__(logging.INFO)
        self.events = events
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        self.events.put(("log", self.format(record)))


class App:
    def __init__(self, root: tk.Tk, minimized: bool = False, settings: Settings | None = None,
                 use_tray: bool = True, check_firewall: bool = True):
        self.root = root
        self.settings = settings or Settings()
        self.events: queue.Queue = queue.Queue()
        self._log_handler = QueueHandler(self.events)
        app_log = logging.getLogger("airplay_windows")
        app_log.addHandler(self._log_handler)
        if app_log.getEffectiveLevel() > logging.INFO:
            app_log.setLevel(logging.INFO)   # the log panel shows INFO, whoever set up logging
        self.fanout = PcmFanout()
        self.capture = LoopbackCapture(self.fanout)
        self.rows: dict[str, SpeakerRow] = {}
        self.connections: dict[str, SpeakerConnection] = {}
        self.sources: list[tuple[str | None, str]] = []
        self.switch_targets: list[tuple[str | None, str]] = []
        self.firewall_status: firewall.FirewallStatus | None = None
        self.tray = None
        self._searching = False
        self._auto_connect_done = False
        self._closing = False
        self._firewall_asked = False
        self._firewall_busy = False
        self._tray_state = None
        self._told_about_tray = False

        root.title(DISPLAY_NAME)
        root.geometry("600x780")
        root.minsize(480, 460)
        root.report_callback_exception = self._report_exception
        self._style()
        self._build()

        self.switcher = OutputSwitcher(self.settings)
        if self.switcher.recover():
            log.info("Restored the Windows default output that the last session left switched")
        for receiver in self.settings.known_receivers():
            self._add_or_update(receiver)
        self._load_sources()
        self.capture.start(self.settings.data.get("source"))
        self.refresh()
        if check_firewall and sys.platform == "win32":
            threading.Thread(target=lambda: self.events.put(("firewall", firewall.check(), None, None)),
                             daemon=True).start()
        if use_tray:
            self._start_tray()
        root.protocol("WM_DELETE_WINDOW", self.hide_window if self.tray else self.close)
        if minimized and self.tray:
            root.withdraw()
        root.after(100, self._tick)

    # -- layout --

    def _style(self) -> None:
        style = ttk.Style(self.root)
        base = ("Segoe UI", 10)
        self.root.option_add("*Font", base)
        style.configure(".", font=base)
        style.configure("Name.TLabel", font=("Segoe UI Semibold", 12))
        style.configure("Detail.TLabel", foreground=GREY)
        style.configure("Header.TLabel", font=("Segoe UI Semibold", 11))
        style.configure("Hint.TLabel", foreground=GREY, font=("Segoe UI", 9))
        style.configure("Banner.TFrame", background="#fff4ce")
        style.configure("Banner.TLabel", background="#fff4ce", foreground="#5c4400")

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        # Shown only when Windows Firewall would block the speakers' UDP packets.
        self.firewall_banner = ttk.Frame(outer, style="Banner.TFrame", padding=10)
        self.firewall_button = ttk.Button(self.firewall_banner, text="Allow through firewall…",
                                          command=self.allow_firewall)
        self.firewall_button.pack(side="right", padx=(10, 0))   # packed first so the text can't squeeze it
        self.firewall_text = ttk.Label(self.firewall_banner, style="Banner.TLabel", justify="left")
        self.firewall_text.pack(side="left", fill="x", expand=True)
        self.firewall_banner.bind("<Configure>", lambda e: self.firewall_text.configure(
            wraplength=max(200, e.width - self.firewall_button.winfo_reqwidth() - 40)))

        source = self.source_frame = ttk.LabelFrame(outer, text="Audio to send", padding=10)
        source.pack(fill="x")
        ttk.Label(source, text="Capture from").grid(row=0, column=0, sticky="w")
        self.source_box = ttk.Combobox(source, state="readonly")
        self.source_box.grid(row=0, column=1, columnspan=2, sticky="we", padx=(8, 0))
        self.source_box.bind("<<ComboboxSelected>>", self._on_source)

        ttk.Label(source, text="Switch output to").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.switch_box = ttk.Combobox(source, state="readonly")
        self.switch_box.grid(row=1, column=1, columnspan=2, sticky="we", padx=(8, 0), pady=(8, 0))
        self.switch_box.bind("<<ComboboxSelected>>", self._on_switch)
        self.hints = [ttk.Label(source, style="Hint.TLabel", justify="left", text=(
            "While a speaker plays, Windows sends its sound to this device instead, so the PC stays silent "
            "(e.g. a virtual audio cable). Your previous default comes back when the last speaker "
            "disconnects or the app closes."))]
        self.hints[-1].grid(row=2, column=1, columnspan=2, sticky="w", padx=(8, 0))
        self.switch_warning = ttk.Label(source, style="Hint.TLabel", foreground=RED, justify="left")
        self.switch_warning.grid(row=3, column=1, columnspan=2, sticky="w", padx=(8, 0))
        self.switch_warning.grid_remove()
        self.hints.append(self.switch_warning)

        ttk.Label(source, text="Level").grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.level = ttk.Progressbar(source, maximum=100)
        self.level.grid(row=4, column=1, sticky="we", padx=(8, 0), pady=(8, 0))
        ttk.Button(source, text="Sound settings…", command=lambda: os.startfile("ms-settings:sound")).grid(
            row=4, column=2, padx=(8, 0), pady=(8, 0))
        self.capture_status = ttk.Label(source, style="Hint.TLabel")
        self.capture_status.grid(row=5, column=1, columnspan=2, sticky="w", padx=(8, 0))

        ttk.Label(source, text="Buffer").grid(row=6, column=0, sticky="w", pady=(8, 0))
        self.buffer_box = ttk.Combobox(source, state="readonly", values=[label for label, _ in BUFFERS])
        self.buffer_box.grid(row=6, column=1, columnspan=2, sticky="we", padx=(8, 0), pady=(8, 0))
        latency = self.settings.data.get("latency_ms", 2000)
        self.buffer_box.current(next((i for i, (_, ms) in enumerate(BUFFERS) if ms == latency), len(BUFFERS) - 1))
        self.buffer_box.bind("<<ComboboxSelected>>", self._on_buffer)
        self.hints.append(ttk.Label(source, style="Hint.TLabel", justify="left", text=(
            "The speaker plays the audio this much later than the PC. "
            "A new buffer size applies the next time you connect.")))
        self.hints[-1].grid(row=7, column=1, columnspan=2, sticky="w", padx=(8, 0))
        source.columnconfigure(1, weight=1)
        source.bind("<Configure>", self._wrap_hints)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(14, 6))
        ttk.Label(header, text="Speakers", style="Header.TLabel").pack(side="left")
        self.search_label = ttk.Label(header, style="Hint.TLabel")
        self.search_label.pack(side="left", padx=10)
        self.refresh_button = ttk.Button(header, text="Refresh", command=self.refresh)
        self.refresh_button.pack(side="right")
        ttk.Button(header, text="Add by IP…", command=self.add_by_ip).pack(side="right", padx=6)

        holder = ttk.Frame(outer, relief="solid", borderwidth=1)
        holder.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(holder, highlightthickness=0, borderwidth=0,
                                background=ttk.Style().lookup("TFrame", "background"))
        scrollbar = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.list_frame = ttk.Frame(self.canvas)
        window = self.canvas.create_window(0, 0, window=self.list_frame, anchor="nw")
        self.list_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)
        self.empty_label = ttk.Label(self.list_frame, style="Hint.TLabel", padding=16, wraplength=480)

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(8, 0))
        self.show_log = tk.BooleanVar(value=False)
        ttk.Checkbutton(bottom, text="Show log", variable=self.show_log, command=self._toggle_log).pack(side="left")
        ttk.Button(bottom, text="Quit", command=self.close).pack(side="right")
        self.summary = ttk.Label(bottom, style="Hint.TLabel")
        self.summary.pack(side="right", padx=10)
        self.log_view = ScrolledText(outer, height=9, font=("Consolas", 9), state="disabled", wrap="word")

    def _wrap_hints(self, _event=None) -> None:
        width = max(200, self.switch_box.winfo_width() - 8)
        for hint in self.hints:
            hint.configure(wraplength=width)

    def _on_wheel(self, event) -> None:
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        while widget is not None and widget is not self.canvas:
            widget = widget.master
        if widget is self.canvas:
            self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _toggle_log(self) -> None:
        if self.show_log.get():
            self.log_view.pack(fill="both", expand=False, pady=(6, 0))
        else:
            self.log_view.pack_forget()

    def _layout_rows(self) -> None:
        for child in self.list_frame.winfo_children():
            child.grid_forget()
        rows = sorted(self.rows.values(), key=lambda r: r.receiver.name.lower())
        if not rows:
            self.empty_label.configure(text="Searching for AirPlay speakers…" if self._searching else
                                       "No speakers found. Check that the PC and the speaker are on the same "
                                       "network, or use “Add by IP…”.")
            self.empty_label.grid(row=0, column=0, sticky="we")
        for i, row in enumerate(rows):
            if i:
                row.separator.grid(row=2 * i - 1, column=0, sticky="we", padx=10)
            row.grid(row=2 * i, column=0, sticky="we")
        self.list_frame.columnconfigure(0, weight=1)

    # -- audio source and output switching --

    def _load_sources(self) -> None:
        try:
            devices = list_playback_devices()
        except Exception as exc:
            devices = []
            log.warning("Could not list playback devices: %s", exc)
        self.sources = [(None, DEFAULT_SOURCE), *devices]
        self.source_box.configure(values=[name for _, name in self.sources])
        wanted = self.settings.data.get("source")
        index = next((i for i, (dev_id, _) in enumerate(self.sources) if dev_id == wanted), 0)
        if wanted and index == 0:
            self.settings.data["source"] = None   # the saved device is gone
        self.source_box.current(index)

        self.switch_targets = [(None, NO_SWITCH), *devices]
        self.switch_box.configure(values=[name for _, name in self.switch_targets])
        wanted = self.settings.data.get("switch_output")
        self.switch_box.current(next((i for i, (dev_id, _) in enumerate(self.switch_targets) if dev_id == wanted), 0))
        self._check_switch()

    def _device_name(self, device_id: str | None) -> str:
        return next((name for dev_id, name in self.sources if dev_id == device_id), device_id or "")

    def _on_source(self, _event=None) -> None:
        device_id = self.sources[self.source_box.current()][0]
        self.settings.data["source"] = device_id
        self.settings.save()
        self.capture.set_device(device_id)
        self._check_switch()

    def _on_switch(self, _event=None) -> None:
        target = self.switch_targets[self.switch_box.current()][0]
        self.settings.data["switch_output"] = target
        self.settings.save()
        self._check_switch()
        active = self.switcher.active
        if active and active["target"] != target:
            self.switcher.restore()        # undo the old switch, then apply the new choice
            self._recapture()
        self._update_output_switch()

    def _check_switch(self) -> None:
        """Warn when the switched-to device isn't the one being captured."""
        target, source = self.settings.data.get("switch_output"), self.settings.data.get("source")
        if target and source and source != target:
            self.switch_warning.grid()
            self.switch_warning.configure(text=f"Capture from is set to {self._device_name(source)}, so audio "
                                               f"Windows sends to {self._device_name(target)} won't reach the "
                                               "speakers. Choose “Windows default output” above.")
        else:
            self.switch_warning.grid_remove()

    def _update_output_switch(self) -> None:
        """Switch the Windows output when the first speaker plays; restore it when none is left."""
        states = [c.state for c in self.connections.values()]
        if PLAYING in states:
            target = self.settings.data.get("switch_output")
            if self.switcher.activate(target):
                log.info("Windows default output switched to %s", self._device_name(target))
                self._recapture()
        elif CONNECTING not in states and self.switcher.restore():
            log.info("Windows default output restored")
            self._recapture()

    def _recapture(self) -> None:
        if self.capture.device_id is None:   # following the default: pick up the change right away
            self.capture.set_device(None)

    def _on_buffer(self, _event=None) -> None:
        self.settings.data["latency_ms"] = BUFFERS[self.buffer_box.current()][1]
        self.settings.save()

    # -- speakers --

    def _add_or_update(self, receiver: Receiver) -> SpeakerRow:
        row = self.rows.get(receiver.key)
        if row is None:
            row = self.rows[receiver.key] = SpeakerRow(self.list_frame, self, receiver)
        elif receiver.key not in self.connections:   # don't swap the address under a live stream
            row.update_receiver(receiver)
        self.settings.remember(receiver)
        return row

    def refresh(self) -> None:
        if self._searching:
            return
        self._searching = True
        self.refresh_button.state(["disabled"])
        self.search_label.configure(text="Searching…")
        self._layout_rows()

        def work():
            try:
                found, direct_only = discovery.discover(4.0)
            except Exception as exc:
                found, direct_only = [], set()
                log.warning("Discovery failed: %s", exc)
            self.events.put(("discovered", found, direct_only))

        threading.Thread(target=work, daemon=True).start()

    def add_by_ip(self) -> None:
        text = simpledialog.askstring("Add speaker", "IP address of the speaker (optionally address:port):",
                                      parent=self.root)
        if not text or not text.strip():
            return
        host, _, port_text = text.strip().partition(":")
        port = int(port_text) if port_text.isdigit() else None
        self.search_label.configure(text=f"Asking {host}…")

        def work():
            receiver = discovery.query_host(host, port)
            if receiver is None:
                log.info("%s did not answer an mDNS query; adding it as a manual AirPlay speaker", host)
                receiver = Receiver.manual(host, port or 7000)
            self.events.put(("added", receiver))

        threading.Thread(target=work, daemon=True).start()

    def connect(self, key: str, password: str | None = None, airplay2: bool | None = None) -> None:
        row = self.rows.get(key)
        if row is None or key in self.connections:
            return
        status = self.firewall_status
        if status and status.needed and status.fixable and not self._firewall_asked and not self._firewall_busy:
            self._firewall_asked = True
            self.show_window()   # the question needs context, also when Connect came from the tray
            if messagebox.askyesno("Allow through Windows Firewall?", status.message + "\n\nAdd a firewall rule "
                                   "for this app now? Windows will ask for administrator permission.",
                                   parent=self.root):
                self.allow_firewall(then=lambda: self.connect(key, password, airplay2))
                return
        conn = SpeakerConnection(row.receiver, self.fanout, password=password or self.settings.password(key),
                                 airplay2=airplay2, latency_ms=self.settings.data.get("latency_ms", 2000),
                                 volume=round(row.volume.get()))
        conn.on_state = lambda state, detail: self.events.put(("state", key, conn, state, detail))
        self.connections[key] = conn
        row.show_state(CONNECTING)
        conn.start()

    def disconnect(self, key: str) -> None:
        conn = self.connections.get(key)
        if conn:
            conn.stop()
            self.rows[key].show_stopping()

    def set_volume(self, key: str, percent: float) -> None:
        self.settings.set_volume(key, percent)
        self.settings.save()
        conn = self.connections.get(key)
        if conn:
            conn.set_volume(percent)

    def set_row_volume(self, key: str, percent: float) -> None:
        """Set a speaker's volume from outside its row (the tray menu), keeping the slider in sync."""
        row = self.rows.get(key)
        if row:
            row.volume.set(min(100, max(0, round(percent))))
            row.show_volume()
            self.set_volume(key, row.volume.get())

    def set_auto_connect(self, key: str, value: bool) -> None:
        self.settings.set_auto_connect(key, value)
        self.settings.save()

    def forget_password(self, key: str) -> None:
        self.settings.set_password(key, None)
        self.settings.save()
        log.info("Forgot the saved password for %s", self.rows[key].receiver.name)

    def remove_speaker(self, key: str) -> None:
        if key in self.connections:
            messagebox.showinfo("Remove speaker", "Disconnect the speaker first.", parent=self.root)
            return
        row = self.rows.pop(key)
        row.separator.destroy()
        row.destroy()
        self.settings.forget(key)
        self.settings.save()
        self._layout_rows()

    def _on_state(self, key: str, conn: SpeakerConnection, state: str, detail: str) -> None:
        if self.connections.get(key) is not conn or key not in self.rows:
            return   # a connection that was already replaced
        row = self.rows[key]
        if state in (IDLE, FAILED, NEEDS_PASSWORD):
            del self.connections[key]
        row.show_state(state, detail)
        self._update_output_switch()
        if state == FAILED:
            log.warning("[%s] %s", row.receiver.name, detail)
        if state == NEEDS_PASSWORD and not self._closing:
            if conn.password and detail.startswith("Wrong password"):
                self.settings.set_password(key, None)
                self.settings.save()
            self.show_window()
            dialog = PasswordDialog(self.root, row.receiver.name, detail)
            if dialog.result and dialog.result[0]:
                password, remember = dialog.result
                if remember:
                    self.settings.set_password(key, password)
                    self.settings.save()
                # A 403 to AirPlay 1 means the password has to go through AirPlay 2 pairing.
                self.connect(key, password=password, airplay2=conn.use_airplay2 or conn.suggest_airplay2)

    def _on_discovered(self, found: list[Receiver], direct_only: set[str]) -> None:
        self._searching = False
        self.refresh_button.state(["!disabled"])
        for receiver in found:
            self._add_or_update(receiver)
        self.settings.save()
        note = f"{len(found)} found" if found else "none found"
        if direct_only:
            note += f" ({len(direct_only)} only by direct query)"
        self.search_label.configure(text=note)
        self._layout_rows()
        if not self._auto_connect_done:
            self._auto_connect_done = True
            for key, row in self.rows.items():
                if self.settings.auto_connect(key):
                    log.info("Auto-connecting %s", row.receiver.name)
                    self.connect(key)

    # -- Windows Firewall --

    def allow_firewall(self, then=None) -> None:
        """Add the inbound rule (UAC prompt) on a thread, then re-check; `then` runs afterwards either way."""
        status = self.firewall_status
        if not status or self._firewall_busy:
            return
        self._firewall_busy = True
        self.firewall_button.state(["disabled"])
        self.firewall_text.configure(text="Waiting for administrator permission…")

        def work():
            try:
                self.events.put(("firewall", firewall.allow(status), None, then))
            except PermissionError:
                self.events.put(("firewall", status, "declined", then))
            except Exception as exc:
                self.events.put(("firewall", status, str(exc), then))

        threading.Thread(target=work, daemon=True).start()

    def _on_firewall(self, status: firewall.FirewallStatus, error: str | None, then) -> None:
        was_busy, self._firewall_busy = self._firewall_busy, False
        self.firewall_status = status
        if status.error:
            log.warning("Could not check Windows Firewall: %s", status.error)
        elif was_busy and not error:
            log.info("Windows Firewall rule added for %s" if not status.needed else
                     "A firewall rule was added, but Windows Firewall still blocks %s", os.path.basename(status.program))
        if error and error != "declined":
            log.warning("Could not add the firewall rule: %s", error)
            messagebox.showerror("Windows Firewall", f"Could not add the firewall rule:\n{error}", parent=self.root)
        if status.needed:
            self.firewall_text.configure(text=status.message)
            self.firewall_button.state(["!disabled"])
            if status.fixable:
                self.firewall_button.pack(side="right", padx=(10, 0), before=self.firewall_text)
            else:
                self.firewall_button.pack_forget()
            self.firewall_banner.pack(fill="x", pady=(0, 10), before=self.source_frame)
        else:
            self.firewall_banner.pack_forget()
        if then and not self._closing:
            then()

    # -- window and notification-area icon --

    def _start_tray(self) -> None:
        try:
            from PIL import ImageTk

            from airplay_windows.ui.icon import icon_image
            from airplay_windows.ui.tray import Tray

            self._window_icon = ImageTk.PhotoImage(icon_image(True, 32))
            self.root.iconphoto(True, self._window_icon)
            self.tray = Tray(self)
        except Exception as exc:   # closing the window then quits, as without a tray
            log.warning("No notification-area icon: %s", exc)
            self.tray = None

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def hide_window(self) -> None:
        self.root.withdraw()
        if self.tray and not self._told_about_tray:
            self._told_about_tray = True
            self.tray.notify("Still running. Use the icon in the notification area to connect speakers, "
                             "change volume or exit.")

    def _update_tray(self) -> None:
        snapshot = [(key, row.receiver.name, row.state, round(row.volume.get()))
                    for key, row in sorted(self.rows.items(), key=lambda kv: kv[1].receiver.name.lower())]
        if snapshot == self._tray_state:
            return
        self._tray_state = snapshot
        playing = [name for _, name, state, _ in snapshot if state == PLAYING]
        tooltip = f"{DISPLAY_NAME}: " + (f"playing on {', '.join(playing)}" if playing else "not streaming")
        self.tray.update(snapshot, tooltip, bool(playing))

    # -- event pump --

    def _append_log(self, text: str) -> None:
        self.log_view.configure(state="normal")
        self.log_view.insert("end", text + "\n")
        if int(self.log_view.index("end-1c").split(".")[0]) > 2000:
            self.log_view.delete("1.0", "500.0")
        self.log_view.see("end")
        self.log_view.configure(state="disabled")

    def _tick(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self._append_log(event[1])
                elif kind == "state":
                    self._on_state(*event[1:])
                elif kind == "discovered":
                    self._on_discovered(*event[1:])
                elif kind == "firewall":
                    self._on_firewall(*event[1:])
                elif kind == "call":
                    event[1]()
                elif kind == "added":
                    self._add_or_update(event[1])
                    self.settings.save()
                    self.search_label.configure(text=f"Added {event[1].name}")
                    self._layout_rows()
                if self._closing:
                    return   # e.g. Exit from the tray menu: the window is gone
        except queue.Empty:
            pass

        peak = self.capture.peak
        self.level["value"] = max(0.0, 100.0 + 100.0 * 20 * math.log10(peak) / 60) if peak > 0 else 0
        if self.capture.error:
            self.capture_status.configure(text=f"Capture problem: {self.capture.error}", foreground=RED)
        else:
            self.capture_status.configure(text=f"Capturing: {self.capture.active_name or '…'}", foreground=GREY)
        playing = sum(1 for c in self.connections.values() if c.state == PLAYING)
        self.summary.configure(text=f"Streaming to {playing} speaker{'s' if playing != 1 else ''}"
                               if playing else "Not streaming")
        if self.tray:
            self._update_tray()
        self.root.after(100, self._tick)

    def _report_exception(self, exc_type, exc, tb) -> None:
        log.error("Unexpected error in the window", exc_info=(exc_type, exc, tb))

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.root.withdraw()
        if self.tray:
            self.tray.stop()
        for conn in list(self.connections.values()):
            conn.stop()
        for conn in list(self.connections.values()):
            conn.join(timeout=3)   # let each speaker get its TEARDOWN
        if self.switcher.restore():
            log.info("Windows default output restored")
        self.capture.stop()
        self.settings.save()
        logging.getLogger("airplay_windows").removeHandler(self._log_handler)
        self.root.destroy()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="airplay-windows", description="Use AirPlay speakers as the PC's audio output.")
    parser.add_argument("--minimized", action="store_true", help="start in the notification area")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    logs.setup(console=False, to_file=True)
    log.info("%s %s starting (log: %s)", DISPLAY_NAME, __version__, paths.log_file())

    instance = None
    if sys.platform == "win32":
        from airplay_windows.windows.single_instance import SingleInstance

        instance = SingleInstance()
        if not instance.first:
            instance.signal_running_instance()   # bring the running copy forward instead
            return 0
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # crisp text on high-DPI screens
        except (AttributeError, OSError):
            pass

    root = tk.Tk()
    app = App(root, minimized=args.minimized)
    if instance:
        instance.listen(lambda: app.events.put(("call", app.show_window)))
    signal.signal(signal.SIGINT, lambda *_: root.after(0, app.close))   # Ctrl+C in a console
    try:
        root.mainloop()
    finally:
        if instance:
            instance.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
