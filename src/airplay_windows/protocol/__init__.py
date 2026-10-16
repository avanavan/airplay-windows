"""AirPlay sender: AirPlay 1 (RAOP) and AirPlay 2 with password pairing."""

from __future__ import annotations

from airplay_windows.protocol.airplay2 import AirPlay2Session
from airplay_windows.protocol.hap import PairingError
from airplay_windows.protocol.raop import RaopSession
from airplay_windows.protocol.receiver import Receiver
from airplay_windows.protocol.rtsp import RtspError
from airplay_windows.protocol.session import Session

__all__ = ["AirPlay2Session", "PairingError", "RaopSession", "Receiver", "RtspError", "Session", "open_session"]


def open_session(receiver: Receiver, password: str | None = None, *, airplay2: bool | None = None,
                 latency_ms: int = 2000, log=None) -> Session:
    """The right session type for `receiver`: AirPlay 2 pairing when it requires a password.

    `airplay2` forces the choice either way; None decides from the TXT record.
    """
    use_airplay2 = receiver.requires_password_pairing if airplay2 is None else airplay2
    if use_airplay2:
        return AirPlay2Session(receiver, password=password or "", latency_ms=latency_ms, log=log)
    return RaopSession(receiver, password=password, latency_ms=latency_ms, log=log)
