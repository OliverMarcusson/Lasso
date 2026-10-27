"""lassod: session state machine, auto-connect gating, sleep handling, control socket."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from lasso import config as config_mod
from lasso import control, protocol
from lasso.ble import Ble
from lasso.config import Config
from lasso.heartbeat import Heartbeat, PingFn, send_udp, udp_ping
from lasso.nm import NM, known_connections
from lasso.notify import Notifier
from lasso.protocol import LassoError

try:
    from lasso.demand import DemandWatcher
except ImportError:  # the daemon works without auto-connect
    DemandWatcher = None  # type: ignore[assignment,misc]

log = logging.getLogger("lasso")

POLL_S = 5
MANUAL_GUARD_S = 5      # ignore a stale "full" right after a manual disconnect
BACKOFF_STEPS_S = (60, 300, 900)
RESUME_DELAY_S = 3      # let Bluetooth and Wi-Fi come back before reconnecting
SLEEP_BLE_RELEASE_S = 3  # BLE RELEASE before sleep must fit logind's delay budget (InhibitDelayMaxSec, 5 s)


class State(StrEnum):
    IDLE = "IDLE"
    REQUESTING = "REQUESTING"
    JOINING = "JOINING"
    ACTIVE = "ACTIVE"
    PAIRING = "PAIRING"


# ---- gating: small pure pieces, spec section 7 ----

@dataclass
class Observation:
    connectivity: str = "unknown"
    ethernet_up: bool = False
    known_wifi: list[str] = field(default_factory=list)  # saved autoconnect connections whose SSID is visible
    user_active: bool = True


class OfflineTracker:
    """Tracks the current offline period (condition 1) and a manual disconnect inside it (condition 3)."""

    def __init__(self) -> None:
        self.offline_since: float | None = None
        self.manual = False
        self._manual_at = -MANUAL_GUARD_S

    def mark_manual(self, now: float) -> None:
        self.manual = True
        self._manual_at = now

    def update(self, connectivity: str, now: float, lasso_active: bool) -> bool:
        """Feed one connectivity sample. Returns True when a real (non-Lasso) network just came back."""
        if connectivity != "full":
            if self.offline_since is None:
                self.offline_since = now
            return False
        was_offline = self.offline_since is not None
        self.offline_since = None
        if lasso_active:
            return False
        if now - self._manual_at >= MANUAL_GUARD_S:
            self.manual = False  # network state changed, the manual disconnect is over
        return was_offline


class Backoff:
    """After a failed auto attempt: wait 1 min, then 5 min, then 15 min (stays at 15)."""

    def __init__(self) -> None:
        self.failures = 0
        self.until = 0.0

    def failed(self, now: float) -> None:
        self.until = now + BACKOFF_STEPS_S[min(self.failures, len(BACKOFF_STEPS_S) - 1)]
        self.failures += 1

    def allowed(self, now: float) -> bool:
        return now >= self.until

    def reset(self) -> None:
        self.failures = 0
        self.until = 0.0


def auto_connect_block(obs: Observation, tracker: OfflineTracker, backoff: Backoff, now: float,
                       grace_s: float, state: State) -> str | None:
    """Why an auto-connect must not happen right now (conditions 1-4), or None if it may."""
    if state is not State.IDLE:
        return f"busy ({state})"
    if tracker.offline_since is None or now - tracker.offline_since < grace_s:
        return "not offline long enough"
    if obs.ethernet_up:
        return "ethernet is up"
    if obs.known_wifi:
        return "known Wi-Fi in range"
    if tracker.manual:
        return "manual disconnect"
    if not obs.user_active:
        return "user idle or locked"
    if not backoff.allowed(now):
        return "backing off"
    return None


def user_active(props: dict[str, str], now_us: int, idle_minutes: float) -> bool:
    """From logind session properties: unlocked, and not idle for longer than idle_minutes."""
    if props.get("LockedHint") == "yes":
        return False
    if props.get("IdleHint") == "yes":
        since = int(props.get("IdleSinceHint") or 0)
        return since == 0 or now_us - since <= idle_minutes * 60_000_000
    return True


async def _loginctl(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "loginctl", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return out.decode()


async def read_user_active(idle_minutes: float) -> bool:
    """Ask logind about our session. If anything is unavailable, assume the user is active."""
    try:
        sid = os.environ.get("XDG_SESSION_ID")
        if not sid:
            out = await _loginctl("show-user", str(os.getuid()), "-p", "Display", "--value")
            sid = out.strip()
        if not sid:
            return True
        out = await _loginctl("show-session", sid, "-p", "IdleHint", "-p", "LockedHint", "-p", "IdleSinceHint")
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        return user_active(props, int(time.time() * 1_000_000), idle_minutes)
    except (OSError, ValueError):
        return True


# ---- the daemon ----

class Daemon:
    def __init__(self, config: Config, *, ble: Any = None, nm: Any = None, notifier: Any = None,
                 make_ping: Callable[[str, int, str], PingFn] = udp_ping,
                 udp_send: Callable[[str, int, bytes], Awaitable[None]] = send_udp,
                 hb_interval: float = protocol.HEARTBEAT_INTERVAL_S,
                 clock: Callable[[], float] = time.monotonic,
                 read_active: Callable[[float], Awaitable[bool]] = read_user_active) -> None:
        self.config = config
        self.ble = ble or Ble()
        self.nm = nm or NM()
        self.notifier = notifier or Notifier()
        self.make_ping = make_ping
        self.udp_send = udp_send
        self.hb_interval = hb_interval
        self.clock = clock
        self.read_active = read_active

        self.state = State.IDLE
        self.detail = ""
        self.session: protocol.Ready | None = None
        self.gateway: str | None = None
        self.tracker = OfflineTracker()
        self.backoff = Backoff()
        # Lifecycle: connect and pair run as the one `_op` task. Anything that ends a session (disconnect,
        # sleep, heartbeat loss, network switch) bumps `_gen`, cancels `_op`, then mutates under `_lock`.
        # Work that awaited outside the lock rechecks `_gen` before touching state.
        self._lock = asyncio.Lock()
        self._gen = 0
        self._op: asyncio.Task | None = None
        self._hb_task: asyncio.Task | None = None
        self._suspended = False
        self._was_active_before_sleep = False
        # After heartbeat loss there is no automatic retry (spec 7) until `lasso connect` or a network change.
        self._hb_lost = False
        self._hb_lost_net: tuple | None = None
        self._inhibit_fd: int | None = None
        self._tasks: set[asyncio.Task] = set()

    # -- small helpers --

    def _set(self, state: State, detail: str = "") -> None:
        self.state, self.detail = state, detail
        log.info("state %s %s", state, detail)

    async def _notify(self, text: str) -> None:
        await self.notifier.show(text)

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _start_op(self, state: State, detail: str, make: Callable[[int], Awaitable[None]]) -> None:
        """Start connect or pair as `_op` if nothing else is going on. `make` gets the new generation."""
        if self._suspended:
            raise LassoError("suspending or resuming, try again in a moment")
        if self.state is not State.IDLE or self._lock.locked() or (self._op and not self._op.done()):
            raise LassoError(f"busy: {self.state}")
        self._gen += 1
        self._set(state, detail)
        self._op = asyncio.ensure_future(make(self._gen))

    async def _cancel_op(self) -> None:
        task, self._op = self._op, None
        if task and not task.done():
            task.cancel()
            await asyncio.wait([task])

    # -- control commands --

    async def handle(self, req: dict[str, Any]) -> dict[str, Any]:
        cmd = req.get("cmd")
        try:
            if cmd == "status":
                pass
            elif cmd == "connect":
                self.tracker.manual = False  # an explicit connect overrides an earlier manual disconnect
                self._hb_lost = False
                await self.connect()
            elif cmd == "disconnect":
                self.tracker.mark_manual(self.clock())
                await self.disconnect()
            elif cmd == "pair":
                await self.pair(req.get("address"))
            else:
                return {"ok": False, "error": f"unknown command {cmd!r}"}
        except LassoError as e:
            return {"ok": False, "error": str(e)}
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            return {"ok": False, "error": "cancelled"}  # the connect/pair op was cancelled, not us
        return {"ok": True, "state": str(self.state), "detail": self.detail}

    # -- connect / disconnect --

    async def connect(self, auto: bool = False) -> None:
        if self._suspended:
            raise LassoError("suspending or resuming, try again in a moment")
        if self.state is State.ACTIVE:
            return
        if self.state not in (State.REQUESTING, State.JOINING) or not self._op or self._op.done():
            if not self.config.phone_address:
                raise LassoError("not paired yet: run `lasso pair <address>` (see `lasso scan`)")
            self._start_op(State.REQUESTING, "", lambda gen: self._connect(auto, gen))
        await asyncio.shield(self._op)

    async def _connect(self, auto: bool, gen: int) -> None:
        """Runs as `_op`; disconnect and sleep cancel it, so no other lifecycle step interleaves."""
        address = self.config.phone_address
        phone = self.config.phone_name
        self.notifier.new_attempt()
        try:
            await self._notify(f"No network, connecting to {phone}… retry in a moment" if auto
                               else f"Connecting to {phone}…")
            self.session = await self.ble.request(address)
            self._set(State.JOINING, self.session.ssid)
            self.gateway = await self.nm.join(self.session.ssid, self.session.psk, self.session.sec)
        except Exception as e:  # noqa: BLE001 - whatever went wrong, end up in IDLE with a message
            if not isinstance(e, LassoError):
                log.exception("unexpected error while connecting")
                e = LassoError(f"{type(e).__name__}: {e}")
            # Tell the phone to stop its hotspot (if it started one), undo our profile. Shielded so a
            # disconnect arriving now does not cut the RELEASE short (it already took the session).
            await asyncio.shield(self._teardown())
            if auto:
                self.backoff.failed(self.clock())
            self._set(State.IDLE, str(e))
            await self._notify(f"Hotspot failed: {e}")
            raise e
        self.backoff.reset()
        self._set(State.ACTIVE, f"via {phone}, gateway {self.gateway}")
        self._hb_task = asyncio.ensure_future(self._heartbeat(self.session, self.gateway, gen))
        await self._notify(f"Connected via {phone}")

    async def disconnect(self) -> None:
        was_busy = self.state is not State.IDLE
        self._gen += 1  # also stops a pending resume reconnect
        await self._cancel_op()
        async with self._lock:
            await self._teardown()
            self._set(State.IDLE, "disconnected")
        if was_busy:
            await self._notify(f"Disconnected from {self.config.phone_name}")

    async def _teardown(self, *, udp: bool = True, ble: bool = True, ble_timeout: float | None = None) -> None:
        """Stop the heartbeat, RELEASE the session, take the `lasso` connection down."""
        hb, self._hb_task = self._hb_task, None
        if hb and hb is not asyncio.current_task():
            hb.cancel()
            with suppress(asyncio.CancelledError):
                await hb
        session, gateway = self.session, self.gateway
        self.session = self.gateway = None
        jobs = [self.nm.down()]
        if session:
            if udp and gateway:
                await self.udp_send(gateway, session.port, protocol.encode("RELEASE", sid=session.sid))
            if ble and self.config.phone_address:
                jobs.append(asyncio.wait_for(self.ble.release(self.config.phone_address, session.sid), ble_timeout))
        await asyncio.gather(*jobs, return_exceptions=True)

    # -- heartbeat --

    async def _heartbeat(self, session: protocol.Ready, gateway: str, gen: int) -> None:
        await Heartbeat(self.make_ping(gateway, session.port, session.sid), self.hb_interval).run()
        # run() only returns when the session is dead.
        async with self._lock:
            if gen != self._gen:
                return
            self._gen += 1
            log.warning("heartbeat lost")
            await self._teardown(udp=False, ble=False)
            self.backoff.failed(self.clock())
            self._hb_lost, self._hb_lost_net = True, None  # no automatic retry until the network changes
            self._set(State.IDLE, "heartbeat lost")
        await self._notify("Hotspot connection lost")

    # -- pairing --

    async def pair(self, address: str | None) -> None:
        address = address or self.config.phone_address
        if not address:
            raise LassoError("pairing needs a device: lasso pair <address> (see `lasso scan`)")
        self._start_op(State.PAIRING, address, lambda _gen: self._pair(address))
        await asyncio.shield(self._op)

    async def _pair(self, address: str) -> None:
        """Runs as `_op`; a disconnect cancels it and owns the state from then on."""
        self.notifier.new_attempt()
        try:
            await self._notify("Pairing: open the pairing screen in the Lasso app and confirm the numeric code")
            name = await self.ble.pair(address, socket.gethostname())
        except Exception as e:  # noqa: BLE001 - not cancellation: we still own the state
            self._set(State.IDLE, str(e))
            await self._notify(str(e))
            raise
        config_mod.save_phone(address, name)
        self.config.phone_address, self.config.phone_name = address, name
        self._set(State.IDLE, f"paired with {name}")
        await self._notify(f"Paired with {name}")

    # -- network observation and auto-connect --

    async def observe(self) -> Observation:
        connectivity, eth, visible, saved, active = await asyncio.gather(
            self.nm.connectivity(), self.nm.ethernet_up(), self.nm.visible_ssids(), self.nm.saved_wifi(),
            self.read_active(self.config.idle_minutes))
        return Observation(connectivity, eth, known_connections(visible, saved), active)

    def _note(self, obs: Observation, now: float, lasso_active: bool) -> None:
        """Feed an observation to the offline tracker and the heartbeat-loss latch."""
        if self.tracker.update(obs.connectivity, now, lasso_active):
            self.backoff.reset()
            self._hb_lost = False  # a real network came back
        if self._hb_lost:
            net = (tuple(obs.known_wifi), obs.ethernet_up)
            if self._hb_lost_net is None:
                self._hb_lost_net = net
            elif net != self._hb_lost_net:
                self._hb_lost = False  # known Wi-Fi or ethernet changed

    async def poll_once(self) -> None:
        obs = await self.observe()
        active = self.state is State.ACTIVE
        self._note(obs, self.clock(), active)
        if active and (obs.known_wifi or obs.ethernet_up):
            await self._switch_to_known(obs)

    async def _switch_to_known(self, obs: Observation) -> None:
        """A better network showed up while on the hotspot: join it and release the phone."""
        gen = self._gen
        if obs.known_wifi and not obs.ethernet_up:
            log.info("known Wi-Fi %s in range, leaving the hotspot", obs.known_wifi[0])
            if not await self.nm.up(obs.known_wifi[0]):
                return
        async with self._lock:
            if gen != self._gen or self.state is not State.ACTIVE:
                return  # the session we meant to leave is gone already
            self._gen += 1
            await self._teardown()
            self._set(State.IDLE, "switched to a known network")
        await self._notify(f"Switched to a known network, released {self.config.phone_name}")

    async def watch(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 - a bad poll must not kill the daemon
                log.exception("poll failed")
            await asyncio.sleep(POLL_S)

    def on_demand(self, reason: str) -> None:
        """Called by DemandWatcher for each qualifying packet."""
        self._spawn(self._demand(reason))

    async def _demand(self, reason: str) -> None:
        if (not self.config.auto_connect or not self.config.phone_address or self.state is not State.IDLE
                or self._suspended):
            return
        obs = await self.observe()
        now = self.clock()
        self._note(obs, now, False)
        why = auto_connect_block(obs, self.tracker, self.backoff, now, self.config.offline_grace_s, self.state)
        if not why and self._hb_lost:
            why = "heartbeat lost, waiting for `lasso connect` or a network change"
        if why:
            log.debug("demand %s ignored: %s", reason, why)
            return
        log.info("auto-connect for %s", reason)
        with suppress(LassoError):
            await self.connect(auto=True)

    # -- logind sleep --

    async def watch_sleep(self) -> None:
        from dbus_fast import BusType, Message, MessageType
        from dbus_fast.aio import MessageBus

        bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()

        def on_message(msg: Message) -> None:
            if msg.message_type is MessageType.SIGNAL and msg.member == "PrepareForSleep":
                self._spawn(self._on_sleep(bool(msg.body[0]), bus))

        bus.add_message_handler(on_message)
        await bus.call(Message(
            destination="org.freedesktop.DBus", path="/org/freedesktop/DBus", interface="org.freedesktop.DBus",
            member="AddMatch", signature="s",
            body=["type='signal',sender='org.freedesktop.login1',member='PrepareForSleep'"]))
        await self._take_inhibitor(bus)
        await bus.wait_for_disconnect()

    async def _take_inhibitor(self, bus: Any) -> None:
        """A delay lock gives us the few seconds logind allows to send RELEASE before sleeping."""
        from dbus_fast import Message, MessageType
        try:
            reply = await bus.call(Message(
                destination="org.freedesktop.login1", path="/org/freedesktop/login1",
                interface="org.freedesktop.login1.Manager", member="Inhibit", signature="ssss",
                body=["sleep", "Lasso", "Release the phone hotspot before sleep", "delay"]))
            if reply.message_type is not MessageType.METHOD_RETURN:
                raise RuntimeError(f"{reply.error_name}: {reply.body}")
            # With fd passing, the body holds an index into unix_fds, not the descriptor itself.
            index = reply.body[0] if len(reply.body) == 1 else None
            if not isinstance(index, int) or not 0 <= index < len(reply.unix_fds):
                raise RuntimeError(f"unexpected Inhibit reply {reply.body!r}")
            self._inhibit_fd = reply.unix_fds[index]
        except Exception as e:  # noqa: BLE001
            log.warning("no sleep inhibitor: %s", e)

    def _drop_inhibitor(self) -> None:
        fd, self._inhibit_fd = self._inhibit_fd, None
        if fd is not None:
            with suppress(OSError):
                os.close(fd)

    async def _on_sleep(self, sleeping: bool, bus: Any) -> None:
        if sleeping:
            self._suspended = True  # blocks connect, pair and demand until resume
            self._gen += 1
            try:
                was_active = self.state is State.ACTIVE
                await self._cancel_op()
                async with self._lock:
                    self._was_active_before_sleep = was_active
                    # With a gateway, UDP RELEASE suffices. Still joining (credentials, no gateway): BLE is
                    # the only way to reach the phone, bounded to fit logind's delay budget.
                    joining = self.session is not None and not self.gateway
                    await self._teardown(ble=joining, ble_timeout=SLEEP_BLE_RELEASE_S)
                    self._set(State.IDLE, "sleeping")
            finally:
                self._drop_inhibitor()
            return
        self._suspended = False
        await self._take_inhibitor(bus)
        if self._was_active_before_sleep:
            self._was_active_before_sleep = False
            gen = self._gen
            await asyncio.sleep(RESUME_DELAY_S)
            if gen != self._gen:
                return  # a disconnect (or another sleep) happened meanwhile and wins
            with suppress(LassoError):
                await self.connect()


async def _logged(name: str, coro: Awaitable[None]) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("%s stopped", name)


async def run(config: Config) -> None:
    daemon = Daemon(config)
    server = await control.serve(daemon.handle)
    background = [asyncio.ensure_future(_logged("poll", daemon.watch())),
                  asyncio.ensure_future(_logged("sleep watcher", daemon.watch_sleep()))]
    if DemandWatcher is not None and config.auto_connect:
        background.append(asyncio.ensure_future(_logged("demand watcher", DemandWatcher(config).run(daemon.on_demand))))
    elif DemandWatcher is None:
        log.warning("lasso.demand is not available, auto-connect is off")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    log.info("lassod ready, socket %s", control.socket_path())
    await stop.wait()

    for task in background:
        task.cancel()
    server.close()
    await daemon.disconnect()  # release the phone before exiting
    control.socket_path().unlink(missing_ok=True)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LASSO_LOG", "INFO"), format="%(name)s: %(levelname)s %(message)s")
    asyncio.run(run(config_mod.load()))
