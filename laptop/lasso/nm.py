"""NetworkManager through the nmcli CLI. One connection named `lasso`, rewritten for every session."""
from __future__ import annotations

import asyncio
import logging
import time

from lasso import protocol
from lasso.protocol import LassoError

log = logging.getLogger("lasso.nm")

CONN = "lasso"

# wpa3t (WPA2/WPA3 transition) is joined as plain wpa-psk: a transition AP accepts it and it avoids SAE
# quirks in some Wi-Fi drivers. Only a strict wpa3 hotspot uses sae. There is no sae -> wpa-psk fallback.
KEY_MGMT = {"wpa3t": "wpa-psk", "wpa2": "wpa-psk", "wpa3": "sae"}


def split_terse(line: str) -> list[str]:
    """Split an `nmcli -t` line on unescaped ':' and undo the backslash escapes."""
    fields: list[str] = []
    cur: list[str] = []
    escaped = False
    for ch in line:
        if escaped:
            cur.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    fields.append("".join(cur))
    return fields


def profile_settings(ssid: str, psk: str, sec: str) -> list[str]:
    """Property/value pairs for `nmcli con add|modify`."""
    return [
        "802-11-wireless.ssid", ssid,
        "802-11-wireless-security.key-mgmt", KEY_MGMT[sec],
        "802-11-wireless-security.psk", psk,
        "connection.metered", "yes",
        "connection.autoconnect", "no",
        "connection.autoconnect-priority", "-10",
    ]


def parse_ethernet_up(out: str) -> bool:
    """From `nmcli -t -f TYPE,STATE dev`."""
    for line in out.splitlines():
        parts = split_terse(line)
        if len(parts) >= 2 and parts[0] == "ethernet" and parts[1] == "connected":
            return True
    return False


def parse_ssids(out: str) -> set[str]:
    """From `nmcli -t -f SSID dev wifi list`."""
    return {line for line in (split_terse(raw)[0] for raw in out.splitlines()) if line}


def known_connections(visible: set[str], saved: dict[str, str]) -> list[str]:
    """Names of saved autoconnect Wi-Fi connections (saved: ssid -> name) whose SSID is visible."""
    return sorted(name for ssid, name in saved.items() if ssid in visible)


class NM:
    async def run(self, *args: str, timeout: float = 60) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "nmcli", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "", "nmcli timed out"
        return proc.returncode or 0, out.decode(), err.decode()

    async def _exists(self) -> bool:
        _, out, _ = await self.run("-t", "-f", "NAME", "con", "show")
        return CONN in (split_terse(line)[0] for line in out.splitlines())

    async def _write_profile(self, ssid: str, psk: str, sec: str) -> None:
        # The PSK is on nmcli's argv for the moment; it is a per-session secret that expires with the session.
        if not await self._exists():
            rc, _, err = await self.run("con", "add", "type", "wifi", "con-name", CONN, "ifname", "*", "ssid", ssid)
            if rc != 0:
                raise LassoError(f"Could not create Wi-Fi profile: {err.strip()}")
        rc, _, err = await self.run("con", "modify", CONN, *profile_settings(ssid, psk, sec))
        if rc != 0:
            raise LassoError(f"Could not configure Wi-Fi profile: {err.strip()}")

    async def gateway(self) -> str:
        _, out, _ = await self.run("-g", "IP4.GATEWAY", "con", "show", CONN)
        gw = out.strip()
        return "" if gw in ("", "--") else gw

    async def join(self, ssid: str, psk: str, sec: str, timeout: float = protocol.WIFI_JOIN_S) -> str:
        """Create/update the `lasso` connection, bring it up, return the IPv4 gateway. 30 s overall."""
        deadline = time.monotonic() + timeout
        await self._write_profile(ssid, psk, sec)
        last = "no suitable network found"
        while True:
            left = deadline - time.monotonic()
            if left < 3:
                raise LassoError(f"Could not join hotspot: {last}")
            await self.run("dev", "wifi", "rescan", timeout=5)  # the new SSID must be in the scan list
            rc, _, err = await self.run("-w", str(int(min(left, 15))), "con", "up", CONN, timeout=left + 5)
            if rc == 0:
                break
            last = err.strip() or last
            await asyncio.sleep(1)
        while True:
            gw = await self.gateway()
            if gw:
                return gw
            if time.monotonic() > deadline:
                raise LassoError("Could not join hotspot: no gateway from DHCP")
            await asyncio.sleep(0.5)

    async def down(self) -> None:
        await self.run("con", "down", CONN, timeout=15)

    async def up(self, name: str) -> bool:
        rc, _, err = await self.run("-w", "20", "con", "up", "id", name, timeout=25)
        if rc != 0:
            log.warning("could not bring up %s: %s", name, err.strip())
        return rc == 0

    async def connectivity(self) -> str:
        _, out, _ = await self.run("-t", "-f", "CONNECTIVITY", "general", timeout=10)
        return out.strip() or "unknown"

    async def ethernet_up(self) -> bool:
        _, out, _ = await self.run("-t", "-f", "TYPE,STATE", "dev", timeout=10)
        return parse_ethernet_up(out)

    async def visible_ssids(self) -> set[str]:
        _, out, _ = await self.run("-t", "-f", "SSID", "dev", "wifi", "list", "--rescan", "no", timeout=10)
        return parse_ssids(out)

    async def saved_wifi(self) -> dict[str, str]:
        """ssid -> connection name for saved Wi-Fi connections with autoconnect on, excluding `lasso`."""
        _, out, _ = await self.run("-t", "-f", "NAME,TYPE", "con", "show", timeout=10)
        names = [p[0] for p in map(split_terse, out.splitlines()) if len(p) >= 2 and p[1] == "802-11-wireless"]
        saved: dict[str, str] = {}
        for name in names:
            if name == CONN:
                continue
            _, props, _ = await self.run("-g", "802-11-wireless.ssid,connection.autoconnect", "con", "show", name,
                                         timeout=10)
            lines = props.splitlines()
            if len(lines) >= 2 and lines[0] and lines[1] == "yes":
                saved[lines[0]] = name
        return saved
