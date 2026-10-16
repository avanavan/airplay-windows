"""Apple Lossless framing for AirPlay 1.

Real compression isn't needed on a LAN: an ALAC "escape" frame carries the
samples verbatim, and every receiver decodes it.
"""

from __future__ import annotations

import array

# rtpmap/fmtp for the ANNOUNCE SDP: 352 frames per packet, 16-bit, stereo, 44.1 kHz.
FMTP = "96 352 0 16 40 10 14 2 255 0 0 44100"
_HEADER = 0b001_0000_000000000000_1_00_1  # stereo element, has-size, no wasted bits, escape


def encode_uncompressed(pcm: bytes) -> bytes:
    """16-bit little-endian stereo PCM -> one uncompressed ALAC frame."""
    frames = len(pcm) // 4
    samples = array.array("h")
    samples.frombytes(pcm[: frames * 4])
    samples.byteswap()
    raw = samples.tobytes()

    acc = (_HEADER << 32) | frames
    nbits = 55
    acc = (acc << (len(raw) * 8)) | int.from_bytes(raw, "big")
    nbits += len(raw) * 8
    acc = (acc << 3) | 7                  # END element
    nbits += 3
    pad = (-nbits) % 8
    return (acc << pad).to_bytes((nbits + pad) // 8, "big")


def decode_uncompressed(frame: bytes) -> bytes:
    """Inverse of encode_uncompressed (used by tests and the fake speaker)."""
    value = int.from_bytes(frame, "big")
    total = len(frame) * 8
    head = value >> (total - 55)
    frames = head & 0xFFFFFFFF
    if head >> 32 != _HEADER:
        raise ValueError("not an uncompressed stereo ALAC frame")
    nraw = frames * 4
    raw = ((value >> (total - 55 - nraw * 8)) & ((1 << (nraw * 8)) - 1)).to_bytes(nraw, "big")
    samples = array.array("h")
    samples.frombytes(raw)
    samples.byteswap()
    return samples.tobytes()
