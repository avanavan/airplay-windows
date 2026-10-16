"""Fake receivers for tests and manual experiments.

FakeSpeaker answers the AirPlay 1 handshake, decodes (and, with a test RSA
key pair, decrypts) the audio it receives, and requests retransmissions like
a real receiver. FakeHomeKitAccessory is the accessory side of password
pair-setup and pair-verify.

Run a fake speaker by hand:  python tests/fake_speaker.py --port 7123 --profile airport
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import socket
import struct
import threading
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from airplay_windows.protocol import alac, hap, raop, rtp
from airplay_windows.protocol.rtsp import digest_response, transport_param

# TXT records shaped like real receivers'.
PROFILES = {
    "homepod": {"et": "0,4", "cn": "0,1,2,3", "ch": "2", "sr": "44100", "ss": "16", "tp": "UDP",
                "am": "AudioAccessory5,1", "pw": "false", "sf": "0x4", "vs": "770.8.1", "da": "true"},
    "airport": {"et": "0,1", "cn": "0,1", "ch": "2", "sr": "44100", "ss": "16", "tp": "UDP",
                "am": "AirPort10,115", "pw": "false", "vs": "130.14"},
    "plain": {"et": "0", "cn": "0,1", "ch": "2", "sr": "44100", "ss": "16", "tp": "UDP",
              "am": "FakeSpeaker1,1", "pw": "false", "vs": "130.14"},
}

TEST_RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _unb64(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4))


class FakeSpeaker:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, password: str | None = None,
                 audio_latency: int = 11025, rsa_key=TEST_RSA_KEY, log=lambda *_: None):
        self.password = password
        self.audio_latency = audio_latency
        self.rsa_key = rsa_key
        self.log = log
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((host, port))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []
        self.audio_packets: dict[int, bytes] = {}   # seq -> decoded PCM
        self.resent_seqs: list[int] = []
        self.sync_packets = 0
        self.timing_replies = 0
        self.volume_db: float | None = None
        self.aes_key = self.aes_iv = None
        self.client_control: tuple[str, int] | None = None
        self.client_timing: tuple[str, int] | None = None
        self.nonce = hashlib.md5(str(time.time()).encode()).hexdigest()
        self.done = threading.Event()
        self._stop = threading.Event()
        self._udp: list[socket.socket] = []

    @property
    def encrypted(self) -> bool:
        return self.aes_key is not None

    def serve_once(self) -> None:
        conn, peer = self.server.accept()
        conn.settimeout(30)
        try:
            while not self._stop.is_set():
                request = self._read_request(conn)
                if request is None:
                    break
                method, uri, headers, body = request
                self.requests.append((method, uri, headers, body))
                self.log(f"[fake] {method} {uri}")
                status, extra, resp_body = self._handle(method, headers, body, peer[0])
                lines = [f"RTSP/1.0 {status}", f"CSeq: {headers.get('cseq', '0')}", "Server: AirTunes/366.0",
                         *[f"{k}: {v}" for k, v in extra]]
                if resp_body:
                    lines.append(f"Content-Length: {len(resp_body)}")
                conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + resp_body)
                if method == "TEARDOWN":
                    break
        except (OSError, ValueError):
            pass
        finally:
            conn.close()
            self.done.set()

    def _read_request(self, conn):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return None
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        method, uri, _ = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length", "0"))
        while len(rest) < length:
            rest += conn.recv(length - len(rest))
        return method, uri, headers, rest[:length]

    def _authorized(self, method: str, headers: dict[str, str]) -> bool:
        if not self.password:
            return True
        auth = headers.get("authorization", "")
        params = {p.split("=", 1)[0].strip(): p.split("=", 1)[1].strip().strip('"')
                  for p in auth.replace("Digest ", "").split(",") if "=" in p}
        expected = {digest_response("iTunes", self.password, "raop", self.nonce, method, params.get("uri", ""), upper)
                    for upper in (False, True)}
        return params.get("response", "") in expected

    def _handle(self, method, headers, body, client_ip):
        if method == "POST":
            return "200 OK", [("Content-Type", "application/octet-stream")], bytes(32) + b"fake-cert"
        if not self._authorized(method, headers):
            return "401 Unauthorized", [("WWW-Authenticate", f'Digest realm="raop", nonce="{self.nonce}"')], b""
        if method == "OPTIONS":
            extra = [("Public", "ANNOUNCE, SETUP, RECORD, PAUSE, FLUSH, TEARDOWN, OPTIONS, GET_PARAMETER, "
                                "SET_PARAMETER, POST")]
            if "apple-challenge" in headers:
                extra.append(("Apple-Response", base64.b64encode(bytes(256)).decode()))
            return "200 OK", extra, b""
        if method == "ANNOUNCE":
            attrs = dict(line[2:].split(":", 1) for line in body.decode().split("\r\n") if line.startswith("a=")
                         and ":" in line)
            if "rsaaeskey" in attrs:
                self.aes_key = self.rsa_key.decrypt(_unb64(attrs["rsaaeskey"]), padding.OAEP(
                    mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None))
                self.aes_iv = _unb64(attrs["aesiv"])
            return "200 OK", [], b""
        if method == "SETUP":
            transport = headers.get("transport", "")
            self.client_control = (client_ip, transport_param(transport, "control_port"))
            self.client_timing = (client_ip, transport_param(transport, "timing_port"))
            audio, control, timing = (self._udp_socket() for _ in range(3))
            for loop, sock in ((self._audio_loop, audio), (self._control_loop, control), (self._timing_loop, timing)):
                threading.Thread(target=loop, args=(sock,), daemon=True).start()
            ports = [s.getsockname()[1] for s in (audio, control, timing)]
            return "200 OK", [("Session", "DEADBEEF"), ("Audio-Latency", str(self.audio_latency)),
                              ("Transport", "RTP/AVP/UDP;unicast;mode=record;"
                                            f"server_port={ports[0]};control_port={ports[1]};timing_port={ports[2]}")], b""
        if method == "RECORD":
            return "200 OK", [("Audio-Latency", str(self.audio_latency))], b""
        if method == "SET_PARAMETER":
            text = body.decode(errors="ignore")
            if text.startswith("volume:"):
                self.volume_db = float(text.split(":", 1)[1])
        return "200 OK", [], b""

    def _udp_socket(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("0.0.0.0", 0))
        s.settimeout(0.5)
        self._udp.append(s)
        return s

    def _store_audio(self, packet: bytes) -> int:
        seq = struct.unpack(">H", packet[2:4])[0]
        payload = packet[12:]
        if self.encrypted:
            payload = raop.aes_decrypt_payload(payload, self.aes_key, self.aes_iv)
        self.audio_packets[seq] = alac.decode_uncompressed(payload)
        return seq

    def _audio_loop(self, sock: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                data, _ = sock.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            if data[1] & 0x7F == rtp.PT_AUDIO:
                self._store_audio(data)

    def _control_loop(self, sock: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                data, _ = sock.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            kind = data[1] & 0x7F
            if kind == rtp.PT_SYNC:
                self.sync_packets += 1
            elif kind == rtp.PT_RESEND_RESPONSE:
                self.resent_seqs.append(self._store_audio(data[4:]))

    def _timing_loop(self, sock: socket.socket) -> None:
        seq = 0
        while not self._stop.is_set():   # poll the sender's clock every second, like a receiver
            if self.client_timing and self.client_timing[1]:
                seq += 1
                request = rtp.header(rtp.PT_TIMING_REQUEST, seq, True) + bytes(20) + struct.pack(">II", *rtp.ntp_now())
                try:
                    sock.sendto(request, self.client_timing)
                    reply, _ = sock.recvfrom(64)
                    if len(reply) == 32 and reply[1] & 0x7F == rtp.PT_TIMING_RESPONSE:
                        self.timing_replies += 1
                except (socket.timeout, OSError):
                    pass
            self._stop.wait(1.0)

    def request_resend(self, seq: int, count: int = 1) -> None:
        self._udp[1].sendto(rtp.header(rtp.PT_RESEND_REQUEST, 1, True) + struct.pack(">HH", seq, count),
                            self.client_control)

    def pcm(self) -> bytes:
        return b"".join(self.audio_packets[s] for s in sorted(self.audio_packets))

    def close(self) -> None:
        self._stop.set()
        self.server.close()
        for s in self._udp:
            s.close()


class FakeHomeKitAccessory:
    """Accessory side of pair-setup/pair-verify, using the SRP-6a server equations."""

    def __init__(self, password: str):
        N, g, h, ib = hap.SRP_N, hap.SRP_G, hap.sha512, hap.int_bytes
        self.salt = os.urandom(16)
        x = int.from_bytes(h(self.salt, h(b"Pair-Setup:" + password.encode())), "big")
        self.v = pow(g, x, N)
        self.b = int.from_bytes(os.urandom(32), "big")
        k = int.from_bytes(h(ib(N), ib(g, hap.SRP_LEN)), "big")
        self.B = (k * self.v + pow(g, self.b, N)) % N
        self.identity = Ed25519PrivateKey.generate()
        self.device_id = b"EA:C4:C5:27:50:84"
        self.shared = b""

    def post(self, path: str, body: bytes) -> bytes:
        H = hap
        req = H.tlv_read(body)
        state = req[H.TLV_STATE][0]
        if path == "/pair-setup" and state == 1:
            return H.tlv_write([(H.TLV_STATE, b"\x02"), (H.TLV_SALT, self.salt), (H.TLV_PUBLIC_KEY, H.int_bytes(self.B))])
        if path == "/pair-setup" and state == 3:
            N, h, ib = H.SRP_N, H.sha512, H.int_bytes
            A = int.from_bytes(req[H.TLV_PUBLIC_KEY], "big")
            u = int.from_bytes(h(ib(A, H.SRP_LEN), ib(self.B, H.SRP_LEN)), "big")
            self.K = h(ib(pow(A * pow(self.v, u, N), self.b, N)))
            hx = bytes(a ^ b for a, b in zip(h(ib(N)), h(ib(H.SRP_G))))
            m1 = h(hx, h(b"Pair-Setup"), self.salt, ib(A), ib(self.B), self.K)
            if req[H.TLV_PROOF] != m1:
                return H.tlv_write([(H.TLV_STATE, b"\x04"), (H.TLV_ERROR, b"\x02")])
            return H.tlv_write([(H.TLV_STATE, b"\x04"), (H.TLV_PROOF, h(ib(A), m1, self.K))])
        if path == "/pair-setup" and state == 5:
            key = ChaCha20Poly1305(H.hkdf("Pair-Setup-Encrypt-Salt", "Pair-Setup-Encrypt-Info", self.K))
            sub = H.tlv_read(key.decrypt(H.nonce(b"PS-Msg05"), req[H.TLV_ENCRYPTED_DATA], None))
            self.controller_id, self.controller_ltpk = sub[H.TLV_IDENTIFIER], sub[H.TLV_PUBLIC_KEY]
            ltpk = H.raw_public(self.identity.public_key())
            x = H.hkdf("Pair-Setup-Accessory-Sign-Salt", "Pair-Setup-Accessory-Sign-Info", self.K)
            reply = H.tlv_write([(H.TLV_IDENTIFIER, self.device_id), (H.TLV_PUBLIC_KEY, ltpk),
                                 (H.TLV_SIGNATURE, self.identity.sign(x + self.device_id + ltpk))])
            return H.tlv_write([(H.TLV_STATE, b"\x06"),
                                (H.TLV_ENCRYPTED_DATA, key.encrypt(H.nonce(b"PS-Msg06"), reply, None))])
        if path == "/pair-verify" and state == 1:
            eph = X25519PrivateKey.generate()
            self.eph_public, self.peer_public = H.raw_public(eph.public_key()), req[H.TLV_PUBLIC_KEY]
            self.shared = eph.exchange(X25519PublicKey.from_public_bytes(self.peer_public))
            self.verify_key = ChaCha20Poly1305(H.hkdf("Pair-Verify-Encrypt-Salt", "Pair-Verify-Encrypt-Info",
                                                      self.shared))
            sig = self.identity.sign(self.eph_public + self.device_id + self.peer_public)
            sub = H.tlv_write([(H.TLV_IDENTIFIER, self.device_id), (H.TLV_SIGNATURE, sig)])
            return H.tlv_write([(H.TLV_STATE, b"\x02"), (H.TLV_PUBLIC_KEY, self.eph_public),
                                (H.TLV_ENCRYPTED_DATA, self.verify_key.encrypt(H.nonce(b"PV-Msg02"), sub, None))])
        if path == "/pair-verify" and state == 3:
            sub = H.tlv_read(self.verify_key.decrypt(H.nonce(b"PV-Msg03"), req[H.TLV_ENCRYPTED_DATA], None))
            Ed25519PublicKey.from_public_bytes(self.controller_ltpk).verify(
                sub[H.TLV_SIGNATURE], self.peer_public + self.controller_id + self.eph_public)
            return H.tlv_write([(H.TLV_STATE, b"\x04")])
        raise AssertionError(f"unexpected {path} state {state}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=7123)
    parser.add_argument("--password")
    args = parser.parse_args()
    speaker = FakeSpeaker("0.0.0.0", args.port, password=args.password, log=print)
    print(f"Fake AirPlay speaker on port {speaker.port} (Ctrl+C to stop)")
    try:
        while True:
            speaker.serve_once()
            print(f"[fake] session ended: {len(speaker.audio_packets)} audio packets")
    except KeyboardInterrupt:
        speaker.close()


if __name__ == "__main__":
    main()
