import asyncio
from types import SimpleNamespace

import pytest

from lasso import ble
from lasso.protocol import LassoError


async def test_wait_rejects_ready_after_deadline(monkeypatch):
    clock = iter([0, 1, 2])  # deadline at 0.5; READY is dequeued at t=1, checked at t=2
    monkeypatch.setattr(ble, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    queue = asyncio.Queue()
    queue.put_nowait({"t": "READY", "v": 1})
    with pytest.raises(TimeoutError):
        await ble._wait(queue, {"READY"}, 0.5)


async def test_wait_returns_wanted_in_time():
    queue = asyncio.Queue()
    queue.put_nowait({"t": "STARTING", "v": 1})
    queue.put_nowait({"t": "READY", "v": 1})
    assert (await ble._wait(queue, {"READY"}, 1))["t"] == "READY"


async def test_connect_has_one_outer_deadline(monkeypatch):
    disconnected = []

    class SlowClient:
        def __init__(self, address, timeout):
            pass

        async def connect(self):  # discovery + connect, each within bleak's own timeout
            await asyncio.sleep(10)

        async def disconnect(self):
            disconnected.append(True)
    monkeypatch.setattr(ble, "BleakClient", SlowClient)
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(LassoError, match="not reachable"):
        async with ble._connected("AA", connect_s=0.05):
            pass
    assert loop.time() - start < 1 and disconnected
