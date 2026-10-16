"""Find AirPlay receivers (`_raop._tcp`) on the local network.

Two methods run together: a normal Bonjour browse, and a "legacy unicast"
mDNS query sent from an ordinary port. Receivers answer the second one
directly instead of to the multicast group, which gets through networks that
drop multicast between Wi-Fi and wired segments.
"""

from __future__ import annotations

import logging
import select
import socket
import threading
import time

from airplay_windows.protocol.receiver import Receiver

log = logging.getLogger(__name__)

RAOP_SERVICE = "_raop._tcp.local."
MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353


def discover(timeout: float = 4.0) -> tuple[list[Receiver], set[str]]:
    """Returns the receivers found, and the service names only the direct-reply query found."""
    direct: dict[str, Receiver] = {}
    targets = [(ip, MDNS_GROUP) for ip in local_ipv4_addresses()]
    worker = threading.Thread(target=lambda: direct.update(direct_reply_query(RAOP_SERVICE, targets, min(timeout, 3.0))))
    worker.start()
    try:
        bonjour = bonjour_browse(timeout)
    except OSError as exc:      # e.g. port 5353 unavailable
        log.warning("Bonjour browse failed: %s", exc)
        bonjour = {}
    worker.join()
    merged = {**direct, **bonjour}
    return sorted(merged.values(), key=lambda r: r.name.lower()), set(direct) - set(bonjour)


def query_host(host: str, port: int | None = None, timeout: float = 2.0) -> Receiver | None:
    """Ask one address for its AirPlay service record. None if it doesn't answer mDNS."""
    found = direct_reply_query(RAOP_SERVICE, [("0.0.0.0", host)], timeout)
    receiver = next((r for r in found.values() if r.host == host), None) or next(iter(found.values()), None)
    if receiver and port:
        receiver.port = port
    return receiver


def bonjour_browse(timeout: float) -> dict[str, Receiver]:
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

    found: dict[str, Receiver] = {}
    zc = Zeroconf()

    class Listener(ServiceListener):
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name, timeout=3000)
            if not info or not info.parsed_addresses():
                return
            service_name = name[: -len(type_) - 1] if name.endswith("." + type_) else name
            if "@" not in service_name:
                return   # AirPlay audio services are always "<id>@<name>"
            ipv4 = [a for a in info.parsed_addresses() if ":" not in a]
            txt = {k.decode(errors="replace"): (v or b"").decode(errors="replace") for k, v in info.properties.items()}
            found[service_name] = Receiver(service_name, (ipv4 or info.parsed_addresses())[0], info.port, txt)

        update_service = add_service

        def remove_service(self, zc, type_, name):
            pass

    browser = ServiceBrowser(zc, RAOP_SERVICE, Listener())
    time.sleep(timeout)
    browser.cancel()
    zc.close()
    return found


def direct_reply_query(service_type: str, targets: list[tuple[str, str]], timeout: float = 3.0,
                       port: int = MDNS_PORT) -> dict[str, Receiver]:
    """mDNS query from an ephemeral port, so responders reply directly (RFC 6762 section 6.7).

    `targets` holds (local address to send from, destination) pairs; the
    destination is the multicast group or one device's own address.
    """
    from zeroconf import DNSIncoming, DNSOutgoing, DNSQuestion
    from zeroconf.const import _CLASS_IN, _FLAGS_QR_QUERY, _TYPE_ANY, _TYPE_PTR

    def query(name: str, qtype: int) -> bytes:
        out = DNSOutgoing(_FLAGS_QR_QUERY, multicast=False)
        out.add_question(DNSQuestion(name, qtype, _CLASS_IN))
        return out.packets()[0]

    services: dict[str, str] = {}                  # lowercase name -> name as advertised
    responders: dict[str, tuple[str, socket.socket]] = {}
    srv: dict[str, tuple[str, int]] = {}
    txt: dict[str, dict[str, str]] = {}
    addresses: dict[str, str] = {}

    def absorb(data: bytes, source: str, sock: socket.socket) -> None:
        try:
            records = DNSIncoming(data).answers()
        except Exception:
            return
        for r in records:
            if r.type == _TYPE_PTR and r.key == service_type.lower():
                services[r.alias.lower()] = r.alias
                responders.setdefault(r.alias.lower(), (source, sock))
            elif r.type == 33:   # SRV
                srv[r.key] = (r.server.lower(), r.port)
            elif r.type == 16:   # TXT
                txt[r.key] = parse_txt(r.text)
            elif r.type == 1:    # A
                addresses[r.key] = socket.inet_ntoa(r.address)

    def collect(socks: list[socket.socket], seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while socks and (remaining := deadline - time.monotonic()) > 0:
            readable, _, _ = select.select(socks, [], [], remaining)
            for sock in readable:
                try:
                    data, (source, _) = sock.recvfrom(9000)
                except OSError:
                    continue
                absorb(data, source, sock)

    senders: list[tuple[socket.socket, str]] = []
    for local_ip, destination in targets:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((local_ip, 0))
            if destination == MDNS_GROUP:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
            senders.append((sock, destination))
        except OSError:
            sock.close()
    socks = [sock for sock, _ in senders]
    try:
        for _ in range(3):   # UDP queries get lost and busy receivers skip some; ask three times
            for sock, destination in senders:
                try:
                    sock.sendto(query(service_type, _TYPE_PTR), (destination, port))
                except OSError:
                    pass
            collect(socks, timeout / 3)
        # A responder may answer the PTR question without SRV/TXT; ask it for the rest directly.
        incomplete = [key for key in services if key not in srv or key not in txt]
        for key in incomplete:
            source, sock = responders[key]
            try:
                sock.sendto(query(services[key], _TYPE_ANY), (source, port))
            except OSError:
                pass
        if incomplete:
            collect(socks, 1.0)
    finally:
        for sock in socks:
            sock.close()

    receivers = {}
    for key, alias in services.items():
        if key not in srv:
            continue
        server, service_port = srv[key]
        service_name = alias[: -len(service_type) - 1]
        if "@" not in service_name:
            continue
        host = addresses.get(server) or responders[key][0]
        receivers[service_name] = Receiver(service_name, host, service_port, txt.get(key, {}))
    return receivers


def parse_txt(raw: bytes) -> dict[str, str]:
    """DNS TXT data: a run of length-prefixed "key=value" strings."""
    out, i = {}, 0
    while i < len(raw):
        length = raw[i]
        item = raw[i + 1: i + 1 + length].decode(errors="replace")
        i += 1 + length
        if item:
            key, _, value = item.partition("=")
            out.setdefault(key, value)
    return out


def local_ipv4_addresses() -> list[str]:
    import ifaddr  # installed with zeroconf

    return [ip.ip for adapter in ifaddr.get_adapters() for ip in adapter.ips
            if isinstance(ip.ip, str) and not ip.ip.startswith(("127.", "169.254."))]
