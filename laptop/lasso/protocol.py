"""Wire messages (contract.md): compact UTF-8 JSON, one message per write/notification/datagram."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

SERVICE_UUID = "210ce96b-a4ff-4f59-9bd3-b9a8dc850af8"
CONTROL_UUID = "7470fd02-45dc-4fda-a006-2c2cb7a6426c"
EVENTS_UUID = "3d83b6c3-38f5-48da-8f70-53b160cebbc9"

VERSION = 1
HEARTBEAT_PORT = 47123
HEARTBEAT_INTERVAL_S = 30
MAX_MISSED = 3
BLE_CONNECT_S = 15
READY_S = 20
WIFI_JOIN_S = 30
MAX_MESSAGE = 180

SECURITIES = ("wpa3t", "wpa3", "wpa2")

ERROR_TEXT = {
    "NOT_PAIRED": "this laptop is not paired (run lasso pair)",
    "NO_PRIVILEGE": "Shizuku is not running on the phone",
    "NO_MOBILE_DATA": "phone has no mobile data",
    "AIRPLANE": "phone is in airplane mode",
    "TETHER_FAILED": "phone could not start tethering",
    "BUSY": "phone is busy",
    "BAD_REQUEST": "phone rejected the request",
}


class LassoError(Exception):
    """A failure with a message that is fit to show the user."""


class ProtocolError(LassoError):
    pass


class PhoneError(LassoError):
    """The phone answered ERROR; str() is the wire code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    @property
    def text(self) -> str:
        return ERROR_TEXT.get(self.code, self.code)


def encode(t: str, **fields: Any) -> bytes:
    data = json.dumps({"t": t, "v": VERSION, **fields}, separators=(",", ":"), ensure_ascii=False).encode()
    if len(data) > MAX_MESSAGE:
        raise ProtocolError(f"{t} message is {len(data)} bytes, limit is {MAX_MESSAGE}")
    return data


def decode(data: bytes) -> dict[str, Any] | None:
    """Parse a message. Returns None for anything that is not a v1 message; unknown fields are kept."""
    if len(data) > MAX_MESSAGE:
        return None
    try:
        msg = json.loads(data.decode())
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(msg, dict) or not isinstance(msg.get("t"), str) or msg.get("v") != VERSION:
        return None
    return msg


@dataclass(frozen=True)
class Ready:
    ssid: str
    psk: str
    sec: str
    sid: str
    port: int
    owned: bool

    @classmethod
    def from_message(cls, msg: dict[str, Any]) -> Ready:
        try:
            ready = cls(msg["ssid"], msg["psk"], msg["sec"], msg["sid"], msg["port"], msg["owned"])
        except KeyError as e:
            raise ProtocolError(f"READY is missing {e.args[0]}") from e
        if not (isinstance(ready.ssid, str) and ready.ssid and isinstance(ready.psk, str) and ready.psk):
            raise ProtocolError("READY has a bad ssid or psk")
        if ready.sec not in SECURITIES:
            raise ProtocolError(f"READY has unknown sec {ready.sec!r}")
        if not isinstance(ready.sid, str) or not re.fullmatch(r"[0-9a-f]{32}", ready.sid):
            raise ProtocolError("READY has a bad sid")
        if not isinstance(ready.port, int) or isinstance(ready.port, bool) or not 0 < ready.port < 65536:
            raise ProtocolError("READY has a bad port")
        if not isinstance(ready.owned, bool):
            raise ProtocolError("READY has a bad owned flag")
        return ready
