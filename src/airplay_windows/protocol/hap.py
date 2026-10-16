"""HomeKit pairing as AirPlay 2 uses it: pair-setup with a password, pair-verify, encrypted framing.

A receiver with "Require Password" on answers every plain request with 403.
It accepts a controller that runs HomeKit pair-setup with the AirPlay
password as the SRP secret, then pair-verify, whose shared secret keys the
encrypted RTSP connection and the audio.
"""

from __future__ import annotations

import hashlib
import os
import socket
import struct
import threading
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from airplay_windows.protocol.rtsp import RtspError

# --- TLV8 ---------------------------------------------------------------------------------

TLV_METHOD = 0x00
TLV_IDENTIFIER = 0x01
TLV_SALT = 0x02
TLV_PUBLIC_KEY = 0x03
TLV_PROOF = 0x04
TLV_ENCRYPTED_DATA = 0x05
TLV_STATE = 0x06
TLV_ERROR = 0x07
TLV_SIGNATURE = 0x0A

TLV_ERRORS = {1: "unknown error", 2: "authentication failed (wrong password?)", 3: "try again later",
              4: "too many pairings", 5: "too many attempts", 6: "unavailable", 7: "busy"}


def tlv_write(items: list[tuple[int, bytes]]) -> bytes:
    """Values longer than 255 bytes are split into consecutive items with the same tag."""
    out = bytearray()
    for tag, value in items:
        if not value:
            out += bytes([tag, 0])
        for i in range(0, len(value), 255):
            chunk = value[i: i + 255]
            out += bytes([tag, len(chunk)]) + chunk
    return bytes(out)


def tlv_read(data: bytes) -> dict[int, bytes]:
    out: dict[int, bytes] = {}
    i, previous = 0, None
    while i + 2 <= len(data):
        tag, length = data[i], data[i + 1]
        value = data[i + 2: i + 2 + length]
        out[tag] = out[tag] + value if tag == previous else value
        previous = tag
        i += 2 + length
    return out


# --- SRP-6a: RFC 5054 3072-bit group, SHA-512, username "Pair-Setup" -------------------------

SRP_N = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
    "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
    "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF05"
    "98DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB"
    "9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718"
    "3995497CEA956AE515D2261898FA051015728E5A8AAAC42DAD33170D04507A33"
    "A85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6BF12FFA06D98A0864"
    "D87602733EC86A64521F2B18177B200CBBE117577A615D6C770988C0BAD946E2"
    "08E24FA074E5AB3143DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF", 16)
SRP_G = 5
SRP_LEN = 384
SRP_USERNAME = "Pair-Setup"


def sha512(*parts: bytes) -> bytes:
    h = hashlib.sha512()
    for part in parts:
        h.update(part)
    return h.digest()


def int_bytes(n: int, length: int | None = None) -> bytes:
    return n.to_bytes(length or max(1, (n.bit_length() + 7) // 8), "big")


class SrpClient:
    def __init__(self, password: str, username: str = SRP_USERNAME, private: int | None = None):
        self.username = username.encode()
        self.password = password.encode()
        self.a = private if private is not None else int.from_bytes(os.urandom(32), "big")
        self.A = pow(SRP_G, self.a, SRP_N)
        self.K = self.M1 = b""

    @property
    def public_key(self) -> bytes:
        return int_bytes(self.A)

    def process(self, salt: bytes, server_public: bytes) -> bytes:
        """Derive the session key K and our proof M1 from the receiver's salt and B."""
        B = int.from_bytes(server_public, "big")
        if B % SRP_N == 0:
            raise ValueError("invalid SRP public key from the receiver")
        k = int.from_bytes(sha512(int_bytes(SRP_N), int_bytes(SRP_G, SRP_LEN)), "big")
        u = int.from_bytes(sha512(int_bytes(self.A, SRP_LEN), int_bytes(B, SRP_LEN)), "big")
        x = int.from_bytes(sha512(salt, sha512(self.username + b":" + self.password)), "big")
        S = pow((B - k * pow(SRP_G, x, SRP_N)) % SRP_N, self.a + u * x, SRP_N)
        self.K = sha512(int_bytes(S))
        hn_xor_hg = bytes(a ^ b for a, b in zip(sha512(int_bytes(SRP_N)), sha512(int_bytes(SRP_G))))
        self.M1 = sha512(hn_xor_hg, sha512(self.username), salt, int_bytes(self.A), int_bytes(B), self.K)
        return self.M1

    def verify_server(self, proof: bytes) -> bool:
        return proof == sha512(int_bytes(self.A), self.M1, self.K)


# --- Keys and ChaCha20-Poly1305 framing ----------------------------------------------------------

def hkdf(salt: str, info: str, secret: bytes) -> bytes:
    return HKDF(hashes.SHA512(), 32, salt.encode(), info.encode()).derive(secret)


def nonce(value: bytes | int) -> bytes:
    """12-byte nonce: 4 zero bytes + an 8-byte label or little-endian counter."""
    return bytes(4) + (value if isinstance(value, bytes) else value.to_bytes(8, "little"))


def raw_public(key) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


class HapChannel:
    """Encrypted framing on a TCP connection: <2-byte LE length><ciphertext + 16-byte tag>.

    At most 1024 plaintext bytes per frame; the length is the AAD; each direction
    counts its own nonces.
    """

    FRAME = 1024

    def __init__(self, sock: socket.socket, output_key: bytes, input_key: bytes):
        self.sock = sock
        self._out, self._in = ChaCha20Poly1305(output_key), ChaCha20Poly1305(input_key)
        self._out_counter = self._in_counter = 0
        self._plain = b""
        self._lock = threading.Lock()

    def write(self, data: bytes) -> None:
        with self._lock:
            frames = []
            for i in range(0, len(data), self.FRAME):
                block = data[i: i + self.FRAME]
                length = struct.pack("<H", len(block))
                frames.append(length + self._out.encrypt(nonce(self._out_counter), block, length))
                self._out_counter += 1
            self.sock.sendall(b"".join(frames))

    def _read_exact(self, size: int) -> bytes:
        buf = b""
        while len(buf) < size:
            chunk = self.sock.recv(size - len(buf))
            if not chunk:
                return b""
            buf += chunk
        return buf

    def read(self, size: int) -> bytes:
        if not self._plain:
            length = self._read_exact(2)
            if not length:
                return b""
            block = self._read_exact(struct.unpack("<H", length)[0] + 16)
            if not block:
                return b""
            self._plain = self._in.decrypt(nonce(self._in_counter), block, length)
            self._in_counter += 1
        out, self._plain = self._plain[:size], self._plain[size:]
        return out


# --- Pairing --------------------------------------------------------------------------------------

class PairingError(RtspError):
    pass


def _check(response: dict[int, bytes], step: str) -> dict[int, bytes]:
    if TLV_ERROR in response:
        code = response[TLV_ERROR][0]
        raise PairingError(f"{step}: the receiver reported {TLV_ERRORS.get(code, code)}")
    return response


class HapPairing:
    """pair-setup with the AirPlay password, then pair-verify. Nothing is stored between sessions."""

    def __init__(self, post):
        """`post(path, body) -> response body` sends one plaintext pairing request."""
        self.post = post
        self.pairing_id = str(uuid.uuid4()).upper().encode()
        self.signing_key = Ed25519PrivateKey.generate()
        self.device_id = b""
        self.device_ltpk: Ed25519PublicKey | None = None

    def pair_setup(self, password: str) -> None:
        m2 = _check(tlv_read(self.post("/pair-setup", tlv_write(
            [(TLV_METHOD, b"\x00"), (TLV_STATE, b"\x01")]))), "pair-setup M2")
        srp = SrpClient(password)
        proof = srp.process(m2[TLV_SALT], m2[TLV_PUBLIC_KEY])
        m4 = _check(tlv_read(self.post("/pair-setup", tlv_write(
            [(TLV_STATE, b"\x03"), (TLV_PUBLIC_KEY, srp.public_key), (TLV_PROOF, proof)]))), "pair-setup M4")
        if not srp.verify_server(m4.get(TLV_PROOF, b"")):
            raise PairingError("pair-setup M4: the receiver's proof did not match (wrong password?)")

        # M5/M6: exchange long-term Ed25519 identities, encrypted with a key derived from K.
        controller_x = hkdf("Pair-Setup-Controller-Sign-Salt", "Pair-Setup-Controller-Sign-Info", srp.K)
        setup_key = ChaCha20Poly1305(hkdf("Pair-Setup-Encrypt-Salt", "Pair-Setup-Encrypt-Info", srp.K))
        public = raw_public(self.signing_key.public_key())
        signature = self.signing_key.sign(controller_x + self.pairing_id + public)
        sub_tlv = tlv_write([(TLV_IDENTIFIER, self.pairing_id), (TLV_PUBLIC_KEY, public), (TLV_SIGNATURE, signature)])
        m6 = _check(tlv_read(self.post("/pair-setup", tlv_write(
            [(TLV_STATE, b"\x05"), (TLV_ENCRYPTED_DATA, setup_key.encrypt(nonce(b"PS-Msg05"), sub_tlv, None))]))),
            "pair-setup M6")
        device = tlv_read(setup_key.decrypt(nonce(b"PS-Msg06"), m6[TLV_ENCRYPTED_DATA], None))
        self.device_id = device[TLV_IDENTIFIER]
        self.device_ltpk = Ed25519PublicKey.from_public_bytes(device[TLV_PUBLIC_KEY])
        accessory_x = hkdf("Pair-Setup-Accessory-Sign-Salt", "Pair-Setup-Accessory-Sign-Info", srp.K)
        try:
            self.device_ltpk.verify(device[TLV_SIGNATURE], accessory_x + self.device_id + device[TLV_PUBLIC_KEY])
        except InvalidSignature as exc:
            raise PairingError("pair-setup M6: the receiver's signature is invalid") from exc

    def pair_verify(self) -> bytes:
        """Returns the X25519 shared secret that the session keys derive from."""
        ephemeral = X25519PrivateKey.generate()
        our_public = raw_public(ephemeral.public_key())
        m2 = _check(tlv_read(self.post("/pair-verify", tlv_write(
            [(TLV_STATE, b"\x01"), (TLV_PUBLIC_KEY, our_public)]))), "pair-verify M2")
        device_public = m2[TLV_PUBLIC_KEY]
        shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(device_public))
        verify_key = ChaCha20Poly1305(hkdf("Pair-Verify-Encrypt-Salt", "Pair-Verify-Encrypt-Info", shared))
        device = tlv_read(verify_key.decrypt(nonce(b"PV-Msg02"), m2[TLV_ENCRYPTED_DATA], None))
        if device.get(TLV_IDENTIFIER) != self.device_id:
            raise PairingError("pair-verify: the receiver's identity changed since pair-setup")
        try:
            self.device_ltpk.verify(device[TLV_SIGNATURE], device_public + self.device_id + our_public)
        except InvalidSignature as exc:
            raise PairingError("pair-verify: the receiver's signature is invalid") from exc
        signature = self.signing_key.sign(our_public + self.pairing_id + device_public)
        sub_tlv = tlv_write([(TLV_IDENTIFIER, self.pairing_id), (TLV_SIGNATURE, signature)])
        _check(tlv_read(self.post("/pair-verify", tlv_write(
            [(TLV_STATE, b"\x03"), (TLV_ENCRYPTED_DATA, verify_key.encrypt(nonce(b"PV-Msg03"), sub_tlv, None))]))),
            "pair-verify M4")
        return shared
