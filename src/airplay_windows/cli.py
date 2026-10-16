"""Command line: find AirPlay speakers and play a test tone or WAV file.

  airplay-windows-cli discover
  airplay-windows-cli play --name "Bedroom" --password ...
  airplay-windows-cli play --host 192.168.1.50 --wav song.wav
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading

from airplay_windows import __version__, discovery, logs
from airplay_windows.audio import sources
from airplay_windows.protocol import PairingError, Receiver, RtspError, open_session, rtp


def describe(r: Receiver) -> str:
    text = (f"{r.name}  ({r.model_name})\n"
            f"    address     {r.host}:{r.port}   id={r.device_id}\n"
            f"    TXT         et={r.txt.get('et')} cn={r.txt.get('cn')} sf={r.txt.get('sf')} "
            f"pw={r.txt.get('pw')} vs={r.txt.get('vs')}\n"
            f"    connection  {'AirPlay 2 with password pairing' if r.requires_password_pairing else 'AirPlay 1'}"
            f"{', RSA/AES' if r.rsa_encryption else ''}{', auth-setup' if r.needs_auth_setup else ''}"
            f"{', password' if r.needs_password else ''}")
    if r.requires_pin_pairing:
        text += "\n    note        asks for PIN pairing, which isn't supported"
    return text


def cmd_discover(args) -> int:
    receivers, direct_only = discovery.discover(args.timeout)
    if not receivers:
        print("No AirPlay speakers found. Check that this PC and the speaker share a network and that the "
              "firewall allows mDNS (UDP 5353).")
        return 1
    for r in receivers:
        print(describe(r))
        if r.service_name in direct_only:
            print("    note        found only by the direct-reply query: this network drops the speaker's "
                  "multicast answers")
    return 0


def resolve(args) -> Receiver:
    if args.host:
        receiver = discovery.query_host(args.host, args.port)
        if receiver:
            return receiver
        print(f"{args.host} did not answer an mDNS query; treating it as a manually added speaker.")
        return Receiver.manual(args.host, args.port or 7000, encrypt=args.encrypt)
    receivers, _ = discovery.discover(args.timeout)
    matches = [r for r in receivers if args.name.lower() in r.name.lower()] if args.name else receivers
    if not matches:
        sys.exit(f"No speaker matching {args.name!r}. Run 'discover' to list speakers.")
    if len(matches) > 1:
        sys.exit(f"Several speakers match: {', '.join(r.name for r in matches)}. Use a more specific --name.")
    return matches[0]


def cmd_play(args) -> int:
    receiver = resolve(args)
    print(describe(receiver))
    source = sources.wav_file(args.wav) if args.wav else sources.tone(args.tone, args.seconds)
    use_airplay2 = True if args.airplay2 else None
    if (args.airplay2 or receiver.requires_password_pairing) and not args.password:
        print("This speaker requires its AirPlay password; pass --password.", file=sys.stderr)
        return 2
    session = open_session(receiver, args.password, airplay2=use_airplay2, latency_ms=args.latency)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        session.connect(volume_percent=args.volume)
        print("Streaming... (Ctrl+C to stop)")
        sent = session.stream(source, stop)
        if not stop.is_set():
            stop.wait(session.latency_samples / rtp.SAMPLE_RATE)   # let the speaker play out its buffer
        s = session.streamer
        print(f"Done: {sent} packets, {s.timing_replies} timing replies, {s.resends_served} resends.")
        return 0
    except (RtspError, PairingError, OSError) as exc:
        print(f"Connection failed: {exc}", file=sys.stderr)
        return 2
    finally:
        session.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="airplay-windows-cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", help="list AirPlay speakers on the network")
    p.add_argument("--timeout", type=float, default=4.0)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("play", help="connect to a speaker and play a tone or WAV file")
    target = p.add_mutually_exclusive_group()
    target.add_argument("--name", help="part of the speaker's name")
    target.add_argument("--host", help="the speaker's IP address (skips discovery)")
    p.add_argument("--port", type=int, help="with --host: RTSP port (default: what the speaker reports, else 7000)")
    p.add_argument("--encrypt", action="store_true", help="with --host, if the speaker doesn't answer mDNS: use RSA/AES")
    p.add_argument("--password", help="the speaker's AirPlay password")
    p.add_argument("--airplay2", action="store_true", help="use AirPlay 2 password pairing even if not advertised")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--wav", help="44.1 kHz 16-bit WAV file")
    src.add_argument("--tone", type=float, default=440.0, help="sine tone frequency in Hz (default 440)")
    p.add_argument("--seconds", type=float, default=10.0, help="tone length (default 10)")
    p.add_argument("--volume", type=float, default=35.0, help="0-100 (default 35)")
    p.add_argument("--latency", type=int, default=2000, help="buffer in ms (default 2000)")
    p.add_argument("--timeout", type=float, default=4.0, help="discovery time in seconds")
    p.set_defaults(func=cmd_play)

    args = parser.parse_args(argv)
    logs.setup(console=True, to_file=False)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
