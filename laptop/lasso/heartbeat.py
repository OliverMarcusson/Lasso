"""UDP heartbeat client: PING the phone every 30 s, give up after 3 missed PONGs."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from lasso import protocol

PONG_WAIT_S = 5

# A ping function sends one PING with the given seq and says whether a matching PONG came back.
PingFn = Callable[[int], Awaitable[bool]]


class Heartbeat:
    def __init__(self, ping: PingFn, interval: float = protocol.HEARTBEAT_INTERVAL_S,
                 max_missed: int = protocol.MAX_MISSED) -> None:
        self._ping = ping
        self.interval = interval
        self.max_missed = max_missed
        self.missed = 0
        self.seq = 0

    @property
    def dead(self) -> bool:
        return self.missed >= self.max_missed

    async def beat(self) -> bool:
        """One PING. Returns True if answered. A PONG resets the miss counter."""
        self.seq += 1
        ok = await self._ping(self.seq)
        self.missed = 0 if ok else self.missed + 1
        return ok

    async def run(self) -> None:
        """Beat until the session is dead, then return. The first PING goes out immediately."""
        while True:
            await self.beat()
            if self.dead:
                return
            await asyncio.sleep(self.interval)


class _PongWaiter(asyncio.DatagramProtocol):
    def __init__(self, sid: str, seq: int, done: asyncio.Future[bool]) -> None:
        self.sid, self.seq, self.done = sid, seq, done

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        msg = protocol.decode(data)
        if msg and msg["t"] == "PONG" and msg.get("sid") == self.sid and msg.get("seq") == self.seq \
                and not self.done.done():
            self.done.set_result(True)


def udp_ping(gateway: str, port: int, sid: str) -> PingFn:
    async def ping(seq: int) -> bool:
        loop = asyncio.get_running_loop()
        done: asyncio.Future[bool] = loop.create_future()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _PongWaiter(sid, seq, done), remote_addr=(gateway, port))
        except OSError:
            return False
        try:
            transport.sendto(protocol.encode("PING", sid=sid, seq=seq))
            await asyncio.wait_for(done, PONG_WAIT_S)
            return True
        except (TimeoutError, OSError):
            return False
        finally:
            transport.close()
    return ping


async def send_udp(gateway: str, port: int, data: bytes) -> None:
    """Fire one datagram (used for RELEASE). Never raises."""
    try:
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            asyncio.DatagramProtocol, remote_addr=(gateway, port))
    except OSError:
        return
    transport.sendto(data)
    transport.close()
