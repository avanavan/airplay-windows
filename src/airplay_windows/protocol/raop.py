"""AirPlay 1 (RAOP / AirTunes v2): ALAC over RTP, optionally AES-encrypted.

Works with AirPort Express, most third-party receivers, Apple TVs and
HomePods that don't require a password.
"""

from __future__ import annotations

import base64
import os
import random
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from airplay_windows.protocol import alac, rtp
from airplay_windows.protocol.rtsp import transport_param
from airplay_windows.protocol.session import Session, Streamer

# The AirPort Express public key. Receivers that list RSA ("et" contains 1)
# decrypt the per-session AES key with the matching private key.
AIRPORT_RSA_MODULUS = int.from_bytes(base64.b64decode(
    "59dE8qLieItsH1WgjrcFRKj6eUWqi+bGLOX1HL3U3GhC/j0Qg90u3sG/1CUtwC5vOYvfDmFI6oSFXi5ELabWJmT2dKHzBJKa3k9ok+8t"
    "9ucRqMd6DZHJ2YCCLlDRKSKv6kDqnw4UwPdpOMXziC/AMj3Z/lUVX1G7WSHCAWKf1zNS1eLvqr+boEjXuBOitnZ/bDzPHrTOZz0Dew0u"
    "owxf/+sG+NCK3eQJVxqcaJ/vEHKIVd2M+5qL71yJQ+87X6oV3eaYvt3zWZYD6z5vYTcrtij2VZ9Zmni/UAaHqn9JdsBWLUEpVviYnhim"
    "NVvYFZeCXg/IdTQ+x4IRdiXNv5hEew=="), "big")
AIRPORT_RSA_EXPONENT = 65537


def _b64(data: bytes) -> str:
    """Base64 without padding, as AirPlay senders write SDP and challenge values."""
    return base64.b64encode(data).decode("ascii").rstrip("=")


def wrap_aes_key(aes_key: bytes, public_key: rsa.RSAPublicKey | None = None) -> bytes:
    """RSA-OAEP (SHA-1) encryption of the session key, for the ANNOUNCE "rsaaeskey" attribute."""
    key = public_key or rsa.RSAPublicNumbers(AIRPORT_RSA_EXPONENT, AIRPORT_RSA_MODULUS).public_key()
    return key.encrypt(aes_key, padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None))


def aes_encrypt_payload(payload: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC over the whole 16-byte blocks; the tail stays clear. The IV restarts per packet."""
    whole = len(payload) - len(payload) % 16
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(payload[:whole]) + encryptor.finalize() + payload[whole:]


def aes_decrypt_payload(payload: bytes, key: bytes, iv: bytes) -> bytes:
    whole = len(payload) - len(payload) % 16
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return decryptor.update(payload[:whole]) + decryptor.finalize() + payload[whole:]


def dmap_item(tag: str, value: bytes) -> bytes:
    return tag.encode("ascii") + struct.pack(">I", len(value)) + value


def now_playing(title: str, artist: str, album: str = "") -> bytes:
    """DMAP listing item shown on receivers with a display."""
    return dmap_item("mlit", dmap_item("minm", title.encode()) + dmap_item("asar", artist.encode())
                     + dmap_item("asal", album.encode()))


class RaopSession(Session):
    keepalive_interval = 15.0      # a bare OPTIONS keeps the receiver from timing out

    def __init__(self, receiver, password=None, latency_ms: int = 2000, log=None, rsa_public_key=None):
        super().__init__(receiver, password=password, latency_ms=latency_ms, log=log)
        self.encrypt = receiver.rsa_encryption
        self.aes_key, self.aes_iv = os.urandom(16), os.urandom(16)
        self._rsa_public_key = rsa_public_key   # tests substitute their own key pair
        self.resource_id = str(random.randint(10**9, 2**32 - 1))
        self.audio_latency = 0

    def connect(self, volume_percent: float = 35) -> None:
        r = self.receiver
        self.log.info("Connecting to %s at %s:%s (AirPlay 1, %s)", r.name, r.host, r.port,
                      "RSA/AES" if self.encrypt else "unencrypted")
        if r.requires_pin_pairing:
            self.log.warning("%s asks for PIN pairing, which isn't supported; trying without it", r.name)
        self.rtsp.open()

        if r.needs_auth_setup:
            # MFi receivers want a Curve25519 key before anything else; the reply isn't needed.
            public = X25519PrivateKey.generate().public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            self.rtsp.send("POST", "/auth-setup", [("Content-Type", "application/octet-stream")], b"\x01" + public)

        challenge = [("Apple-Challenge", _b64(os.urandom(16)))] if self.encrypt else []
        self.rtsp.request("OPTIONS", "*", challenge)

        self.uri = f"rtsp://{self.rtsp.local_ip}/{self.resource_id}"
        self.rtsp.request("ANNOUNCE", self.uri, [("Content-Type", "application/sdp")], self._sdp().encode("ascii"))

        self.streamer = Streamer(self)
        transport = (f"RTP/AVP/UDP;unicast;interleaved=0-1;mode=record;"
                     f"control_port={self.streamer.control_port};timing_port={self.streamer.timing_port}")
        response = self.rtsp.request("SETUP", self.uri, [("Transport", transport)])
        self.session_id = response.header("Session") or ""
        server = response.header("Transport") or ""
        self.audio_ep = (r.host, transport_param(server, "server_port") or 6000)
        control = transport_param(server, "control_port")
        self.control_ep = (r.host, control) if control else None
        self._apply_receiver_latency(response.header("Audio-Latency"))

        seq, rtptime = self.streamer.first_seq, self.streamer.first_timestamp
        response = self.rtsp.request("RECORD", self.uri, [("Session", self.session_id), ("Range", "npt=0-"),
                                                          ("RTP-Info", f"seq={seq};rtptime={rtptime}")])
        self._apply_receiver_latency(response.header("Audio-Latency"))

        self.set_volume(volume_percent)
        self.rtsp.request("SET_PARAMETER", self.uri, [("RTP-Info", f"rtptime={rtptime}"),
                                                      ("Content-Type", "application/x-dmap-tagged")],
                          now_playing("System audio", "airplay-windows"))
        self.log.info("Connected: audio=%s control=%s latency=%d samples", self.audio_ep, self.control_ep,
                      self.latency_samples)

    def _apply_receiver_latency(self, value: str | None) -> None:
        """Buffer at least as much as the receiver says it needs."""
        if value and value.isdigit():
            self.audio_latency = int(value)
            self.latency_samples = max(self.latency_samples, self.audio_latency)

    def _sdp(self) -> str:
        host = self.receiver.host
        lines = ["v=0", f"o=iTunes {self.resource_id} 0 IN IP4 {host}", "s=iTunes", f"c=IN IP4 {host}", "t=0 0",
                 "m=audio 0 RTP/AVP 96", "a=rtpmap:96 AppleLossless", f"a=fmtp:{alac.FMTP}"]
        if self.encrypt:
            lines += [f"a=rsaaeskey:{_b64(wrap_aes_key(self.aes_key, self._rsa_public_key))}",
                      f"a=aesiv:{_b64(self.aes_iv)}"]
        return "\r\n".join(lines) + "\r\n\r\n"

    def set_volume(self, percent: float) -> None:
        self.rtsp.request("SET_PARAMETER", self.uri, [("Session", self.session_id), ("Content-Type", "text/parameters")],
                          f"volume: {rtp.percent_to_db(percent):.6f}\r\n".encode("ascii"))

    def keepalive(self) -> None:
        self.rtsp.request("OPTIONS", "*")

    def build_audio_packet(self, seq: int, timestamp: int, pcm: bytes, first: bool) -> bytes:
        payload = alac.encode_uncompressed(pcm)
        if self.encrypt:
            payload = aes_encrypt_payload(payload, self.aes_key, self.aes_iv)
        return rtp.audio_packet(seq, timestamp, self.streamer.ssrc, payload)
