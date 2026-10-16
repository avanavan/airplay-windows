"""One speaker's connection: connect, stream the captured audio, report state, disconnect."""

from __future__ import annotations

import logging
import socket
import threading

from airplay_windows.audio.buffer import PcmFanout
from airplay_windows.protocol import PairingError, Receiver, RtspError, Session, open_session

IDLE, CONNECTING, PLAYING, FAILED, NEEDS_PASSWORD = "idle", "connecting", "playing", "failed", "needs_password"


class SpeakerLog(logging.LoggerAdapter):
    """Prefixes every line with the speaker's name."""

    def process(self, msg, kwargs):
        return f"[{self.extra['speaker']}] {msg}", kwargs


class SpeakerConnection:
    """Runs on its own thread; `on_state(state, detail)` is called from that thread."""

    def __init__(self, receiver: Receiver, fanout: PcmFanout, *, password: str | None = None,
                 airplay2: bool | None = None, latency_ms: int = 2000, volume: float = 35,
                 on_state=lambda state, detail: None):
        self.receiver = receiver
        self.fanout = fanout
        self.password = password
        self.use_airplay2 = receiver.requires_password_pairing if airplay2 is None else airplay2
        self.latency_ms = latency_ms
        self.volume = volume
        self.on_state = on_state
        self.log = SpeakerLog(logging.getLogger("airplay_windows.speaker"), {"speaker": receiver.name})
        self.state = IDLE
        self.suggest_airplay2 = False   # a 403 to AirPlay 1: retry with password pairing
        self.session: Session | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._volume_lock = threading.Lock()
        self._volume_busy = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"speaker-{self.receiver.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Finish streaming; TEARDOWN is sent from the connection's own thread."""
        self._stop.set()
        if self.state == CONNECTING and self.session:
            self.session.abort()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        self.on_state(state, detail)

    def _run(self) -> None:
        sub = None
        session = None
        try:
            if self.use_airplay2 and not self.password:
                self._set_state(NEEDS_PASSWORD, "This speaker requires its AirPlay password.")
                return
            self._set_state(CONNECTING)
            session = self.session = open_session(self.receiver, self.password, airplay2=self.use_airplay2,
                                                  latency_ms=self.latency_ms, log=self.log)
            connect_volume = self.volume
            session.connect(volume_percent=connect_volume)
            if self._stop.is_set():
                self._set_state(IDLE)
                return
            sub = self.fanout.subscribe()
            self._set_state(PLAYING)
            if self.volume != connect_volume:   # the slider moved during the handshake
                self.set_volume(self.volume)
            session.stream(sub, self._stop)
            self._set_state(IDLE if self._stop.is_set() else FAILED, "" if self._stop.is_set() else "Stream ended")
        except Exception as exc:   # report every failure instead of losing the thread
            if self._stop.is_set():
                self._set_state(IDLE)
            else:
                self._set_state(*self._explain(exc, session))
        finally:
            if sub:
                self.fanout.unsubscribe(sub)
            if session:
                session.close()
            self.session = None

    def _explain(self, exc: Exception, session: Session | None) -> tuple[str, str]:
        if isinstance(exc, PairingError):
            if "wrong password" in str(exc) or "authentication failed" in str(exc):
                return NEEDS_PASSWORD, "Wrong password."
            return FAILED, str(exc)
        if isinstance(exc, RtspError):
            if exc.status == 401:
                return NEEDS_PASSWORD, "Wrong password." if self.password else "This speaker requires a password."
            if exc.status == 403 and not self.use_airplay2:
                self.suggest_airplay2 = True
                return NEEDS_PASSWORD, ("The speaker refused the connection (403). If “Require Password” "
                                        "is on in the Home app, enter that password.")
            if exc.status == 403:
                return FAILED, ("The speaker refused the connection (403). In the Home app, check that speaker "
                                "access isn't limited to people in your Home.")
            return FAILED, str(exc)
        if isinstance(exc, socket.timeout):
            streamer = session.streamer if session else None
            if streamer and self.state == CONNECTING and streamer.timing_replies == 0:
                return FAILED, ("Timed out, and none of the speaker's clock-sync packets got through: Windows "
                                "Firewall is probably blocking them. Allow the app through the firewall.")
            return FAILED, "Timed out waiting for the speaker."
        if isinstance(exc, ConnectionRefusedError):
            return FAILED, "Connection refused: the speaker isn't accepting AirPlay at this address."
        if isinstance(exc, OSError):
            return FAILED, f"Connection lost: {exc.strerror or exc}"
        self.log.exception("Unexpected error")
        return FAILED, f"{type(exc).__name__}: {exc}"

    def set_volume(self, percent: float) -> None:
        """Change the volume; rapid changes are coalesced into one request at a time."""
        with self._volume_lock:
            self.volume = percent
            if self._volume_busy or self.state != PLAYING:
                return
            self._volume_busy = True
        threading.Thread(target=self._apply_volume, daemon=True).start()

    def _apply_volume(self) -> None:
        applied = None
        while True:
            with self._volume_lock:
                session = self.session
                if self.volume == applied or self.state != PLAYING or not session:
                    self._volume_busy = False
                    return
                target = self.volume
            try:
                session.set_volume(target)
                applied = target
            except Exception as exc:
                self.log.warning("Volume change failed: %s", exc)
                with self._volume_lock:
                    self._volume_busy = False
                return
