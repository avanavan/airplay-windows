"""RTP packets for AirPlay audio: audio, sync, timing and retransmission."""

from __future__ import annotations

import math
import struct
import time

SAMPLE_RATE = 44100
FRAMES_PER_PACKET = 352
BYTES_PER_FRAME = 4                                     # 16-bit stereo
PCM_BYTES_PER_PACKET = FRAMES_PER_PACKET * BYTES_PER_FRAME
NTP_EPOCH_OFFSET = 2208988800

PT_TIMING_REQUEST = 82
PT_TIMING_RESPONSE = 83
PT_SYNC = 84
PT_RESEND_REQUEST = 85
PT_RESEND_RESPONSE = 86
PT_AUDIO = 96

_CLOCK_START = time.perf_counter()


def ntp_now() -> tuple[int, int]:
    """A monotonic clock in NTP format. Receivers only need it to be steady, not wall time."""
    elapsed = time.perf_counter() - _CLOCK_START
    whole = int(elapsed)
    return whole + NTP_EPOCH_OFFSET, int((elapsed - whole) * 4294967296.0) & 0xFFFFFFFF


def header(payload_type: int, seq: int, marker: bool) -> bytes:
    """RTP version 2, no padding, no extension."""
    return bytes([0x80, (0x80 if marker else 0) | payload_type]) + struct.pack(">H", seq & 0xFFFF)


def audio_packet(seq: int, timestamp: int, ssrc: int, payload: bytes) -> bytes:
    return header(PT_AUDIO, seq, marker=False) + struct.pack(">II", timestamp & 0xFFFFFFFF, ssrc) + payload


def resend_response(packet: bytes) -> bytes:
    """AirPlay 1 retransmission: type 86 around the original packet."""
    return header(PT_RESEND_RESPONSE, math.ceil(len(packet) / 4), marker=True) + packet


def sync_packet(rtp_playing_now: int, rtp_next: int, first: bool = False) -> bytes:
    """Ties an RTP timestamp to the NTP clock: `rtp_playing_now` is what should be heard now."""
    sec, frac = ntp_now()
    head = header(PT_SYNC, 7, marker=True)
    if first:
        head = bytes([head[0] | 0x10]) + head[1:]   # extension bit marks the first sync
    return head + struct.pack(">IIII", rtp_playing_now & 0xFFFFFFFF, sec, frac, rtp_next & 0xFFFFFFFF)


def timing_response(request: bytes) -> bytes | None:
    """Answer a receiver's NTP-style timing request, echoing its send time."""
    if len(request) != 32 or request[1] & 0x7F != PT_TIMING_REQUEST:
        return None
    seq = struct.unpack(">H", request[2:4])[0]
    sec, frac = ntp_now()
    return header(PT_TIMING_RESPONSE, seq, marker=True) + bytes(4) + request[24:32] + struct.pack(
        ">IIII", sec, frac, sec, frac)


def percent_to_db(percent: float) -> float:
    """AirPlay volume: -30 dB (quietest) to 0 dB, and -144 dB for mute."""
    if percent <= 0:
        return -144.0
    if percent >= 100:
        return -0.001
    return -30.0 * ((100.0 - percent) / 100.0)
