"""Parse packets read from the catch-all TUN device and decide whether they are user demand.

Everything here works on untrusted bytes and must never raise.
"""

import ipaddress
import struct
from dataclasses import dataclass

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

TCP, UDP, ICMP, ICMPV6 = 6, 17, 1, 58
# IPv6 extension headers that use the generic (next header, length in 8-byte units - 1) layout.
_IPV6_EXT = {0, 43, 60}
_IPV6_FRAGMENT = 44

# DHCP, DHCPv6, NTP, NetBIOS, SSDP, mDNS, LLMNR: local or background protocols, never demand.
IGNORED_UDP_PORTS = {67, 68, 123, 137, 138, 546, 547, 1900, 5353, 5355}

# Suffix matches. Local-only names and reverse lookups.
LOCAL_SUFFIXES = ("local", "lan", "home.arpa", "internal", "in-addr.arpa", "ip6.arpa")

# Background lookups made without the user asking for anything. Suffix matches, so only list
# names whose whole subtree is background traffic.
BACKGROUND_NAMES = (
    # Connectivity checks (NetworkManager distro defaults, GNOME, KDE, Ubuntu, Android-style).
    "ping.archlinux.org",
    "nmcheck.gnome.org",
    "networkcheck.kde.org",
    "connectivity-check.ubuntu.com",
    "connectivitycheck.gstatic.com",
    "connectivitycheck.android.com",
    "detectportal.firefox.com",
    # Time.
    "pool.ntp.org",
    "ntp.ubuntu.com",
    "time.cloudflare.com",
    # Update mirrors and update checks.
    "geo.mirror.pkgbuild.com",
    "mirror.cachyos.org",
    "motd.ubuntu.com",
    "odrs.gnome.org",
    "aus5.mozilla.org",
    "update.googleapis.com",
    "clients2.google.com",
    "update.code.visualstudio.com",
    # Telemetry and browser background services.
    "incoming.telemetry.mozilla.org",
    "push.services.mozilla.com",
    "firefox.settings.services.mozilla.com",
    "content-signature-2.cdn.mozilla.net",
    "shavar.services.mozilla.com",
    "normandy.cdn.mozilla.net",
    "safebrowsing.googleapis.com",
    "optimizationguide-pa.googleapis.com",
    "clientservices.googleapis.com",
    "dc.services.visualstudio.com",
    "mobile.events.data.microsoft.com",
    # Tailscale control plane and logging.
    "controlplane.tailscale.com",
    "log.tailscale.io",
    "log.tailscale.com",
)


@dataclass
class Packet:
    proto: int
    src: IPAddress
    dst: IPAddress
    sport: int | None = None
    dport: int | None = None
    icmp_type: int | None = None
    dns_name: str | None = None  # set for UDP to port 53 when the question parses


def parse(data: bytes) -> Packet | None:
    """Parse an IPv4 or IPv6 packet (no packet-info header). None if it is not usable."""
    try:
        return _parse(data)
    except (IndexError, ValueError, struct.error):
        return None


def _parse(data: bytes) -> Packet | None:
    if not data:
        return None
    version = data[0] >> 4
    if version == 4:
        ihl = (data[0] & 0x0F) * 4
        if ihl < 20 or len(data) < ihl:
            return None
        proto = data[9]
        src = ipaddress.IPv4Address(data[12:16])
        dst = ipaddress.IPv4Address(data[16:20])
        frag_offset = struct.unpack_from("!H", data, 6)[0] & 0x1FFF
        if frag_offset:
            return Packet(proto, src, dst)  # later fragment: no transport header
        payload = data[ihl:]
    elif version == 6:
        if len(data) < 40:
            return None
        proto = data[6]
        src = ipaddress.IPv6Address(data[8:24])
        dst = ipaddress.IPv6Address(data[24:40])
        offset = 40
        for _ in range(8):  # bounded walk over extension headers
            if proto in _IPV6_EXT:
                proto, length = data[offset], (data[offset + 1] + 1) * 8
                offset += length
            elif proto == _IPV6_FRAGMENT:
                frag_offset = struct.unpack_from("!H", data, offset + 2)[0] >> 3
                proto = data[offset]
                offset += 8
                if frag_offset:
                    return Packet(proto, src, dst)
            else:
                break
        payload = data[offset:]
    else:
        return None

    pkt = Packet(proto, src, dst)
    if proto in (TCP, UDP) and len(payload) >= 4:
        pkt.sport, pkt.dport = struct.unpack_from("!HH", payload)
        if proto == UDP and pkt.dport == 53:
            pkt.dns_name = parse_dns_question(payload[8:])
    elif proto in (ICMP, ICMPV6) and payload:
        pkt.icmp_type = payload[0]
    return pkt


def parse_dns_question(msg: bytes) -> str | None:
    """Return the lowercased first question name of a DNS query, or None if malformed."""
    if len(msg) < 12:
        return None
    flags, qdcount = struct.unpack_from("!HH", msg, 2)
    if flags & 0x8000 or (flags >> 11) & 0xF != 0 or qdcount == 0:
        return None  # a response, not a standard query, or no question
    labels: list[str] = []
    pos, jumps, total = 12, 0, 0
    while True:
        if pos >= len(msg):
            return None
        length = msg[pos]
        if length & 0xC0 == 0xC0:
            if pos + 1 >= len(msg) or jumps >= 16:
                return None
            pos = ((length & 0x3F) << 8) | msg[pos + 1]
            jumps += 1
            continue
        if length & 0xC0:
            return None  # reserved label types
        if length == 0:
            break
        label = msg[pos + 1 : pos + 1 + length]
        if len(label) != length:
            return None
        total += length + 1
        if total > 255:
            return None
        try:
            labels.append(label.decode("ascii").lower())
        except UnicodeDecodeError:
            return None
        pos += 1 + length
    return ".".join(labels) or None


def _matches_suffix(name: str, suffixes: tuple[str, ...]) -> bool:
    return any(name == s or name.endswith("." + s) for s in suffixes)


def _ignored_destination(dst: IPAddress) -> bool:
    if dst.is_multicast or dst.is_link_local or dst.is_unspecified or dst.is_loopback:
        return True
    return dst == ipaddress.IPv4Address("255.255.255.255")


def demand_reason(pkt: Packet, deny_names: tuple[str, ...] = ()) -> str | None:
    """A human-readable reason if the packet is plausible user demand, otherwise None."""
    if _ignored_destination(pkt.dst):
        return None
    if pkt.proto == UDP:
        if pkt.dport is None or pkt.dport in IGNORED_UDP_PORTS or pkt.sport in IGNORED_UDP_PORTS:
            return None
        if pkt.dport == 53:
            name = pkt.dns_name
            if name is None or "." not in name:
                return None
            if _matches_suffix(name, LOCAL_SUFFIXES + BACKGROUND_NAMES + deny_names):
                return None
            return f"dns:{name}"
        return f"udp:{_hostport(pkt.dst, pkt.dport)}"
    if pkt.proto == TCP:
        if pkt.dport is None:
            return None
        return f"tcp:{_hostport(pkt.dst, pkt.dport)}"
    if (pkt.proto == ICMP and pkt.icmp_type == 8) or (pkt.proto == ICMPV6 and pkt.icmp_type == 128):
        return f"ping:{pkt.dst}"
    # Other ICMP (including IPv6 ND and MLD), fragments without ports, and other protocols.
    return None


def _hostport(addr: IPAddress, port: int) -> str:
    return f"[{addr}]:{port}" if addr.version == 6 else f"{addr}:{port}"


def normalize_names(names: list[str]) -> tuple[str, ...]:
    return tuple(n.strip().strip(".").lower() for n in names if n.strip().strip("."))
