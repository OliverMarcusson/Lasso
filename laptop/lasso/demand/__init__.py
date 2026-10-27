"""Demand detection: read packets from the catch-all TUN device and report user demand."""

import asyncio
import fcntl
import logging
import os
import struct
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from .packet import demand_reason, normalize_names, parse

if TYPE_CHECKING:
    from lasso.config import Config

log = logging.getLogger(__name__)

TUNSETIFF = 0x400454CA
IFF_TUN = 0x0001
IFF_NO_PI = 0x1000
RATE_LIMIT_S = 5.0


def open_tun(name: str) -> int:
    """Attach to an existing persistent TUN device. Raises OSError on failure."""
    fd = os.open("/dev/net/tun", os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        fcntl.ioctl(fd, TUNSETIFF, struct.pack("16sH", name.encode(), IFF_TUN | IFF_NO_PI))
    except OSError:
        os.close(fd)
        raise
    return fd


class DemandWatcher:
    def __init__(self, config: "Config") -> None:
        self.tun_name = config.tun_name
        self.deny_names = normalize_names(config.deny_names)
        # deny_units is enforced by the nftables ruleset (packaging/network), not here.
        self._last: float | None = None

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
        reason = demand_reason(pkt, self.deny_names)
        if reason is None:
            log.debug("ignored %s -> %s proto %d", pkt.src, pkt.dst, pkt.proto)
            return
        if self._last is not None and now - self._last < RATE_LIMIT_S:
            return
        self._last = now
        log.info("demand: %s", reason)
        on_demand(reason)
