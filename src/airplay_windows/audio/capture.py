"""Record what Windows plays (WASAPI loopback) as 44.1 kHz 16-bit stereo PCM."""

from __future__ import annotations

import logging
import threading
import time
import warnings

from airplay_windows.audio.buffer import PcmFanout
from airplay_windows.protocol.rtp import SAMPLE_RATE

log = logging.getLogger(__name__)


def list_playback_devices() -> list[tuple[str, str]]:
    """(endpoint id, name) of every playback device."""
    import soundcard

    return [(s.id, s.name) for s in soundcard.all_speakers()]


def float_to_pcm16(data) -> bytes:
    """soundcard's float32 frames (n x 2) -> 16-bit little-endian interleaved PCM."""
    import numpy as np

    return (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


class LoopbackCapture:
    """Records one playback device, or the Windows default (following changes), on a thread.

    Windows converts the device's mix to 44.1 kHz for us, so no resampling is needed.
    """

    CHECK_DEFAULT_EVERY = 3.0

    def __init__(self, fanout: PcmFanout):
        self.fanout = fanout
        self.device_id: str | None = None   # None follows the Windows default output
        self.active_name = ""
        self.error = ""
        self.peak = 0.0
        self._restart = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, device_id: str | None = None) -> None:
        self.device_id = device_id
        self._thread = threading.Thread(target=self._run, name="loopback-capture", daemon=True)
        self._thread.start()

    def set_device(self, device_id: str | None) -> None:
        """Switch devices; with the same value, re-resolve the default right away."""
        self.device_id = device_id
        self._restart.set()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        _com_initialize()
        import soundcard

        warnings.filterwarnings("ignore", category=soundcard.SoundcardRuntimeWarning)
        while not self._stop.is_set():
            self._restart.clear()
            wanted = self.device_id
            try:
                speaker = soundcard.get_speaker(wanted) if wanted else soundcard.default_speaker()
                mic = soundcard.get_microphone(id=speaker.id, include_loopback=True)
                self.active_name, self.error = speaker.name, ""
                log.info("Capturing system audio from %s", speaker.name)
                with mic.recorder(samplerate=SAMPLE_RATE, channels=2) as recorder:
                    self._pump(recorder, speaker.id, following_default=wanted is None)
            except Exception as exc:  # unplugged device, exclusive mode, driver errors...
                self.active_name, self.error, self.peak = "", str(exc) or type(exc).__name__, 0.0
                log.warning("Audio capture error: %s; retrying", self.error)
                self._stop.wait(2.0)

    def _pump(self, recorder, speaker_id: str, following_default: bool) -> None:
        import numpy as np
        import soundcard

        next_check = time.monotonic() + self.CHECK_DEFAULT_EVERY
        while not self._stop.is_set() and not self._restart.is_set():
            data = recorder.record(numframes=None)
            if len(data):
                self.peak = max(float(np.abs(data).max()), self.peak * 0.7)
                self.fanout.push(float_to_pcm16(data))
            else:
                self.peak *= 0.7
                time.sleep(0.005)
            if following_default and time.monotonic() >= next_check:
                next_check = time.monotonic() + self.CHECK_DEFAULT_EVERY
                if soundcard.default_speaker().id != speaker_id:
                    log.info("The Windows default output changed; following it")
                    return


def _com_initialize() -> None:
    """WASAPI is COM; each thread that uses it initialises COM first."""
    try:
        import ctypes

        ctypes.windll.ole32.CoInitializeEx(None, 0)
    except (AttributeError, OSError):
        pass
