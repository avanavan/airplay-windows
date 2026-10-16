"""What AirPlay 1 and AirPlay 2 sessions share: the RTSP connection and the UDP streamer."""

from __future__ import annotations

import logging
import random
import socket
import struct
import threading
import time
from typing import Iterable

from airplay_windows import __version__
from airplay_windows.protocol import rtp
from airplay_windows.protocol.receiver import Receiver
from airplay_windows.protocol.rtsp import RtspConnection


class Session:
    """Connect to one receiver, stream PCM to it, change its volume, disconnect.

    Subclasses implement connect(), set_volume(), keepalive() and the packet
    format; the Streamer calls back into them.
    """

    user_agent = f"airplay-windows/{__version__}"
    keepalive_interval = 15.0
    sync_sets_extension = False
    min_latency_samples = 0
    max_latency_samples = 1 << 31

    def __init__(self, receiver: Receiver, password: str | None = None, latency_ms: int = 2000,
                 log: logging.Logger | logging.LoggerAdapter | None = None):
        self.receiver = receiver
        self.password = password
        self.log = log or logging.getLogger(__name__)
        self.latency_samples = self._clamp_latency(int(latency_ms / 1000 * rtp.SAMPLE_RATE))
        # Receivers use these to address remote-control requests back to the sender.
        self.dacp_id = f"{random.getrandbits(64):016X}"
        self.active_remote = random.randint(100000000, 2**31 - 1)
        self.rtsp = RtspConnection(
            receiver.host, receiver.port, self.user_agent,
            [("Client-Instance", self.dacp_id), ("DACP-ID", self.dacp_id), ("Active-Remote", str(self.active_remote))],
            password=password, digest_upper=not receiver.digest_lowercase, log=self.log)
        self.uri = "*"
        self.session_id = ""
        self.audio_ep: tuple[str, int] | None = None
        self.control_ep: tuple[str, int] | None = None
        self.streamer: Streamer | None = None

    def _clamp_latency(self, samples: int) -> int:
        return min(max(samples, self.min_latency_samples), self.max_latency_samples)

    # -- implemented by subclasses --

    def connect(self, volume_percent: float = 35) -> None:
        raise NotImplementedError

    def set_volume(self, percent: float) -> None:
        raise NotImplementedError

    def keepalive(self) -> None:
        raise NotImplementedError

    def build_audio_packet(self, seq: int, timestamp: int, pcm: bytes, first: bool) -> bytes:
        raise NotImplementedError

    def wrap_resend(self, packet: bytes) -> bytes:
        return rtp.resend_response(packet)

    # -- shared --

    def stream(self, pcm_source: Iterable[bytes], stop: threading.Event | None = None) -> int:
        """Send `pcm_source` (352-frame chunks) in real time until it ends or `stop` is set."""
        return self.streamer.run(pcm_source, stop or threading.Event())

    def abort(self) -> None:
        self.rtsp.abort()

    def close(self) -> None:
        if self.streamer:
            self.streamer.stop()
        if self.rtsp.sock:
            try:
                self.rtsp.sock.settimeout(2.5)
                if self.session_id:
                    self.rtsp.send("TEARDOWN", self.uri, [("Session", self.session_id)])
            except Exception:   # best effort: the receiver may already be gone
                pass
            self.rtsp.close()


class Streamer:
    """The UDP side: audio packets, a sync packet every second, timing replies and resends."""

    BACKLOG = 1000   # packets kept for retransmission

    def __init__(self, session: Session):
        self.session = session
        self.audio_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.control_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.timing_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for s in (self.audio_sock, self.control_sock, self.timing_sock):
            s.bind(("0.0.0.0", 0))
        self.control_port = self.control_sock.getsockname()[1]
        self.timing_port = self.timing_sock.getsockname()[1]
        self.first_seq = random.randint(0, 0xFFFF)
        self.first_timestamp = random.randint(0, 0xFFFFFFFF)
        self.ssrc = random.getrandbits(32)
        self._backlog: dict[int, bytes] = {}
        self._stopped = threading.Event()
        self._t0 = time.perf_counter()
        self.packets_sent = 0
        self.resends_served = 0
        self.timing_replies = 0
        for target in (self._timing_loop, self._control_loop):
            threading.Thread(target=target, daemon=True).start()

    def rtp_clock(self) -> int:
        return (self.first_timestamp + int((time.perf_counter() - self._t0) * rtp.SAMPLE_RATE)) & 0xFFFFFFFF

    def _timing_loop(self) -> None:
        self.timing_sock.settimeout(0.5)
        while not self._stopped.is_set():
            try:
                data, addr = self.timing_sock.recvfrom(128)
            except (socket.timeout, OSError):
                continue
            reply = rtp.timing_response(data)
            if reply:
                self.timing_sock.sendto(reply, addr)
                self.timing_replies += 1

    def _control_loop(self) -> None:
        self.control_sock.settimeout(0.5)
        while not self._stopped.is_set():
            try:
                data, _ = self.control_sock.recvfrom(128)
            except (socket.timeout, OSError):
                continue
            if len(data) != 8 or data[1] & 0x7F != rtp.PT_RESEND_REQUEST:
                continue
            missed, count = struct.unpack(">HH", data[4:8])
            for i in range(count):
                packet = self._backlog.get((missed + i) & 0xFFFF)
                if packet is None:
                    continue
                try:
                    if self.session.control_ep:
                        self.control_sock.sendto(self.session.wrap_resend(packet), self.session.control_ep)
                    else:
                        self.audio_sock.sendto(packet, self.session.audio_ep)
                except OSError:
                    continue
                self.resends_served += 1

    def _send_sync(self, first: bool) -> None:
        if not self.session.control_ep:
            return
        now = (self.rtp_clock() - 882) & 0xFFFFFFFF     # 20 ms margin
        packet = rtp.sync_packet(now - self.session.latency_samples, now, first and self.session.sync_sets_extension)
        self.control_sock.sendto(packet, self.session.control_ep)

    def run(self, pcm_source: Iterable[bytes], stop: threading.Event) -> int:
        seq, ts = self.first_seq, self.first_timestamp
        self._t0 = time.perf_counter()
        next_sync = 0.0
        next_keepalive = self._t0 + self.session.keepalive_interval
        sent = 0
        for pcm in pcm_source:
            if stop.is_set() or self._stopped.is_set():
                break
            if len(pcm) < rtp.PCM_BYTES_PER_PACKET:
                pcm = pcm + bytes(rtp.PCM_BYTES_PER_PACKET - len(pcm))
            now = time.perf_counter()
            if now >= next_sync:
                self._send_sync(first=sent == 0)
                next_sync = now + 1.0
            if now >= next_keepalive:
                self.session.keepalive()
                next_keepalive = now + self.session.keepalive_interval
            packet = self.session.build_audio_packet(seq, ts, pcm, first=sent == 0)
            self._backlog[seq] = packet
            self._backlog.pop((seq - self.BACKLOG) & 0xFFFF, None)
            self.audio_sock.sendto(packet, self.session.audio_ep)
            sent += 1
            self.packets_sent = sent
            seq = (seq + 1) & 0xFFFF
            ts = (ts + rtp.FRAMES_PER_PACKET) & 0xFFFFFFFF
            delay = self._t0 + sent * rtp.FRAMES_PER_PACKET / rtp.SAMPLE_RATE - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
        return sent

    def stop(self) -> None:
        self._stopped.set()
        for s in (self.audio_sock, self.control_sock, self.timing_sock):
            s.close()
