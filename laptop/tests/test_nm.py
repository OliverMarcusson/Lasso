import pytest

from lasso.nm import CONN, NM, profile_settings
from lasso.protocol import LassoError


def test_profile_settings_by_security():
    s = dict(zip(*[iter(profile_settings("Lasso-x", "wpa3"))] * 2))
    assert s["802-11-wireless-security.key-mgmt"] == "sae"
    assert s["connection.metered"] == "yes" and s["connection.autoconnect"] == "no"
    assert s["connection.autoconnect-priority"] == "-10"
    for sec in ("wpa3t", "wpa2"):
        s = dict(zip(*[iter(profile_settings("Lasso-x", sec))] * 2))
        assert s["802-11-wireless-security.key-mgmt"] == "wpa-psk"


class FakeNmcli(NM):
    def __init__(self, existing=False, up_ok=True, gateway="192.168.43.1"):
        self.existing, self.up_ok, self.gw = existing, up_ok, gateway
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *args, timeout=60):
        self.calls.append(args)
        if args[:4] == ("-t", "-f", "NAME", "con"):
            return 0, (CONN + "\nHome\n" if self.existing else "Home\n"), ""
        if "up" in args:
            return (0, "", "") if self.up_ok else (4, "", "Error: no suitable device")
        if args[0] == "-g":
            return 0, self.gw + "\n", ""
        return 0, "", ""


async def test_join_creates_then_modifies_and_returns_gateway():
    nm = FakeNmcli()
    assert await nm.join("Lasso-x", "pw", "wpa3t") == "192.168.43.1"
    verbs = [c[:3] for c in nm.calls if c[0] == "con"]
    assert verbs[0] == ("con", "add", "type") and verbs[1][:2] == ("con", "modify")
    assert any(c[:4] == ("-w", "15", "con", "up") or c[2:4] == ("con", "up") for c in nm.calls)


async def test_join_reuses_existing_profile():
    nm = FakeNmcli(existing=True)
    await nm.join("Lasso-x", "pw", "wpa2")
    assert not any(c[:2] == ("con", "add") for c in nm.calls)


async def test_join_times_out_with_reason():
    nm = FakeNmcli(up_ok=False)
    with pytest.raises(LassoError, match="Could not join hotspot: Error: no suitable device"):
        await nm.join("Lasso-x", "pw", "wpa2", timeout=3.5)


import asyncio
import os
import stat

from lasso import nm as nm_mod


class FakeProc:
    def __init__(self):
        self.killed = self.reaped = False
        self.started = asyncio.Event()

    async def communicate(self):
        self.started.set()
        await asyncio.Event().wait()

    def kill(self):
        self.killed = True

    async def wait(self):
        self.reaped = True
        return -9


async def test_cancelled_nmcli_is_killed_and_reaped(monkeypatch):
    proc = FakeProc()

    async def spawn(*a, **kw):
        return proc
    monkeypatch.setattr(nm_mod.asyncio, "create_subprocess_exec", spawn)
    task = asyncio.ensure_future(NM().run("con", "up", "lasso"))
    await proc.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.killed and proc.reaped


async def test_join_deadline_covers_profile_setup():
    nm = NM()

    async def blocked(*a):
        await asyncio.Event().wait()
    nm._write_profile = blocked
    with pytest.raises(LassoError, match="Could not join hotspot"):
        await asyncio.wait_for(nm.join("Lasso-x", "A" * 24, "wpa2", timeout=0.05), 1)


async def test_join_deadline_covers_gateway_lookup():
    nm = FakeNmcli(gateway="--")
    with pytest.raises(LassoError, match="no gateway from DHCP"):
        await asyncio.wait_for(nm.join("Lasso-x", "A" * 24, "wpa2", timeout=0.2), 1)


async def test_psk_never_on_argv_and_passwd_file_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    seen = []

    class Spy(FakeNmcli):
        async def run(self, *args, timeout=60):
            if "passwd-file" in args:
                path = args[args.index("passwd-file") + 1]
                seen.append((path, open(path).read(), stat.S_IMODE(os.stat(path).st_mode)))
            return await super().run(*args, timeout=timeout)
    nm = Spy()
    await nm.join("Lasso-x", "secretPSK123", "wpa3t")
    assert not any("secretPSK123" in a for c in nm.calls for a in c)
    modify = next(c for c in nm.calls if c[:2] == ("con", "modify"))
    assert dict(zip(modify[3::2], modify[4::2]))["802-11-wireless-security.psk-flags"] == "2"
    [(path, text, mode)] = seen
    assert text == "802-11-wireless-security.psk:secretPSK123\n" and mode == 0o600
    assert os.path.dirname(path) == str(tmp_path) and not os.path.exists(path)


async def test_saved_wifi_unescapes_ssid():
    class Fake(NM):
        async def run(self, *args, **kwargs):
            if args[:3] == ("-t", "-f", "NAME,TYPE"):
                return 0, "Cafe:802-11-wireless\n", ""
            return 0, "Cafe\\:5G\\\\x\nyes\n", ""
    assert await Fake().saved_wifi() == {"Cafe:5G\\x": "Cafe"}
