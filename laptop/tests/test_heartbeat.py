import asyncio

from lasso import protocol
from lasso.heartbeat import Heartbeat, send_udp, udp_ping


def scripted(answers):
    calls = []

    async def ping(seq):
        calls.append(seq)
        return answers[len(calls) - 1]
    return ping, calls


async def test_three_misses_is_dead():
    ping, calls = scripted([False, False, False, True])
    hb = Heartbeat(ping, interval=0)
    await hb.run()
    assert hb.dead and hb.missed == 3 and calls == [1, 2, 3]


async def test_pong_resets_miss_counter():
    ping, calls = scripted([False, False, True, False, False, False, True])
    hb = Heartbeat(ping, interval=0)
    await hb.run()
    assert calls == [1, 2, 3, 4, 5, 6]


async def test_first_ping_is_immediate_and_not_dead_before_limit():
    ping, _ = scripted([False])
    hb = Heartbeat(ping, interval=3600)
    assert await hb.beat() is False
    assert hb.missed == 1 and not hb.dead


class FakePhone(asyncio.DatagramProtocol):
    def __init__(self, sid, reply=True):
        self.sid, self.reply, self.got = sid, reply, []

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        msg = protocol.decode(data)
        self.got.append(msg)
        if self.reply and msg["t"] == "PING" and msg["sid"] == self.sid:
            self.transport.sendto(protocol.encode("PONG", sid=msg["sid"], seq=msg["seq"]), addr)


async def start_phone(sid, reply=True):
    transport, phone = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: FakePhone(sid, reply), local_addr=("127.0.0.1", 0))
    return transport, phone, transport.get_extra_info("sockname")[1]


async def test_udp_ping_roundtrip():
    transport, phone, port = await start_phone("s1")
    try:
        assert await udp_ping("127.0.0.1", port, "s1")(7) is True
        assert phone.got[0] == {"t": "PING", "v": 1, "sid": "s1", "seq": 7}
    finally:
        transport.close()


async def test_udp_ping_wrong_sid_gets_no_reply(monkeypatch):
    monkeypatch.setattr("lasso.heartbeat.PONG_WAIT_S", 0.2)
    transport, _, port = await start_phone("s1")
    try:
        assert await udp_ping("127.0.0.1", port, "other")(1) is False
    finally:
        transport.close()


async def test_send_udp_delivers_release():
    transport, phone, port = await start_phone("s1", reply=False)
    try:
        await send_udp("127.0.0.1", port, protocol.encode("RELEASE", sid="s1"))
        await asyncio.sleep(0.1)
        assert phone.got == [{"t": "RELEASE", "v": 1, "sid": "s1"}]
    finally:
        transport.close()
