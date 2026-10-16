"""AirPlay 1: packet formats, TXT-record decisions, and full sessions against a fake speaker."""

import array
import base64
import threading
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from airplay_windows.audio import sources
from airplay_windows.protocol import AirPlay2Session, RaopSession, Receiver, RtspError, alac, open_session, raop, rtp
from fake_speaker import PROFILES, TEST_RSA_KEY, FakeSpeaker


def test_alac_round_trip():
    pcm = array.array("h", range(-704, 704)).tobytes()   # 352 stereo frames
    frame = alac.encode_uncompressed(pcm)
    assert frame[:3] == bytes([0x20, 0x00, 0x12])
    assert alac.decode_uncompressed(frame) == pcm


def test_aes_leaves_partial_block_clear():
    key, iv, payload = bytes(range(16)), bytes(range(16, 32)), bytes(range(37))
    encrypted = raop.aes_encrypt_payload(payload, key, iv)
    assert encrypted[32:] == payload[32:] and encrypted[:32] != payload[:32]
    assert raop.aes_decrypt_payload(encrypted, key, iv) == payload


def test_aes_key_is_wrapped_with_the_airport_public_key():
    assert RaopSession.__init__ and raop.AIRPORT_RSA_MODULUS.bit_length() == 2048
    wrapped = raop.wrap_aes_key(bytes(16))
    assert len(wrapped) == 256
    assert raop.wrap_aes_key(bytes(16)) != wrapped                      # OAEP is randomised
    oaep = padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None)
    assert TEST_RSA_KEY.decrypt(raop.wrap_aes_key(b"k" * 16, TEST_RSA_KEY.public_key()), oaep) == b"k" * 16


@pytest.mark.parametrize("txt, rsa, auth_setup, password_pairing, pin", [
    (PROFILES["homepod"], False, True, False, False),
    (PROFILES["airport"], True, False, False, False),
    (PROFILES["plain"], False, False, False, False),
    ({"et": "0,3,5", "sf": "0x80484"}, False, False, True, False),      # HomePod with Require Password
    ({"et": "0", "sf": "0x204"}, False, False, False, True),            # Apple TV asking for a PIN
])
def test_txt_record_decisions(txt, rsa, auth_setup, password_pairing, pin):
    r = Receiver("AABBCCDDEEFF@Kitchen", "10.0.0.2", 7000, txt)
    assert (r.rsa_encryption, r.needs_auth_setup, r.requires_password_pairing, r.requires_pin_pairing) == \
        (rsa, auth_setup, password_pairing, pin)
    assert r.name == "Kitchen" and r.device_id == "AABBCCDDEEFF" and r.key == "AABBCCDDEEFF"


def test_receiver_identity_and_models():
    manual = Receiver.manual("10.0.0.9")
    assert manual.key == "10.0.0.9" and manual.needs_auth_setup
    assert not Receiver.manual("10.0.0.9", encrypt=True).needs_auth_setup
    assert Receiver("x@y", "h", 1, {"am": "AudioAccessory5,1"}).model_name == "HomePod mini"
    assert Receiver("x@y", "h", 1, {"am": "AirPort10,115"}).model_name == "AirPort Express"


def test_session_choice():
    homepod = Receiver("EAC4C5275084@Bedroom", "10.0.0.5", 7000, {"sf": "0x80484"})
    assert isinstance(open_session(homepod, "pw"), AirPlay2Session)
    assert isinstance(open_session(homepod, "pw", airplay2=False), RaopSession)
    assert isinstance(open_session(Receiver("a@b", "h", 1, PROFILES["airport"])), RaopSession)


def test_volume_curve():
    assert rtp.percent_to_db(0) == -144.0
    assert rtp.percent_to_db(100) == -0.001
    assert rtp.percent_to_db(35) == pytest.approx(-19.5)


def padded(chunks):
    """The streamer zero-pads the last chunk to a full packet."""
    pcm = b"".join(chunks)
    return pcm + bytes(-len(pcm) % rtp.PCM_BYTES_PER_PACKET)


def run_session(profile, seconds=1.5, password=None, speaker_password=None):
    speaker = FakeSpeaker(password=speaker_password)
    threading.Thread(target=speaker.serve_once, daemon=True).start()
    receiver = Receiver("AABBCCDDEEFF@Fake", "127.0.0.1", speaker.port, dict(PROFILES[profile]))
    session = RaopSession(receiver, password=password, rsa_public_key=TEST_RSA_KEY.public_key())
    source = list(sources.tone(440, seconds))
    try:
        session.connect(volume_percent=35)

        def lose_a_packet():
            target = (session.streamer.first_seq + 20) & 0xFFFF
            while target not in speaker.audio_packets:
                time.sleep(0.01)
            speaker.request_resend(target)

        threading.Thread(target=lose_a_packet, daemon=True).start()
        sent = session.stream(iter(source))
        time.sleep(0.3)
    finally:
        session.close()
        speaker.done.wait(5)
        speaker.close()
    return speaker, session, source, sent


def test_mfi_receiver_end_to_end():
    speaker, session, source, sent = run_session("homepod")
    methods = [m for m, _, _, _ in speaker.requests]
    assert methods == ["POST", "OPTIONS", "ANNOUNCE", "SETUP", "RECORD", "SET_PARAMETER", "SET_PARAMETER", "TEARDOWN"]
    auth_setup = speaker.requests[0][3]
    assert speaker.requests[0][1] == "/auth-setup" and len(auth_setup) == 33 and auth_setup[0] == 1
    assert "apple-challenge" not in speaker.requests[1][2]
    assert not speaker.encrypted
    assert sent == len(source) and len(speaker.audio_packets) == sent
    assert speaker.pcm() == padded(source)
    assert speaker.resent_seqs, "the resend request was not served"
    assert speaker.sync_packets >= 1 and speaker.timing_replies >= 1
    assert speaker.volume_db == pytest.approx(-19.5)
    assert session.latency_samples == 88200   # max(2 s buffer, the speaker's 11025)


def test_rsa_receiver_gets_a_fresh_session_key():
    speaker, session, source, _ = run_session("airport", seconds=0.5)
    assert [m for m, _, _, _ in speaker.requests][0] == "OPTIONS"     # no auth-setup without MFi
    challenge = speaker.requests[0][2]["apple-challenge"]
    assert len(base64.b64decode(challenge + "==")) == 16
    sdp = speaker.requests[1][3].decode()
    assert "a=rtpmap:96 AppleLossless" in sdp and "a=rsaaeskey:" in sdp
    assert speaker.aes_key == session.aes_key and speaker.aes_iv == session.aes_iv
    assert speaker.pcm() == padded(source)
    # Another session uses another key.
    assert RaopSession(Receiver("a@b", "h", 1, PROFILES["airport"])).aes_key != session.aes_key


def test_password_protected_receiver():
    speaker, _, source, _ = run_session("plain", seconds=0.3, password="secret", speaker_password="secret")
    assert speaker.pcm() == padded(source)
    assert "authorization" in speaker.requests[-1][2]


def test_missing_password_is_reported():
    with pytest.raises(RtspError) as err:
        run_session("plain", seconds=0.1, speaker_password="secret")
    assert err.value.status == 401


def test_keepalive_is_a_bare_options():
    speaker = FakeSpeaker()
    threading.Thread(target=speaker.serve_once, daemon=True).start()
    session = RaopSession(Receiver("AABBCCDDEEFF@Fake", "127.0.0.1", speaker.port, dict(PROFILES["airport"])),
                          rsa_public_key=TEST_RSA_KEY.public_key())
    try:
        session.connect()
        session.keepalive()
        method, uri, headers, _ = speaker.requests[-1]
        assert (method, uri) == ("OPTIONS", "*") and "apple-challenge" not in headers
    finally:
        session.close()
        speaker.done.wait(5)
        speaker.close()
