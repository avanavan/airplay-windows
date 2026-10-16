"""AirPlay 2 password pairing and encrypted audio."""

import array
import os
import socket

import pytest

from airplay_windows.protocol import Receiver, hap
from airplay_windows.protocol.airplay2 import AirPlay2Session, decrypt_audio_packet
from fake_speaker import FakeHomeKitAccessory


def test_tlv_round_trip_splits_long_values():
    value = os.urandom(600)
    encoded = hap.tlv_write([(hap.TLV_STATE, b"\x03"), (hap.TLV_PUBLIC_KEY, value)])
    assert encoded.count(bytes([hap.TLV_PUBLIC_KEY, 255])) == 2
    assert hap.tlv_read(encoded) == {hap.TLV_STATE: b"\x03", hap.TLV_PUBLIC_KEY: value}


def test_password_pairing():
    accessory = FakeHomeKitAccessory("correct horse")
    pairing = hap.HapPairing(accessory.post)
    pairing.pair_setup("correct horse")
    assert pairing.device_id == accessory.device_id
    assert pairing.pair_verify() == accessory.shared


def test_wrong_password_is_reported():
    pairing = hap.HapPairing(FakeHomeKitAccessory("right").post)
    with pytest.raises(hap.PairingError, match="wrong password"):
        pairing.pair_setup("wrong")


def test_hap_channel_round_trip_across_frames():
    a, b = socket.socketpair()
    k1, k2 = os.urandom(32), os.urandom(32)
    sender, receiver = hap.HapChannel(a, k1, k2), hap.HapChannel(b, k2, k1)
    message = os.urandom(3000)   # three frames
    sender.write(message)
    got = b""
    while len(got) < len(message):
        got += receiver.read(4096)
    assert got == message
    a.close(), b.close()


def test_audio_packets_round_trip():
    session = AirPlay2Session(Receiver("X@Bedroom", "127.0.0.1", 7000, {"sf": "0x80484"}), password="x")
    pcm = array.array("h", range(-704, 704)).tobytes()
    first = session.build_audio_packet(7, 1000, pcm, first=True)
    second = session.build_audio_packet(8, 1352, pcm, first=False)
    assert first[:2] == b"\x80\xe0" and second[:2] == b"\x80\x60"
    assert first[-8:] != second[-8:]   # the nonce counter advances
    assert decrypt_audio_packet(second, session.audio_key) == pcm
    assert session.wrap_resend(second)[:4] == b"\x80\xd6" + second[2:4]


def test_latency_is_kept_within_what_setup_offers():
    receiver = Receiver("X@Bedroom", "127.0.0.1", 7000, {"sf": "0x80484"})
    assert AirPlay2Session(receiver, "x", latency_ms=100).latency_samples == 11025
    assert AirPlay2Session(receiver, "x", latency_ms=5000).latency_samples == 88200
