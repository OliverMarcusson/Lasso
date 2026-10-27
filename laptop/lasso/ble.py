"""BLE central side: talk to the phone's GATT server through bleak (BlueZ)."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from lasso import protocol
from lasso.protocol import LassoError, PhoneError, Ready

log = logging.getLogger("lasso.ble")

PAIR_WAIT_S = 60       # the user has to compare the numeric codes
RELEASE_CONNECT_S = 5  # best-effort RELEASE must not hold up teardown

Queue = asyncio.Queue[dict]


@asynccontextmanager
async def _connected(address: str, *, pair_first: bool = False, connect_s: float = protocol.BLE_CONNECT_S
                     ) -> AsyncIterator[tuple[BleakClient, Queue]]:
    """Connect, subscribe to the events characteristic, always disconnect on exit.

    BlueZ negotiates the ATT MTU itself; all messages fit the default (185) anyway.
    """
    queue: Queue = asyncio.Queue()

    def on_event(_char: object, data: bytearray) -> None:
        msg = protocol.decode(bytes(data))
        if msg is not None:
            queue.put_nowait(msg)

    client = BleakClient(address, timeout=connect_s)
    try:
        # bleak may spend its timeout on discovery and then again on connecting: bound the whole thing.
        async with asyncio.timeout(connect_s):
            await client.connect()
    except (BleakError, OSError, TimeoutError) as e:
        log.info("BLE connect failed: %s", e)
        with suppress(Exception):
            await asyncio.wait_for(client.disconnect(), RELEASE_CONNECT_S)
        raise LassoError("Phone not reachable (Bluetooth)") from e
    try:
        if pair_first:
            try:
                await client.pair()
            except (BleakError, NotImplementedError, OSError) as e:
                # Already bonded, or the agent said no; the PAIR exchange tells which.
                log.warning("pair(): %s", e)
        await client.start_notify(protocol.EVENTS_UUID, on_event)
        yield client, queue
    except (BleakError, OSError) as e:
        raise LassoError(f"Bluetooth error: {e}") from e
    finally:
        with suppress(Exception):
            await client.disconnect()


async def _send(client: BleakClient, data: bytes) -> None:
    await client.write_gatt_char(protocol.CONTROL_UUID, data, response=True)


async def _wait(queue: Queue, wanted: set[str], timeout: float) -> dict:
    """Next message whose type is in `wanted`, within `timeout` overall (TimeoutError otherwise).
    ERROR raises PhoneError, other types are skipped."""
    deadline = time.monotonic() + timeout
    while True:
        msg = await asyncio.wait_for(queue.get(), max(deadline - time.monotonic(), 0))
        if time.monotonic() > deadline:  # queued messages do not wait, so check: a late answer is no answer
            raise TimeoutError
        if msg["t"] == "ERROR":
            raise PhoneError(str(msg.get("code", "UNKNOWN")))
        if msg["t"] in wanted:
            return msg


class Ble:
    async def request(self, address: str) -> Ready:
        """REQUEST a hotspot; returns the credentials from READY. BLE is dropped afterwards."""
        async with _connected(address) as (client, queue):
            await _send(client, protocol.encode("REQUEST"))
            try:
                msg = await _wait(queue, {"READY"}, protocol.READY_S)
            except PhoneError as e:
                raise LassoError(f"Phone could not start hotspot: {e.text}") from e
            except TimeoutError as e:
                raise LassoError("Phone could not start hotspot: no answer") from e
            return Ready.from_message(msg)

    async def release(self, address: str, sid: str) -> None:
        """Best effort RELEASE over BLE; never raises."""
        try:
            async with _connected(address, connect_s=RELEASE_CONNECT_S) as (client, _queue):
                await _send(client, protocol.encode("RELEASE", sid=sid))
        except Exception as e:  # noqa: BLE001 - best effort by definition
            log.info("BLE RELEASE failed: %s", e)

    async def pair(self, address: str, hostname: str) -> str:
        """Bond (user confirms the numeric comparison in the desktop agent), PAIR, wait for PAIRED."""
        async with _connected(address, pair_first=True) as (client, queue):
            await _send(client, protocol.encode("PAIR", name=hostname[:60]))
            try:
                msg = await _wait(queue, {"PAIRED"}, PAIR_WAIT_S)
            except PhoneError as e:
                raise LassoError(f"Pairing failed: {e.text}") from e
            except TimeoutError as e:
                raise LassoError("Pairing failed: the phone did not answer (is its pairing screen open?)") from e
            return str(msg.get("name") or address)


async def scan(timeout: float = 8.0) -> list[tuple[str, str]]:
    """Nearby devices advertising the Lasso service as (address, name)."""
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    want = protocol.SERVICE_UUID
    return [(dev.address, dev.name or adv.local_name or "?")
            for dev, adv in found.values() if want in [u.lower() for u in adv.service_uuids]]
