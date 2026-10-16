"""Direct-reply mDNS discovery against a fake responder."""

import socket
import threading

import pytest

from airplay_windows import discovery


def fake_mdns_responder(split_txt: bool):
    """Answers like a HomePod. With split_txt, TXT only comes in reply to a follow-up ANY query."""
    from zeroconf import DNSAddress, DNSIncoming, DNSOutgoing, DNSPointer, DNSService, DNSText
    from zeroconf.const import _CLASS_IN, _FLAGS_AA, _FLAGS_QR_RESPONSE, _TYPE_A, _TYPE_PTR, _TYPE_SRV, _TYPE_TXT

    service = "EAC4C5275084@Bedroom._raop._tcp.local."
    txt = b"".join(bytes([len(kv)]) + kv for kv in (b"et=0,3,5", b"sf=0x80484", b"am=AudioAccessory5,1"))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(5)
    queries = []

    def serve():
        while True:
            try:
                data, addr = sock.recvfrom(9000)
            except OSError:
                return
            question = DNSIncoming(data).questions[0]
            queries.append(question.type)
            out = DNSOutgoing(_FLAGS_QR_RESPONSE | _FLAGS_AA, multicast=False)
            if question.type == _TYPE_PTR:
                out.add_answer_at_time(DNSPointer("_raop._tcp.local.", _TYPE_PTR, _CLASS_IN, 4500, service), 0)
                out.add_additional_answer(DNSService(service, _TYPE_SRV, _CLASS_IN, 120, 0, 0, 7000, "Bedroom.local."))
                out.add_additional_answer(DNSAddress("Bedroom.local.", _TYPE_A, _CLASS_IN, 120,
                                                     socket.inet_aton("10.9.8.7")))
            if question.type != _TYPE_PTR or not split_txt:
                out.add_additional_answer(DNSText(service, _TYPE_TXT, _CLASS_IN, 4500, txt))
            sock.sendto(out.packets()[0], addr)

    threading.Thread(target=serve, daemon=True).start()
    return sock, queries


@pytest.mark.parametrize("split_txt", [False, True])
def test_direct_reply_query(split_txt):
    responder, queries = fake_mdns_responder(split_txt)
    port = responder.getsockname()[1]
    try:
        found = discovery.direct_reply_query(discovery.RAOP_SERVICE, [("127.0.0.1", "127.0.0.1")], timeout=0.5,
                                             port=port)
    finally:
        responder.close()
    r = found["EAC4C5275084@Bedroom"]
    assert (r.host, r.port, r.model_name) == ("10.9.8.7", 7000, "HomePod mini")
    assert r.requires_password_pairing and not r.needs_auth_setup and not r.rsa_encryption
    assert (255 in queries) == split_txt   # the follow-up ANY query only when TXT was missing


def test_parse_txt():
    raw = b"\x06et=0,1\x00\x05pw=no\x04am=X"
    assert discovery.parse_txt(raw) == {"et": "0,1", "pw": "no", "am": "X"}
