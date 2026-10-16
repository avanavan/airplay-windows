"""AirPlay 2 for receivers with "Require Password" on (e.g. HomePod, HomePod mini).

After HomeKit pairing with the password (hap.py) the RTSP connection is
encrypted, the stream is set up with binary plists, and audio is 16-bit PCM
sealed with ChaCha20-Poly1305. The UDP side (sync, timing, resends) is the
same Streamer as AirPlay 1.
"""

from __future__ import annotations

import array
import os
import plistlib
import random
import socket
import struct
import threading
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from airplay_windows.protocol import rtp
from airplay_windows.protocol.hap import HapChannel, HapPairing, PairingError, hkdf, nonce
from airplay_windows.protocol.session import Session, Streamer

STREAM_REALTIME = 0x60
AUDIO_FORMAT_PCM_44100_16_2 = 0x800
COMPRESSION_PCM = 1


class EventChannel(threading.Thread):
    """The receiver's event connection (SETUP's eventPort). Every request gets 200 OK."""

    def __init__(self, host: str, port: int, shared: bytes):
        super().__init__(daemon=True)
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(None)
        # The receiver writes with its "write" key, so ours are swapped.
        self.channel = HapChannel(self.sock, hkdf("Events-Salt", "Events-Read-Encryption-Key", shared),
                                  hkdf("Events-Salt", "Events-Write-Encryption-Key", shared))

    def run(self) -> None:
        buf = b""
        try:
            while True:
                chunk = self.channel.read(4096)
                if not chunk:
                    return
                buf += chunk
                while b"\r\n\r\n" in buf:
                    head, _, rest = buf.partition(b"\r\n\r\n")
                    lines = head.decode("latin-1").split("\r\n")
                    headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
                    length = int(headers.get("content-length", "0") or 0)
                    if len(rest) < length:
                        break
                    buf = rest[length:]
                    protocol = lines[0].rsplit(" ", 1)[-1]
                    reply = f"{protocol} 200 OK\r\nContent-Length: 0\r\nAudio-Latency: 0\r\n"
                    if "cseq" in headers:
                        reply += f"CSeq: {headers['cseq']}\r\n"
                    self.channel.write((reply + "\r\n").encode())
        except (OSError, InvalidTag):
            return

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class AirPlay2Session(Session):
    user_agent = "AirPlay/550.10"
    keepalive_interval = 2.0          # POST /feedback
    sync_sets_extension = True
    min_latency_samples = 11025       # what we offer in SETUP: 0.25 s ...
    max_latency_samples = 88200       # ... to 2 s

    def __init__(self, receiver, password: str, latency_ms: int = 2000, log=None):
        super().__init__(receiver, password=password, latency_ms=latency_ms, log=log)
        self.stream_connection_id = random.getrandbits(32)
        self.audio_key = os.urandom(32)
        self._audio_cipher = ChaCha20Poly1305(self.audio_key)
        self._audio_counter = 0
        self._events: EventChannel | None = None

    def _post_pairing(self, path: str, body: bytes) -> bytes:
        response = self.rtsp.send("POST", path, [("X-Apple-HKP", "3"), ("Content-Type", "application/octet-stream")],
                                  body)
        if response.status != 200:
            raise PairingError(f"{path}: {response.status} {response.reason}", response.status)
        return response.body

    def _plist(self, method: str, body: dict) -> dict:
        response = self.rtsp.request(method, self.uri, [("Content-Type", "application/x-apple-binary-plist")],
                                     plistlib.dumps(body, fmt=plistlib.FMT_BINARY))
        return plistlib.loads(response.body) if response.body else {}

    def connect(self, volume_percent: float = 35) -> None:
        r = self.receiver
        self.log.info("Connecting to %s at %s:%s (AirPlay 2 password pairing)", r.name, r.host, r.port)
        self.rtsp.open()

        pairing = HapPairing(self._post_pairing)
        pairing.pair_setup(self.password or "")
        shared = pairing.pair_verify()
        self.rtsp.encrypt(HapChannel(self.rtsp.sock, hkdf("Control-Salt", "Control-Write-Encryption-Key", shared),
                                     hkdf("Control-Salt", "Control-Read-Encryption-Key", shared)))
        self.log.info("Paired; the control connection is now encrypted")

        self.uri = f"rtsp://{self.rtsp.local_ip}/{self.stream_connection_id}"
        self.streamer = Streamer(self)
        mac = ":".join(f"{b:02X}" for b in uuid.getnode().to_bytes(6, "big"))
        info = self._plist("SETUP", {
            "deviceID": mac, "macAddress": mac, "sessionUUID": str(uuid.uuid4()).upper(),
            "timingProtocol": "NTP", "timingPort": self.streamer.timing_port,
            "isMultiSelectAirPlay": True, "groupContainsGroupLeader": False,
            "name": "airplay-windows", "model": "Windows", "sourceVersion": "550.10",
            "senderSupportsRelay": False, "statsCollectionEnabled": False,
        })
        if info.get("eventPort"):
            self._events = EventChannel(r.host, info["eventPort"], shared)
            self._events.start()

        stream = self._plist("SETUP", {"streams": [{
            "type": STREAM_REALTIME, "ct": COMPRESSION_PCM, "audioFormat": AUDIO_FORMAT_PCM_44100_16_2,
            "spf": rtp.FRAMES_PER_PACKET, "sr": rtp.SAMPLE_RATE, "audioMode": "default",
            "controlPort": self.streamer.control_port, "shk": self.audio_key,
            "latencyMin": self.min_latency_samples, "latencyMax": self.max_latency_samples, "isMedia": True,
            "supportsDynamicStreamID": False, "streamConnectionID": self.stream_connection_id,
        }]})["streams"][0]
        self.audio_ep = (r.host, stream["dataPort"])
        self.control_ep = (r.host, stream["controlPort"])
        self.session_id = "0"   # AirPlay 2 receivers don't hand out an RTSP session id

        self.rtsp.request("RECORD", self.uri)
        self.rtsp.request("FLUSH", self.uri, [("Range", "npt=0-"), ("Session", self.session_id),
                                              ("RTP-Info", f"seq={self.streamer.first_seq};"
                                                           f"rtptime={self.streamer.first_timestamp}")])
        self.set_volume(volume_percent)
        self.log.info("Connected: audio=%s control=%s latency=%d samples", self.audio_ep, self.control_ep,
                      self.latency_samples)

    def set_volume(self, percent: float) -> None:
        self.rtsp.request("SET_PARAMETER", self.uri, [("Content-Type", "text/parameters")],
                          f"volume: {rtp.percent_to_db(percent):.6f}\r\n".encode("ascii"))

    def keepalive(self) -> None:
        self.rtsp.send("POST", "/feedback", quiet=True)

    def build_audio_packet(self, seq: int, timestamp: int, pcm: bytes, first: bool) -> bytes:
        head = bytes([0x80, 0xE0 if first else 0x60]) + struct.pack(
            ">HII", seq & 0xFFFF, timestamp & 0xFFFFFFFF, self.stream_connection_id)
        samples = array.array("h")
        samples.frombytes(pcm)
        samples.byteswap()              # PCM is big-endian on the wire
        n = nonce(self._audio_counter)
        self._audio_counter += 1
        return head + self._audio_cipher.encrypt(n, samples.tobytes(), head[4:12]) + n[4:]

    def wrap_resend(self, packet: bytes) -> bytes:
        return b"\x80\xd6" + packet[2:4] + packet

    def close(self) -> None:
        if self._events:
            self._events.close()
        super().close()


def decrypt_audio_packet(packet: bytes, key: bytes) -> bytes:
    """Inverse of AirPlay2Session.build_audio_packet, returning little-endian PCM."""
    head, sealed, tail = packet[:12], packet[12:-8], packet[-8:]
    samples = array.array("h")
    samples.frombytes(ChaCha20Poly1305(key).decrypt(bytes(4) + tail, sealed, head[4:12]))
    samples.byteswap()
    return samples.tobytes()
