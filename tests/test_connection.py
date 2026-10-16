"""SpeakerConnection: the per-speaker thread the app drives, against the fake speaker."""

import socket
import threading
import time

import pytest

from airplay_windows.audio.buffer import PcmFanout
from airplay_windows.core import connection as c
from airplay_windows.core.connection import SpeakerConnection
from airplay_windows.protocol import Receiver, raop, rtp
from fake_speaker import PROFILES, FakeSpeaker


class Feeder:
    """Stands in for the loopback capture: pushes a known ramp in real time."""

    def __init__(self, fanout):
        self.fanout = fanout
        self.stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        n = 0
        while not self.stop.is_set():
            self.fanout.push(b"".join(((n + i) % 30000).to_bytes(2, "little") * 2 for i in range(441)))
            n += 441
            time.sleep(0.01)


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def speaker():
    s = FakeSpeaker()
    threading.Thread(target=s.serve_once, daemon=True).start()
    yield s
    s.close()


def make(speaker, profile="homepod", **kwargs):
    states = []
    receiver = Receiver("AABBCCDDEEFF@Fake", "127.0.0.1", speaker.port, dict(PROFILES[profile]))
    fanout = PcmFanout()
    conn = SpeakerConnection(receiver, fanout, on_state=lambda s, d: states.append((s, d)), **kwargs)
    return conn, fanout, states


def test_streams_changes_volume_and_tears_down(speaker):
    conn, fanout, states = make(speaker, volume=35)
    feeder = Feeder(fanout)
    try:
        conn.start()
        assert wait_for(lambda: conn.state == c.PLAYING)
        assert wait_for(lambda: len(speaker.audio_packets) > 60)
        conn.set_volume(80)
        assert wait_for(lambda: speaker.volume_db == pytest.approx(rtp.percent_to_db(80)))
        conn.stop()
        conn.join(5)
        assert not conn.alive and [s for s, _ in states] == [c.CONNECTING, c.PLAYING, c.IDLE]
        assert speaker.requests[-1][0] == "TEARDOWN" and fanout.listeners == 0
        pcm = speaker.pcm()
        samples = [int.from_bytes(pcm[i:i + 2], "little") for i in range(0, len(pcm), 4)]
        start = next(i for i, v in enumerate(samples) if v)
        run = samples[start:start + 2000]
        assert all((b - a) % 30000 == 1 for a, b in zip(run, run[1:]))   # the ramp arrived intact
    finally:
        feeder.stop.set()


def test_volume_changes_do_not_collide_with_keepalives(speaker):
    conn, fanout, _ = make(speaker)
    feeder = Feeder(fanout)
    try:
        conn.start()
        assert wait_for(lambda: conn.state == c.PLAYING)
        conn.session.keepalive_interval = 0.05   # hammer the RTSP connection with OPTIONS
        for v in range(20, 60, 2):
            conn.set_volume(v)
            time.sleep(0.03)
        assert wait_for(lambda: speaker.volume_db == pytest.approx(rtp.percent_to_db(58)))
        assert conn.state == c.PLAYING
        conn.stop()
        conn.join(5)
        assert conn.state == c.IDLE
    finally:
        feeder.stop.set()


def test_password_is_requested_then_used():
    speaker = FakeSpeaker(password="secret")
    threading.Thread(target=speaker.serve_once, daemon=True).start()
    try:
        conn, _, states = make(speaker, profile="plain")
        conn.start()
        conn.join(5)
        assert states[-1] == (c.NEEDS_PASSWORD, "This speaker requires a password.")
        threading.Thread(target=speaker.serve_once, daemon=True).start()
        retry, _, states = make(speaker, profile="plain", password="secret")
        retry.start()
        assert wait_for(lambda: retry.state == c.PLAYING)
        retry.stop()
        retry.join(5)
        assert [s for s, _ in states] == [c.CONNECTING, c.PLAYING, c.IDLE]
    finally:
        speaker.close()


def test_password_pairing_receiver_without_password_asks_for_it():
    states = []
    conn = SpeakerConnection(Receiver("EAC4C5275084@Bedroom", "127.0.0.1", 1, {"sf": "0x80484"}), PcmFanout(),
                             on_state=lambda s, d: states.append((s, d)))
    assert conn.use_airplay2
    conn.start()
    conn.join(5)
    assert states == [(c.NEEDS_PASSWORD, "This speaker requires its AirPlay password.")]


def test_unreachable_speaker_fails_with_a_message():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]   # nothing listens here once closed
    states = []
    conn = SpeakerConnection(Receiver("AABBCCDDEEFF@Gone", "127.0.0.1", port, dict(PROFILES["plain"])), PcmFanout(),
                             on_state=lambda s, d: states.append((s, d)))
    conn.start()
    conn.join(10)
    assert states[-1][0] == c.FAILED and "refused" in states[-1][1]
