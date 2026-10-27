"""Daemon state machine with BLE, nmcli, notifications and UDP faked."""
import asyncio

import pytest

from lasso import protocol
from lasso.config import Config
from lasso.daemon import Daemon, Observation, State
from lasso.protocol import LassoError, Ready

READY = Ready("Lasso-ab12cd", "pw", "wpa3t", "0123456789abcdef0123456789abcdef", 47123, True)


class FakeBle:
    def __init__(self):
        self.fail: str | None = None
        self.released: list[str] = []
        self.paired: tuple[str, str] | None = None

    async def request(self, address):
        if self.fail:
            raise LassoError(self.fail)
        return READY

    async def release(self, address, sid):
        self.released.append(sid)

    async def pair(self, address, hostname):
        self.paired = (address, hostname)
        return "Galaxy"


class FakeNM:
    def __init__(self):
        self.fail: str | None = None
        self.calls: list[str] = []
        self.up_ok = True
        self.obs = Observation(connectivity="none")

    async def join(self, ssid, psk, sec):
        self.calls.append(f"join {ssid} {sec}")
        if self.fail:
            raise LassoError(self.fail)
        return "192.168.43.1"

    async def down(self):
        self.calls.append("down")

    async def up(self, name):
        self.calls.append(f"up {name}")
        return self.up_ok


class FakeNotifier:
    def __init__(self):
        self.texts: list[str] = []
        self.attempts = 0

    def new_attempt(self):
        self.attempts += 1

    async def show(self, text, body="", urgency="normal"):
        self.texts.append(text)


class Pings:
    """make_ping replacement: answers come from `answers`, then repeat `default`."""
    def __init__(self):
        self.answers: list[bool] = []
        self.default = True
        self.args = None

    def __call__(self, gateway, port, sid):
        self.args = (gateway, port, sid)

        async def ping(seq):
            await asyncio.sleep(0)
            return self.answers.pop(0) if self.answers else self.default
        return ping


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    r = type("Rig", (), {})()
    r.ble, r.nm, r.notifier, r.pings, r.udp = FakeBle(), FakeNM(), FakeNotifier(), Pings(), []
    r.now = [1000.0]

    async def udp_send(gw, port, data):
        r.udp.append((gw, port, protocol.decode(data)))

    async def read_active(_):
        return True

    r.daemon = Daemon(Config(phone_address="AA:BB:CC:DD:EE:FF", phone_name="Galaxy"), ble=r.ble, nm=r.nm,
                      notifier=r.notifier, make_ping=r.pings, udp_send=udp_send, hb_interval=0.01,
                      clock=lambda: r.now[0], read_active=read_active)
    r.daemon.nm.connectivity = lambda: _const(r.nm.obs.connectivity)
    r.daemon.nm.ethernet_up = lambda: _const(r.nm.obs.ethernet_up)
    r.daemon.nm.visible_ssids = lambda: _const({"Home"} if r.nm.obs.known_wifi else set())
    r.daemon.nm.saved_wifi = lambda: _const({"Home": "Home wifi"})
    return r


async def _const(v):
    return v


async def test_connect_happy_path(rig):
    d = rig.daemon
    await d.connect()
    assert d.state is State.ACTIVE and d.gateway == "192.168.43.1"
    assert rig.nm.calls == ["join Lasso-ab12cd wpa3t"]
    assert rig.notifier.texts == ["Connecting to Galaxy…", "Connected via Galaxy"]
    assert rig.notifier.attempts == 1
    await asyncio.sleep(0.05)
    assert rig.pings.args == ("192.168.43.1", 47123, READY.sid)
    await d.disconnect()


async def test_auto_attempt_wording(rig):
    await rig.daemon.connect(auto=True)
    assert rig.notifier.texts[0] == "No network, connecting to Galaxy… retry in a moment"
    await rig.daemon.disconnect()


async def test_ble_failure_goes_idle_and_notifies(rig):
    rig.ble.fail = "Phone not reachable (Bluetooth)"
    with pytest.raises(LassoError):
        await rig.daemon.connect()
    d = rig.daemon
    assert d.state is State.IDLE
    assert rig.notifier.texts[-1] == "Hotspot failed: Phone not reachable (Bluetooth)"
    assert d.backoff.failures == 0  # manual attempts do not back off


async def test_join_failure_releases_phone_and_backs_off_for_auto(rig):
    rig.nm.fail = "Could not join hotspot"
    with pytest.raises(LassoError):
        await rig.daemon.connect(auto=True)
    assert rig.ble.released == [READY.sid]
    assert "down" in rig.nm.calls
    assert rig.daemon.state is State.IDLE
    assert not rig.daemon.backoff.allowed(rig.now[0] + 59)
    assert rig.daemon.backoff.allowed(rig.now[0] + 61)


async def test_disconnect_sends_udp_and_ble_release_and_downs_connection(rig):
    d = rig.daemon
    await d.connect()
    await d.disconnect()
    assert d.state is State.IDLE and d.session is None
    assert rig.udp == [("192.168.43.1", 47123, {"t": "RELEASE", "v": 1, "sid": READY.sid})]
    assert rig.ble.released == [READY.sid]
    assert rig.nm.calls[-1] == "down"
    assert rig.notifier.texts[-1] == "Disconnected from Galaxy"


async def test_three_missed_pongs_ends_session_without_release(rig):
    rig.pings.default = False
    d = rig.daemon
    await d.connect()
    for _ in range(100):
        if d.state is State.IDLE:
            break
        await asyncio.sleep(0.01)
    assert d.state is State.IDLE
    assert rig.notifier.texts[-1] == "Hotspot connection lost"
    assert rig.nm.calls[-1] == "down"
    assert rig.udp == [] and rig.ble.released == []
    assert not d.backoff.allowed(rig.now[0] + 30)   # no immediate auto retry


async def test_connect_twice_is_idempotent(rig):
    await rig.daemon.connect()
    await rig.daemon.connect()
    assert rig.nm.calls.count("join Lasso-ab12cd wpa3t") == 1
    await rig.daemon.disconnect()


async def test_disconnect_while_joining_cancels_and_releases(rig):
    async def slow_join(*a):
        await asyncio.sleep(10)
    rig.nm.join = slow_join
    d = rig.daemon
    task = asyncio.ensure_future(d.connect())
    for _ in range(50):
        if d.state is State.JOINING:
            break
        await asyncio.sleep(0.01)
    await d.disconnect()
    with pytest.raises((asyncio.CancelledError, LassoError)):
        await task
    assert d.state is State.IDLE
    assert rig.ble.released == [READY.sid]
    assert rig.nm.calls[-1] == "down"  # deactivates the pending `lasso` activation too


async def test_not_paired(rig):
    rig.daemon.config.phone_address = None
    with pytest.raises(LassoError, match="not paired"):
        await rig.daemon.connect()


async def test_known_wifi_while_active_switches_and_releases(rig):
    d = rig.daemon
    await d.connect()
    rig.nm.obs = Observation(connectivity="full", known_wifi=["Home wifi"])
    await d.poll_once()
    assert d.state is State.IDLE
    assert "up Home wifi" in rig.nm.calls
    assert rig.ble.released == [READY.sid]


async def test_known_wifi_switch_failure_keeps_session(rig):
    d = rig.daemon
    await d.connect()
    rig.nm.up_ok = False
    rig.nm.obs = Observation(connectivity="full", known_wifi=["Home wifi"])
    await d.poll_once()
    assert d.state is State.ACTIVE
    await d.disconnect()


async def test_demand_triggers_auto_connect_only_when_gated_open(rig):
    d = rig.daemon
    rig.nm.obs = Observation(connectivity="none")
    await d._demand("dns:example.com")           # first sample starts the offline period
    assert d.state is State.IDLE
    rig.now[0] += 25
    await d._demand("dns:example.com")
    assert d.state is State.ACTIVE
    assert rig.notifier.texts[0].startswith("No network, connecting")
    await d.disconnect()


async def test_demand_ignored_after_manual_disconnect(rig):
    d = rig.daemon
    rig.nm.obs = Observation(connectivity="none")
    await d._demand("x")
    rig.now[0] += 25
    d.tracker.mark_manual(rig.now[0])
    await d._demand("x")
    assert d.state is State.IDLE


async def test_failed_auto_attempt_then_backoff_blocks_demand(rig):
    d = rig.daemon
    rig.nm.obs = Observation(connectivity="none")
    await d._demand("x")
    rig.now[0] += 25
    rig.ble.fail = "Phone not reachable (Bluetooth)"
    await d._demand("x")
    assert rig.notifier.texts[-1].startswith("Hotspot failed")
    n = len(rig.notifier.texts)
    rig.now[0] += 30
    await d._demand("x")
    assert len(rig.notifier.texts) == n          # backing off
    rig.now[0] += 31
    await d._demand("x")
    assert len(rig.notifier.texts) > n


async def test_sleep_releases_and_resume_reconnects(rig, monkeypatch):
    monkeypatch.setattr("lasso.daemon.RESUME_DELAY_S", 0)
    d = rig.daemon
    await d.connect()
    await d._on_sleep(True, None)
    assert d.state is State.IDLE
    assert rig.udp and rig.ble.released == []    # UDP only, BLE would delay suspend

    async def no_inhibit(bus):
        pass
    d._take_inhibitor = no_inhibit
    await d._on_sleep(False, None)
    assert d.state is State.ACTIVE
    await d.disconnect()


async def test_resume_without_prior_session_stays_idle(rig, monkeypatch):
    monkeypatch.setattr("lasso.daemon.RESUME_DELAY_S", 0)

    async def no_inhibit(bus):
        pass
    rig.daemon._take_inhibitor = no_inhibit
    await rig.daemon._on_sleep(True, None)
    await rig.daemon._on_sleep(False, None)
    assert rig.daemon.state is State.IDLE


async def test_pair_saves_config(rig, tmp_path):
    from lasso import config
    d = rig.daemon
    d.config.phone_address = None
    resp = await d.handle({"cmd": "pair", "address": "11:22:33:44:55:66"})
    assert resp["ok"] and d.config.phone_address == "11:22:33:44:55:66"
    assert rig.ble.paired[0] == "11:22:33:44:55:66"
    assert config.load().phone_address == "11:22:33:44:55:66"
    assert d.state is State.IDLE
    assert any("Pairing" in t for t in rig.notifier.texts)


async def test_pair_without_device_errors(rig):
    rig.daemon.config.phone_address = None
    resp = await rig.daemon.handle({"cmd": "pair"})
    assert resp["ok"] is False and "lasso pair <address>" in resp["error"]


async def test_handle_commands(rig):
    d = rig.daemon
    assert (await d.handle({"cmd": "status"}))["state"] == "IDLE"
    assert (await d.handle({"cmd": "connect"}))["state"] == "ACTIVE"
    assert (await d.handle({"cmd": "disconnect"}))["state"] == "IDLE"
    assert d.tracker.manual is True
    assert (await d.handle({"cmd": "nope"}))["ok"] is False
    rig.ble.fail = "Phone not reachable (Bluetooth)"
    resp = await d.handle({"cmd": "connect"})
    assert resp == {"ok": False, "error": "Phone not reachable (Bluetooth)"}
    assert d.tracker.manual is False


# ---- review regressions: lifecycle serialization ----

from types import SimpleNamespace  # noqa: E402

from dbus_fast import MessageType  # noqa: E402


async def _no_inhibit(bus):
    pass


async def test_inhibitor_keeps_the_received_fd_not_the_index(rig, monkeypatch):
    closed = []
    monkeypatch.setattr("lasso.daemon.os.close", closed.append)
    reply = SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=[0], unix_fds=[987])
    await rig.daemon._take_inhibitor(SimpleNamespace(call=lambda m: _const(reply)))
    rig.daemon._drop_inhibitor()
    assert closed == [987]
    bad = SimpleNamespace(message_type=MessageType.ERROR, error_name="x", body=["no"], unix_fds=[])
    await rig.daemon._take_inhibitor(SimpleNamespace(call=lambda m: _const(bad)))
    assert rig.daemon._inhibit_fd is None


async def test_stale_network_switch_leaves_newer_session_alone(rig):
    d = rig.daemon
    await d.connect()
    started, finish = asyncio.Event(), asyncio.Event()

    async def up(name):
        started.set()
        await finish.wait()
        return True
    rig.nm.up = up
    switch = asyncio.ensure_future(d._switch_to_known(Observation(known_wifi=["Home"])))
    await started.wait()
    await d.disconnect()
    await d.connect()
    finish.set()
    await switch
    assert d.state is State.ACTIVE and d.session is not None
    assert len(rig.ble.released) == 1
    await d.disconnect()


async def test_disconnect_cancels_pairing(rig):
    d = rig.daemon
    started = asyncio.Event()

    async def pair(*a):
        started.set()
        await asyncio.Event().wait()
    rig.ble.pair = pair
    task = asyncio.ensure_future(d.handle({"cmd": "pair", "address": "11:22:33:44:55:66"}))
    await started.wait()
    assert (await d.handle({"cmd": "connect"}))["ok"] is False   # busy while pairing
    await d.disconnect()
    assert (await task) == {"ok": False, "error": "cancelled"}
    await d.connect()
    await asyncio.sleep(0.01)
    assert d.state is State.ACTIVE and d.config.phone_address == "AA:BB:CC:DD:EE:FF"
    await d.disconnect()


async def test_manual_disconnect_beats_pending_resume(rig, monkeypatch):
    d = rig.daemon
    monkeypatch.setattr("lasso.daemon.RESUME_DELAY_S", 0.03)
    await d.connect()
    await d._on_sleep(True, None)
    d._take_inhibitor = _no_inhibit
    task = asyncio.ensure_future(d._on_sleep(False, None))
    await asyncio.sleep(0)
    await d.handle({"cmd": "disconnect"})
    await task
    assert d.state is State.IDLE and rig.nm.calls.count("join Lasso-ab12cd wpa3t") == 1


async def test_connect_and_demand_blocked_while_suspended(rig):
    d = rig.daemon
    await d._on_sleep(True, None)
    with pytest.raises(LassoError, match="suspend"):
        await d.connect()
    rig.now[0] += 100
    await d._demand("x")
    assert d.state is State.IDLE and rig.nm.calls == ["down"]
    d._take_inhibitor = _no_inhibit
    await d._on_sleep(False, None)
    await d.connect()
    assert d.state is State.ACTIVE
    await d.disconnect()


async def test_sleep_while_joining_releases_over_ble_within_budget(rig, monkeypatch):
    monkeypatch.setattr("lasso.daemon.SLEEP_BLE_RELEASE_S", 0.05)
    d = rig.daemon
    started = asyncio.Event()

    async def join(*a):
        started.set()
        await asyncio.Event().wait()
    rig.nm.join = join
    task = asyncio.ensure_future(d.connect())
    await started.wait()
    await d._on_sleep(True, None)
    assert rig.ble.released == [READY.sid] and rig.udp == []
    assert d.session is None and d.state is State.IDLE
    with pytest.raises(asyncio.CancelledError):
        await task

    # A BLE RELEASE that hangs must not hold up the sleep beyond the budget.
    async def stuck_release(address, sid):
        await asyncio.sleep(10)
    rig.ble.release = stuck_release
    d._take_inhibitor = _no_inhibit
    await d._on_sleep(False, None)
    task = asyncio.ensure_future(d.connect())
    await asyncio.sleep(0.01)
    await asyncio.wait_for(d._on_sleep(True, None), 1)
    with pytest.raises(asyncio.CancelledError):
        await task


async def _wait_state(d, state):
    for _ in range(200):
        if d.state is state:
            return
        await asyncio.sleep(0.005)


async def test_heartbeat_loss_latches_auto_connect_until_network_change(rig):
    d = rig.daemon
    rig.pings.default = False
    await d._demand("x")
    rig.now[0] += 25
    await d._demand("x")
    await _wait_state(d, State.IDLE)
    assert d.detail == "heartbeat lost"
    rig.pings.default = True
    rig.now[0] += 1000                            # well past any backoff
    await d._demand("x")
    await d._demand("x")
    assert d.state is State.IDLE                  # no automatic retry in v1
    rig.nm.obs = Observation(connectivity="none", ethernet_up=True)
    await d.poll_once()                           # ethernet changed: latch off (ethernet itself still blocks)
    rig.nm.obs = Observation(connectivity="none")
    rig.now[0] += 25
    await d._demand("x")
    assert d.state is State.ACTIVE
    await d.disconnect()


async def test_explicit_connect_clears_heartbeat_latch(rig):
    d = rig.daemon
    rig.pings.default = False
    await d.connect()
    await _wait_state(d, State.IDLE)
    rig.pings.default = True
    assert (await d.handle({"cmd": "connect"}))["state"] == "ACTIVE"
    assert d._hb_lost is False
    await d.disconnect()
