import ipaddress
import struct

from lasso.demand import (
    DemandWatcher,
    cgroup_names,
    find_socket_cgroup,
    socket_inode,
)
from lasso.demand.packet import demand_reason, parse, reject_reply
from test_demand_packet import (
    DST6,
    SRC6,
    config,
    dns_query,
    ipv4,
    ipv6,
    tcp_syn,
    udp,
)


def ones_complement_ok(pseudo_src: bytes, pseudo_dst: bytes, nh: int, body: bytes) -> bool:
    """Independent check: summing pseudo header + body (checksum included) folds to 0xffff."""
    data = pseudo_src + pseudo_dst + len(body).to_bytes(4, "big") + bytes(3) + bytes([nh]) + body
    if len(data) % 2:
        data += b"\0"
    total = sum(int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2))
    while total > 0xFFFF:
        total = (total & 0xFFFF) + (total >> 16)
    return total == 0xFFFF


def split(reply: bytes):
    assert reply[0] >> 4 == 6
    plen, nh = struct.unpack_from("!HB", reply, 4)
    assert plen == len(reply) - 40
    return reply[8:24], reply[24:40], nh, reply[40:]


def reject(packet: bytes) -> bytes | None:
    return reject_reply(parse(packet), packet)


# Rejection replies


def test_tcp_syn_gets_rst_ack():
    syn = ipv6(6, tcp_syn(443))  # seq 1, no payload
    src, dst, nh, tcp = split(reject(syn))
    assert src == ipaddress.IPv6Address(DST6).packed and dst == ipaddress.IPv6Address(SRC6).packed
    assert nh == 6
    sport, dport, seq, ack, off, flags = struct.unpack_from("!HHIIBB", tcp)
    assert (sport, dport, seq, ack, flags) == (443, 40000, 0, 2, 0x14)
    assert ones_complement_ok(src, dst, 6, tcp)


def test_syn_with_data_acks_payload():
    hdr = struct.pack("!HHIIBBHHH", 40000, 80, 100, 0, 5 << 4, 0x02, 64240, 0, 0)
    tcp = split(reject(ipv6(6, hdr + b"abcd")))[3]
    assert struct.unpack_from("!I", tcp, 8)[0] == 100 + 1 + 4


def test_non_syn_tcp_and_udp_get_icmp_unreachable():
    ack = struct.pack("!HHIIBBHHH", 40000, 80, 5, 5, 5 << 4, 0x10, 1000, 0, 0)
    for packet, code in [(ipv6(6, ack), 4), (ipv6(17, udp(443, b"quic")), 4),
                         (ipv6(58, b"\x80\0\0\0\0\0\0\0"), 0), (ipv6(47, b"\0" * 8), 0)]:
        src, dst, nh, icmp = split(reject(packet))
        assert nh == 58 and icmp[0] == 1 and icmp[1] == code
        assert icmp[8:] == packet[: len(icmp) - 8]  # invoking packet included
        assert ones_complement_ok(src, dst, 58, icmp)


def test_icmp_reply_truncated_to_min_mtu():
    reply = reject(ipv6(17, udp(443, b"x" * 3000)))
    assert len(reply) == 1280
    src, dst, nh, icmp = split(reply)
    assert ones_complement_ok(src, dst, 58, icmp)


def test_no_reply_cases():
    assert reject(ipv6(58, b"\x01\x04\0\0\0\0\0\0")) is None  # ICMPv6 error
    assert reject(ipv6(58, b"\x03\0\0\0\0\0\0\0")) is None  # time exceeded
    assert reject(ipv6(17, udp(5353), dst="ff02::fb")) is None  # multicast destination
    assert reject(ipv6(58, b"\x80\0\0\0\0\0\0\0", dst="ff02::1")) is None
    pkt = ipv6(17, udp(443))
    assert reject(pkt[:8] + bytes(16) + pkt[24:]) is None  # unspecified source
    assert reject(pkt[:8] + ipaddress.IPv6Address("ff02::1").packed + pkt[24:]) is None
    assert reject_reply(parse(ipv4(6, tcp_syn(80))), b"") is None  # IPv4 is never answered


def test_watcher_rejects_after_classifying_and_rate_limits():
    watcher = DemandWatcher(config())
    sent: list[bytes] = []
    watcher.reply = sent.append
    calls: list[str] = []
    syn = ipv6(6, tcp_syn(443))
    for i in range(150):
        watcher.handle(syn, calls.append, 100.0 + i / 1000)
    assert len(sent) == 100 and len(calls) == 1
    watcher.handle(syn, calls.append, 101.5)  # new window
    assert len(sent) == 101
    watcher.handle(ipv4(6, tcp_syn(443)), calls.append, 102.0)
    assert len(sent) == 101  # IPv4 is untouched


def test_watcher_rejects_ignored_ipv6_too():
    watcher = DemandWatcher(config())
    sent: list[bytes] = []
    watcher.reply = sent.append
    watcher.handle(ipv6(17, udp(547), dst="fe80::1"), lambda r: None, 1.0)  # not demand
    assert len(sent) == 1


# TCP DNS


def test_tcp_dns_is_not_demand():
    assert demand_reason(parse(ipv4(6, tcp_syn(53)))) is None
    assert demand_reason(parse(ipv6(6, tcp_syn(53)))) is None
    assert demand_reason(parse(ipv4(17, udp(53, dns_query("example.com"))))) == "dns:example.com"


# /proc attribution

TCP_TABLE = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0200D30A:9C40 22EAB85D:01BB 02 00000000:00000000 00:00000000 00000000  1000        0 111 1 0 100 0
   1: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 222 1 0 100 0
   2: 0200D30A:9C41 22EAB85D:01BB 06 00000000:00000000 00:00000000 00000000     0        0 0 1 0 100 0
   garbage line
"""
TCP6_TABLE = """\
  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 02000000000000000000000000000000:9C42 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 333 1 0 100 0
   1: 000000000000000000000000FFFF0000:9C43 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 444 1 0 100 0
   2: 00000000000000000000000000000000:1F91 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 555 1 0 100 0
"""


def test_socket_inode_ipv4():
    a = ipaddress.ip_address("10.211.0.2")
    assert socket_inode(TCP_TABLE, a, 0x9C40) == 111
    assert socket_inode(TCP_TABLE, a, 0x1F90) == 222  # wildcard bind
    assert socket_inode(TCP_TABLE, a, 0x9C41) is None  # inode 0 (TIME_WAIT)
    assert socket_inode(TCP_TABLE, a, 1) is None
    assert socket_inode("", a, 1) is None


def proc_hex(addr: str) -> str:
    packed = ipaddress.ip_address(addr).packed
    return "".join(packed[i : i + 4][::-1].hex().upper() for i in range(0, len(packed), 4))


def test_socket_inode_ipv6_and_mapped():
    lasso6 = ipaddress.ip_address("fd6c:6173:736f::2")
    table = TCP6_TABLE.replace("02000000000000000000000000000000", proc_hex("fd6c:6173:736f::2"))
    table = table.replace("000000000000000000000000FFFF0000", proc_hex("::ffff:10.211.0.2"))
    assert socket_inode(table, lasso6, 0x9C42) == 333
    assert socket_inode(table, lasso6, 0x1F91) == 555  # :: wildcard
    assert socket_inode(table, ipaddress.ip_address("10.211.0.2"), 0x9C43) == 444  # dual-stack
    assert proc_hex("10.211.0.2") == "0200D30A"  # matches the fixture's IPv4 encoding


def test_cgroup_names():
    text = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/syncthing.service\n"
    names = cgroup_names(text)
    assert "syncthing.service" in names and "app.slice" in names and "syncthing" not in names
    assert cgroup_names("") == set()


def test_find_socket_cgroup(tmp_path):
    import os

    for pid, link in [("100", "socket:[999]"), ("200", "socket:[111]"), ("abc", "socket:[111]")]:
        d = tmp_path / pid / "fd"
        d.mkdir(parents=True)
        os.symlink(link, d / "3")
        (tmp_path / pid / "cgroup").write_text(f"0::/app.slice/{pid}.service\n")
    uid = os.getuid()
    assert find_socket_cgroup(111, str(tmp_path), uid) == "0::/app.slice/200.service\n"
    assert find_socket_cgroup(111, str(tmp_path), uid + 1) is None  # other user's processes
    assert find_socket_cgroup(5, str(tmp_path), uid) is None
    assert find_socket_cgroup(5, str(tmp_path / "missing"), uid) is None


def test_watcher_deny_units(monkeypatch):
    import lasso.demand as demand

    watcher = DemandWatcher(config(deny_units=["syncthing.service"]))
    calls: list[str] = []
    syn = ipv4(6, tcp_syn(443))  # source 10.211.0.2:40000
    monkeypatch.setattr(demand, "_read", lambda path: "")
    watcher.handle(syn, calls.append, 100.0)  # owner unknown: demand
    assert len(calls) == 1

    watcher = DemandWatcher(config(deny_units=["syncthing.service"]))
    monkeypatch.setattr(demand, "_read", lambda path: TCP_TABLE if path.endswith("/tcp") else "")
    monkeypatch.setattr(demand, "socket_inode", lambda table, a, p: 111 if table else None)
    cgroups = {"x": "0::/user.slice/app.slice/syncthing.service\n"}
    monkeypatch.setattr(demand, "find_socket_cgroup", lambda inode: cgroups["x"])
    calls.clear()
    watcher.handle(syn, calls.append, 200.0)
    assert calls == []  # denied
    cgroups["x"] = "0::/user.slice/app.slice/app-firefox.scope\n"
    watcher.handle(syn, calls.append, 201.0)  # still cached as denied for a short while
    assert calls == []
    watcher.handle(syn, calls.append, 220.0)  # cache expired, owner now allowed
    assert calls == ["tcp:93.184.216.34:443"]
    # DNS is never attributed.
    watcher = DemandWatcher(config(deny_units=["syncthing.service"]))
    calls.clear()
    watcher.handle(ipv4(17, udp(53, dns_query("example.com"))), calls.append, 300.0)
    assert calls == ["dns:example.com"]
