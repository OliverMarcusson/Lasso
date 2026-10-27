"""Demand detection: read packets from the catch-all TUN device and report user demand."""

import asyncio
import fcntl
import ipaddress
import logging
import os
import struct
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from .packet import TCP, UDP, IPAddress, Packet, demand_reason, normalize_names, parse, reject_reply

if TYPE_CHECKING:
    from lasso.config import Config

log = logging.getLogger(__name__)

TUNSETIFF = 0x400454CA
IFF_TUN = 0x0001
IFF_NO_PI = 0x1000
RATE_LIMIT_S = 5.0
REJECTS_PER_S = 100
DENY_CACHE_S = 10.0
MAX_SCANNED_PIDS = 4096  # ponytail: bounds the /proc/*/fd scan; raise if a user runs more processes


def open_tun(name: str) -> int:
    """Attach to an existing persistent TUN device. Raises OSError on failure."""
    fd = os.open("/dev/net/tun", os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        fcntl.ioctl(fd, TUNSETIFF, struct.pack("16sH", name.encode(), IFF_TUN | IFF_NO_PI))
    except OSError:
        os.close(fd)
        raise
    return fd


def _proc_addr(hexaddr: str) -> IPAddress:
    """Decode a /proc/net address: 32-bit words in host (little endian) byte order."""
    raw = bytes.fromhex(hexaddr)
    packed = b"".join(raw[i : i + 4][::-1] for i in range(0, len(raw), 4))
    return ipaddress.ip_address(packed)


def socket_inode(table: str, addr: IPAddress, port: int) -> int | None:
    """Inode of the socket bound to addr:port (or the wildcard address) in /proc/net/{tcp,udp}*
    text. An exact address match wins over a wildcard bind."""
    wildcard = None
    for line in table.splitlines()[1:]:
        f = line.split()
        try:
            local, inode = f[1], int(f[9])
            host, _, hexport = local.partition(":")
            bound = _proc_addr(host)
            if int(hexport, 16) != port or inode == 0:
                continue
        except (IndexError, ValueError):
            continue
        bound = getattr(bound, "ipv4_mapped", None) or bound
        if bound == addr:
            return inode
        if bound.is_unspecified and wildcard is None:
            wildcard = inode
    return wildcard


def cgroup_names(text: str) -> set[str]:
    """All path components of the cgroups listed in /proc/PID/cgroup text."""
    names: set[str] = set()
    for line in text.splitlines():
        names.update(p for p in line.split(":", 2)[-1].split("/") if p)
    return names


def find_socket_cgroup(inode: int, proc: str = "/proc", uid: int | None = None) -> str | None:
    """cgroup text of the first process of this uid holding socket:[inode], or None."""
    uid = os.getuid() if uid is None else uid
    target = f"socket:[{inode}]"
    try:
        pids = [e for e in os.listdir(proc) if e.isdigit()][:MAX_SCANNED_PIDS]
    except OSError:
        return None
    for pid in pids:
        try:
            if os.stat(f"{proc}/{pid}").st_uid != uid:
                continue
            for fd in os.listdir(f"{proc}/{pid}/fd"):
                if os.readlink(f"{proc}/{pid}/fd/{fd}") == target:
                    with open(f"{proc}/{pid}/cgroup") as fh:
                        return fh.read()
        except OSError:
            continue
    return None


def _read(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


class DemandWatcher:
    def __init__(self, config: "Config") -> None:
        self.tun_name = config.tun_name
        self.deny_names = normalize_names(config.deny_names)
        self.deny_units = {u.strip() for u in config.deny_units if u.strip()}
        self._last: float | None = None
        self._denied: dict[tuple[int, int | None], float] = {}  # (proto, sport) -> expiry
        self._reject_window = 0.0
        self._reject_count = 0
        self.reply: Callable[[bytes], None] | None = None  # writes a packet back into the TUN

    async def run(self, on_demand: Callable[[str], None]) -> None:
        """Read packets from the TUN device forever. Call on_demand(reason) for each qualifying
        packet, rate-limited to at most once per 5 s. reason is human readable, for example
        "dns:example.com" or "tcp:93.184.216.34:443". Never raises because of malformed packets."""
        # TUNSETIFF on a missing name would try to create a new device, so check first.
        if not os.path.exists(f"/sys/class/net/{self.tun_name}"):
            log.warning("TUN device %s does not exist; auto-connect disabled", self.tun_name)
            return
        try:
            fd = open_tun(self.tun_name)
        except OSError as e:
            log.warning("cannot open TUN device %s (%s); auto-connect disabled", self.tun_name, e)
            return

        def reply(packet: bytes) -> None:
            try:
                os.write(fd, packet)
            except OSError:
                pass  # queue full or device gone: the reply is best effort

        self.reply = reply
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()

        def readable() -> None:
            while True:
                try:
                    data = os.read(fd, 65535)
                except BlockingIOError:
                    return
                except OSError as e:
                    log.warning("reading %s failed (%s); auto-connect disabled", self.tun_name, e)
                    if not done.done():
                        done.set_result(None)
                    return
                self.handle(data, on_demand, time.monotonic())

        loop.add_reader(fd, readable)
        try:
            await done
        finally:
            loop.remove_reader(fd)
            os.close(fd)

    def handle(self, data: bytes, on_demand: Callable[[str], None], now: float) -> None:
        pkt = parse(data)
        if pkt is None:
            return
        self._demand(pkt, on_demand, now)
        if pkt.dst.version == 6 and self.reply is not None:
            self._reject(pkt, data, now)

    def _demand(self, pkt: Packet, on_demand: Callable[[str], None], now: float) -> None:
        reason = demand_reason(pkt, self.deny_names)
        if reason is None:
            log.debug("ignored %s -> %s proto %d", pkt.src, pkt.dst, pkt.proto)
            return
        if self._last is not None and now - self._last < RATE_LIMIT_S:
            return
        if pkt.dns_name is None and self._denied_unit(pkt, now):
            return
        self._last = now
        log.info("demand: %s", reason)
        on_demand(reason)

    def _reject(self, pkt: Packet, data: bytes, now: float) -> None:
        if now - self._reject_window >= 1.0:
            self._reject_window, self._reject_count = now, 0
        if self._reject_count >= REJECTS_PER_S:
            return
        reply = reject_reply(pkt, data)
        if reply is not None:
            self._reject_count += 1
            self.reply(reply)

    def _denied_unit(self, pkt: Packet, now: float) -> bool:
        """True if the process owning the packet's source socket is in a deny_units cgroup.
        Fails open: an unknown owner counts as demand."""
        if not self.deny_units or pkt.sport is None or pkt.proto not in (TCP, UDP):
            return False
        key = (pkt.proto, pkt.sport)
        if self._denied.get(key, 0.0) > now:
            return True  # a chatty denied unit would otherwise rescan /proc for every packet
        kind = "tcp" if pkt.proto == TCP else "udp"
        tables = [kind + "6"] if pkt.src.version == 6 else [kind, kind + "6"]
        inode = None
        for t in tables:
            inode = socket_inode(_read(f"/proc/net/{t}"), pkt.src, pkt.sport)
            if inode is not None:
                break
        cgroup = find_socket_cgroup(inode) if inode is not None else None
        if cgroup is None or not (cgroup_names(cgroup) & self.deny_units):
            return False
        if len(self._denied) > 256:
            self._denied = {k: v for k, v in self._denied.items() if v > now}
        self._denied[key] = now + DENY_CACHE_S
        log.debug("ignored %s from denied unit", pkt.sport)
        return True
