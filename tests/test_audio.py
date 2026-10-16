"""Jitter buffers, fan-out and sample conversion."""

from airplay_windows.audio.buffer import PcmFanout, Subscription
from airplay_windows.audio.capture import float_to_pcm16
from airplay_windows.protocol.rtp import PCM_BYTES_PER_PACKET as PACKET


def test_subscription_primes_then_plays_in_order():
    sub = Subscription(target_ms=20, max_ms=300)
    assert sub.read() == bytes(PACKET)          # nothing buffered yet: silence
    data = bytes(range(256)) * 40              # more than the 20 ms target
    sub.push(data)
    assert sub.read() == data[:PACKET]
    assert sub.read() == data[PACKET: 2 * PACKET]


def test_subscription_pads_on_underrun_and_reprimes():
    sub = Subscription(target_ms=10, max_ms=300)
    sub.push(b"\x01" * (sub.target + 100))
    sub.read()
    tail = sub.read()
    assert tail.startswith(b"\x01") and tail.endswith(b"\x00") and len(tail) == PACKET
    assert sub.underruns == 1
    sub.push(b"\x02" * 8)                      # below the target again: still priming
    assert sub.read() == bytes(PACKET)


def test_subscription_drops_oldest_audio_on_overrun():
    sub = Subscription(target_ms=20, max_ms=100)
    sub.push(b"\x01" * sub.limit)
    sub.push(b"\x02" * 400)
    assert sub.overruns == 1
    assert len(sub._buf) == sub.target and sub._buf.endswith(b"\x02" * 400)


def test_fanout_copies_to_every_subscriber():
    fanout = PcmFanout()
    a, b = fanout.subscribe(target_ms=1), fanout.subscribe(target_ms=1)
    fanout.push(b"\x05" * PACKET)
    assert a.read() == b.read() == b"\x05" * PACKET
    fanout.unsubscribe(a)
    assert fanout.listeners == 1 and list(a) == []


def test_float_to_pcm16_clips_and_interleaves():
    import numpy as np

    pcm = float_to_pcm16(np.array([[0.5, -0.5], [2.0, -2.0]], dtype=np.float32))
    assert pcm == b"".join(v.to_bytes(2, "little", signed=True) for v in (16383, -16383, 32767, -32767))
