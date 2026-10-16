"""Hand captured audio to every connected speaker without ever blocking a streamer.

The capture runs on the sound card's clock and each streamer on the PC's, so
every speaker reads through its own small jitter buffer: silence until it
fills, the oldest audio dropped if it overfills.
"""

from __future__ import annotations

import threading

from airplay_windows.protocol.rtp import PCM_BYTES_PER_PACKET, SAMPLE_RATE

BYTES_PER_MS = SAMPLE_RATE * 4 // 1000


class Subscription:
    """One speaker's view of the captured audio: an endless iterator of 352-frame packets."""

    def __init__(self, target_ms: int = 60, max_ms: int = 300):
        self.target = target_ms * BYTES_PER_MS // 4 * 4
        self.limit = max(max_ms * BYTES_PER_MS // 4 * 4, self.target + PCM_BYTES_PER_PACKET)
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._priming = True
        self.closed = False
        self.underruns = 0
        self.overruns = 0

    def push(self, pcm: bytes) -> None:
        with self._lock:
            self._buf += pcm
            if len(self._buf) > self.limit:
                del self._buf[: len(self._buf) - self.target]
                self.overruns += 1

    def read(self) -> bytes:
        with self._lock:
            if self._priming:
                if len(self._buf) < self.target:
                    return bytes(PCM_BYTES_PER_PACKET)
                self._priming = False
            if len(self._buf) >= PCM_BYTES_PER_PACKET:
                out = bytes(self._buf[:PCM_BYTES_PER_PACKET])
                del self._buf[:PCM_BYTES_PER_PACKET]
                return out
            # Ran dry: play what is left, then silence until the buffer refills.
            out = bytes(self._buf) + bytes(PCM_BYTES_PER_PACKET - len(self._buf))
            self._buf.clear()
            self._priming = True
            self.underruns += 1
            return out

    def __iter__(self):
        while not self.closed:
            yield self.read()


class PcmFanout:
    def __init__(self):
        self._subs: list[Subscription] = []
        self._lock = threading.Lock()

    def subscribe(self, **kwargs) -> Subscription:
        sub = Subscription(**kwargs)
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        sub.closed = True
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def push(self, pcm: bytes) -> None:
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            sub.push(pcm)

    @property
    def listeners(self) -> int:
        with self._lock:
            return len(self._subs)
