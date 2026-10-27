import ipaddress
import struct
from types import SimpleNamespace

from lasso.demand import DemandWatcher
from lasso.demand.packet import demand_reason, parse, parse_dns_question

SRC4, DST4 = "10.211.0.2", "93.184.216.34"
SRC6, DST6 = "fd6c:6173:736f::2", "2606:2800:220:1:248:1893:25c8:1946"


def ipv4(proto: int, payload: bytes, dst: str = DST4, frag: int = 0) -> bytes:
    header = struct.pack(
        "!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), 1, frag, 64, proto, 0,
        ipaddress.IPv4Address(SRC4).packed, ipaddress.IPv4Address(dst).packed,
    )
    return header + payload


def ipv6(next_header: int, payload: bytes, dst: str = DST6) -> bytes:
    header = struct.pack("!IHBB", 6 << 28, len(payload), next_header, 64)
    return header + ipaddress.IPv6Address(SRC6).packed + ipaddress.IPv6Address(dst).packed + payload


def udp(dport: int, data: bytes = b"", sport: int = 40000) -> bytes:
    return struct.pack("!HHHH", sport, dport, 8 + len(data), 0) + data


def tcp_syn(dport: int) -> bytes:
    return struct.pack("!HHIIBBHHH", 40000, dport, 1, 0, 5 << 4, 0x02, 64240, 0, 0)


def qname(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"


def dns_query(name: str) -> bytes:
    return struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0) + qname(name) + b"\0\1\0\1"


def reason(packet: bytes, deny: tuple[str, ...] = ()) -> str | None:
    pkt = parse(packet)
    return None if pkt is None else demand_reason(pkt, deny)


# Parsing


def test_ipv4_tcp():
    pkt = parse(ipv4(6, tcp_syn(443)))
    assert pkt.proto == 6 and str(pkt.src) == SRC4 and str(pkt.dst) == DST4 and pkt.dport == 443


def test_ipv6_udp():
    pkt = parse(ipv6(17, udp(443, b"quic")))
    assert pkt.proto == 17 and str(pkt.dst) == DST6 and pkt.sport == 40000 and pkt.dport == 443


def test_ipv6_skips_extension_header():
    hop_by_hop = bytes([6, 0]) + b"\0" * 6  # next header TCP, 8 bytes long
    pkt = parse(ipv6(0, hop_by_hop + tcp_syn(80)))
    assert pkt.proto == 6 and pkt.dport == 80


def test_ipv4_dns_name():
    assert parse(ipv4(17, udp(53, dns_query("Example.COM")))).dns_name == "example.com"


def test_ipv4_later_fragment_has_no_ports():
    pkt = parse(ipv4(6, b"\0" * 20, frag=100))
    assert pkt.dport is None and reason(ipv4(6, b"\0" * 20, frag=100)) is None


def test_garbage_and_truncated_packets():
    for data in [b"", b"\x45", b"\x45" + b"\0" * 10, b"\x60" + b"\0" * 20, b"\x10" * 40, b"\x4f" * 30]:
        assert parse(data) is None or demand_reason(parse(data)) is None
    full = ipv4(17, udp(53, dns_query("example.com")))
    for cut in range(len(full)):
        pkt = parse(full[:cut])  # must never raise
        if pkt is not None:
            demand_reason(pkt)


# DNS question parsing


def test_dns_compression_pointer():
    # The question name points at a copy of "example.com" placed after the question.
    header = struct.pack("!HHHHHH", 1, 0x0100, 1, 0, 0, 0)
    target = 12 + 2 + 4
    msg = header + struct.pack("!H", 0xC000 | target) + b"\0\1\0\1" + qname("example.com")
    assert parse_dns_question(msg) == "example.com"


def test_dns_pointer_loop_and_bad_pointer():
    header = struct.pack("!HHHHHH", 1, 0x0100, 1, 0, 0, 0)
    assert parse_dns_question(header + b"\xc0\x0c") is None  # points at itself
    assert parse_dns_question(header + b"\xc0\xff") is None  # out of range
    assert parse_dns_question(header + b"\xc0") is None  # truncated pointer


def test_dns_malformed():
    header = struct.pack("!HHHHHH", 1, 0x0100, 1, 0, 0, 0)
    assert parse_dns_question(b"\0" * 5) is None
    assert parse_dns_question(header + b"\x07exa") is None  # truncated label
    assert parse_dns_question(header + b"\x40abc\0") is None  # reserved label type
    assert parse_dns_question(header + b"\x03\xff\xfe\xfd\0") is None  # non-ascii
    assert parse_dns_question(header + b"\0") is None  # root
    assert parse_dns_question(header + (b"\x3f" + b"a" * 63) * 5 + b"\0") is None  # > 255
    response = struct.pack("!HHHHHH", 1, 0x8180, 1, 0, 0, 0) + qname("example.com")
    assert parse_dns_question(response) is None
    no_question = struct.pack("!HHHHHH", 1, 0x0100, 0, 0, 0, 0) + qname("example.com")
    assert parse_dns_question(no_question) is None


# Filtering


def test_demand_reasons():
    assert reason(ipv4(17, udp(53, dns_query("example.com")))) == "dns:example.com"
    assert reason(ipv4(6, tcp_syn(443))) == f"tcp:{DST4}:443"
    assert reason(ipv6(6, tcp_syn(443))) == f"tcp:[{DST6}]:443"
    assert reason(ipv6(17, udp(443))) == f"udp:[{DST6}]:443"
    assert reason(ipv4(1, b"\x08\0\0\0\0\0\0\0")) == f"ping:{DST4}"
    assert reason(ipv6(58, b"\x80\0\0\0\0\0\0\0")) == f"ping:{DST6}"


def test_dns_names_filtered():
    for name in ["printer", "nas.local", "router.lan", "x.home.arpa", "a.corp.internal",
                 "4.3.2.1.in-addr.arpa", "1.0.ip6.arpa", "ping.archlinux.org",
                 "0.arch.pool.ntp.org", "nmcheck.gnome.org", "detectportal.firefox.com"]:
        assert reason(ipv4(17, udp(53, dns_query(name)))) is None, name
    # Suffix match only on label boundaries.
    assert reason(ipv4(17, udp(53, dns_query("notpool.ntp.org.example.com")))) is not None
    assert reason(ipv4(17, udp(53, dns_query("myping.archlinux.org")))) is not None


def test_config_deny_names():
    packet = ipv4(17, udp(53, dns_query("api.syncthing.net")))
    assert reason(packet) == "dns:api.syncthing.net"
    assert reason(packet, ("syncthing.net",)) is None


def test_ignored_destinations_and_protocols():
    assert reason(ipv4(17, udp(5353), dst="224.0.0.251")) is None  # mDNS multicast
    assert reason(ipv4(17, udp(67, sport=68), dst="255.255.255.255")) is None  # DHCP
    assert reason(ipv4(6, tcp_syn(80), dst="169.254.1.1")) is None  # link-local
    assert reason(ipv6(17, udp(1900), dst="ff02::c")) is None  # SSDP
    assert reason(ipv6(17, udp(547), dst="fe80::1")) is None  # DHCPv6
    assert reason(ipv4(17, udp(5355))) is None  # LLMNR unicast
    assert reason(ipv6(58, b"\x85\0\0\0\0\0\0\0", dst="ff02::2")) is None  # router solicitation
    assert reason(ipv6(58, b"\x87\0\0\0\0\0\0\0")) is None  # neighbour solicitation, unicast
    assert reason(ipv6(58, b"\x8f\0\0\0\0\0\0\0", dst="ff02::16")) is None  # MLDv2 report
    assert reason(ipv4(1, b"\x00\0\0\0\0\0\0\0")) is None  # echo reply
    assert reason(ipv4(47, b"\0" * 8)) is None  # GRE


# Watcher: rate limiting and the missing-device path


def config(**kw):
    return SimpleNamespace(**{"tun_name": "lasso0", "deny_names": [], "deny_units": [], **kw})


def test_rate_limit():
    watcher = DemandWatcher(config(deny_names=["Blocked.Example."]))
    calls: list[str] = []
    syn = ipv4(6, tcp_syn(443))
    watcher.handle(syn, calls.append, 100.0)
    watcher.handle(syn, calls.append, 102.0)
    watcher.handle(syn, calls.append, 104.9)
    watcher.handle(syn, calls.append, 105.0)
    assert calls == [f"tcp:{DST4}:443"] * 2
    # Ignored and garbage packets do not consume the rate limit window.
    watcher.handle(ipv4(17, udp(53, dns_query("blocked.example"))), calls.append, 111.0)
    watcher.handle(b"\xff\x00", calls.append, 111.0)
    watcher.handle(ipv4(17, udp(53, dns_query("example.com"))), calls.append, 111.0)
    assert calls[-1] == "dns:example.com" and len(calls) == 3


def test_missing_device_returns(caplog):
    import asyncio

    watcher = DemandWatcher(config(tun_name="lasso-test-missing"))
    asyncio.run(watcher.run(lambda reason: None))
    assert "does not exist" in caplog.text
