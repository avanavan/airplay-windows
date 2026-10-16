"""Test sources for the command line: a sine tone and WAV files."""

from __future__ import annotations

import array
import math
import wave

from airplay_windows.protocol.rtp import FRAMES_PER_PACKET, SAMPLE_RATE


def tone(freq: float = 440.0, seconds: float = 10.0, amplitude: float = 0.3):
    """Stereo sine tone in 352-frame chunks."""
    total = int(seconds * SAMPLE_RATE)
    step = 2 * math.pi * freq / SAMPLE_RATE
    n = 0
    while n < total:
        frames = min(FRAMES_PER_PACKET, total - n)
        chunk = array.array("h")
        for i in range(frames):
            v = int(32767 * amplitude * math.sin((n + i) * step))
            chunk.append(v)
            chunk.append(v)
        n += frames
        yield chunk.tobytes()


def wav_file(path: str):
    """44.1 kHz 16-bit WAV in 352-frame chunks; mono is duplicated to stereo."""
    with wave.open(path, "rb") as w:
        if w.getframerate() != SAMPLE_RATE or w.getsampwidth() != 2 or w.getnchannels() not in (1, 2):
            raise ValueError("the WAV file must be 44100 Hz, 16-bit, mono or stereo")
        mono = w.getnchannels() == 1
        while True:
            data = w.readframes(FRAMES_PER_PACKET)
            if not data:
                return
            if mono:
                src = array.array("h")
                src.frombytes(data)
                data = array.array("h", (s for s in src for _ in (0, 1))).tobytes()
            yield data
