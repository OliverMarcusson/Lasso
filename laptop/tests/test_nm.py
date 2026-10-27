import pytest

from lasso.nm import CONN, NM, profile_settings
from lasso.protocol import LassoError


def test_profile_settings_by_security():
    s = dict(zip(*[iter(profile_settings("Lasso-x", "pw", "wpa3"))] * 2))
    assert s["802-11-wireless-security.key-mgmt"] == "sae"
    assert s["connection.metered"] == "yes" and s["connection.autoconnect"] == "no"
    assert s["connection.autoconnect-priority"] == "-10"
    for sec in ("wpa3t", "wpa2"):
        s = dict(zip(*[iter(profile_settings("Lasso-x", "pw", sec))] * 2))
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
