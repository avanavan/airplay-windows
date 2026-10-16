"""The RTSP control connection to a receiver."""

from __future__ import annotations

import hashlib
import logging
import socket
import threading
from dataclasses import dataclass


class RtspError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class RtspResponse:
    status: int
    reason: str
    headers: dict[str, str]
    body: bytes

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


def transport_param(transport: str, name: str) -> int | None:
    """An integer parameter from a Transport header, e.g. server_port."""
    for part in transport.split(";"):
        key, _, value = part.partition("=")
        if key.strip().lower() == name and value.strip().isdigit():
            return int(value)
    return None


def digest_response(username: str, password: str, realm: str, nonce: str, method: str, uri: str,
                    upper: bool) -> str:
    """RFC 2617 digest without qop. AirPlay 1 receivers differ on hex case (TXT "da")."""
    def md5(text: str) -> str:
        digest = hashlib.md5(text.encode("latin-1")).hexdigest()
        return digest.upper() if upper else digest

    return md5(f"{md5(f'{username}:{realm}:{password}')}:{nonce}:{md5(f'{method}:{uri}')}")


def _auth_param(header: str, key: str) -> str:
    for part in header.replace("Digest", "").split(","):
        name, _, value = part.strip().partition("=")
        if name == key:
            return value.strip('"')
    return ""


class RtspConnection:
    """Requests and responses on one TCP connection; one exchange at a time across threads.

    After `encrypt(channel)`, bytes go through `channel` (an encrypted HAP channel)
    instead of the plain socket.
    """

    USERNAME = "iTunes"   # the username AirPlay 1 receivers expect in digest auth

    def __init__(self, host: str, port: int, user_agent: str, common_headers: list[tuple[str, str]],
                 password: str | None = None, digest_upper: bool = True, timeout: float = 30,
                 log: logging.Logger | logging.LoggerAdapter | None = None):
        self.host, self.port = host, port
        self.user_agent = user_agent
        self.common_headers = common_headers
        self.password = password
        self.digest_upper = digest_upper
        self.timeout = timeout
        self.log = log or logging.getLogger(__name__)
        self.sock: socket.socket | None = None
        self.local_ip = ""
        self._channel = None
        self._cseq = 0
        self._realm = self._nonce = ""
        self._use_auth = False
        self.lock = threading.RLock()

    def open(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        self.local_ip = self.sock.getsockname()[0]

    def encrypt(self, channel) -> None:
        self._channel = channel

    def _write(self, data: bytes) -> None:
        (self._channel.write if self._channel else self.sock.sendall)(data)

    def _recv(self, size: int) -> bytes:
        return self._channel.read(size) if self._channel else self.sock.recv(size)

    def send(self, method: str, uri: str, headers: list[tuple[str, str]] | None = None, body: bytes = b"",
             quiet: bool = False) -> RtspResponse:
        """One request and its response, whatever the status."""
        with self.lock:
            self._cseq += 1
            lines = [f"{method} {uri} RTSP/1.0", f"CSeq: {self._cseq}", *[f"{k}: {v}" for k, v in headers or []],
                     f"User-Agent: {self.user_agent}", *[f"{k}: {v}" for k, v in self.common_headers]]
            if self._use_auth:
                lines.append("Authorization: " + self._authorization(method, uri))
            if body:
                lines.append(f"Content-Length: {len(body)}")
            if not quiet:
                self.log.info(">> %s %s", method, uri)
            self._write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body)
            response = self._read_response()
            if not quiet:
                self.log.info("<< %s %s", response.status, response.reason)
            return response

    def request(self, method: str, uri: str, headers: list[tuple[str, str]] | None = None,
                body: bytes = b"") -> RtspResponse:
        """Like send(), but answers a digest challenge once and raises RtspError unless 200."""
        response = self.send(method, uri, headers, body)
        if response.status == 401:
            challenge = response.header("WWW-Authenticate") or ""
            self._realm, self._nonce = _auth_param(challenge, "realm"), _auth_param(challenge, "nonce")
            if not self.password:
                raise RtspError("the receiver requires a password", 401)
            self._use_auth = True
            response = self.send(method, uri, headers, body)
        if response.status == 200:
            return response
        messages = {401: "wrong password", 403: "the receiver refused the connection",
                    453: "the receiver is busy with another stream", 503: "the receiver is unavailable"}
        raise RtspError(f"{method}: {response.status} {messages.get(response.status, response.reason)}",
                        response.status)

    def _authorization(self, method: str, uri: str) -> str:
        response = digest_response(self.USERNAME, self.password or "", self._realm, self._nonce, method, uri,
                                   self.digest_upper)
        return (f'Digest username="{self.USERNAME}", realm="{self._realm}", nonce="{self._nonce}", '
                f'uri="{uri}", response="{response}"')

    def _read_response(self) -> RtspResponse:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self._recv(4096)
            if not chunk:
                raise RtspError("the receiver closed the connection")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0") or 0)
        while len(rest) < length:
            chunk = self._recv(length - len(rest))
            if not chunk:
                break
            rest += chunk
        return RtspResponse(int(parts[1]), parts[2] if len(parts) > 2 else "", headers, rest[:length])

    def abort(self) -> None:
        """Unblock a thread waiting on this connection (used to cancel a handshake)."""
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None
