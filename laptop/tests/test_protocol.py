import json

import pytest

from lasso import protocol
from lasso.protocol import ProtocolError, Ready

READY = {"t": "READY", "v": 1, "ssid": "Lasso-ab12cd", "psk": "A" * 24, "sec": "wpa3t",
         "sid": "0123456789abcdef0123456789abcdef", "port": 47123, "owned": True}


def test_encode_is_compact_json_with_version():
    data = protocol.encode("RELEASE", sid="x")
    assert data == b'{"t":"RELEASE","v":1,"sid":"x"}'


def test_encode_rejects_oversize():
    with pytest.raises(ProtocolError):
        protocol.encode("PAIR", name="h" * 200)


def test_encode_ready_sized_message_fits():
    assert len(json.dumps(READY, separators=(",", ":"))) <= protocol.MAX_MESSAGE


def test_decode_ignores_unknown_fields_and_keeps_them():
    msg = protocol.decode(b'{"t":"STARTING","v":1,"extra":[1,2]}')
    assert msg == {"t": "STARTING", "v": 1, "extra": [1, 2]}


@pytest.mark.parametrize("raw", [b"", b"nope", b"[]", b'{"v":1}', b'{"t":"X"}', b'{"t":"X","v":2}', b"\xff\xfe"])
def test_decode_rejects_non_messages(raw):
    assert protocol.decode(raw) is None


def test_ready_from_message():
    r = Ready.from_message(READY)
    assert (r.ssid, r.sec, r.port, r.owned) == ("Lasso-ab12cd", "wpa3t", 47123, True)


@pytest.mark.parametrize("patch", [{"sec": "wep"}, {"sid": "XYZ"}, {"port": 0}, {"port": "1"}, {"owned": "yes"},
                                    {"psk": ""}, {"ssid": 5}])
def test_ready_validation(patch):
    with pytest.raises(ProtocolError):
        Ready.from_message({**READY, **patch})


def test_ready_missing_field():
    bad = {k: v for k, v in READY.items() if k != "sid"}
    with pytest.raises(ProtocolError):
        Ready.from_message(bad)


def test_phone_error_text():
    assert "Shizuku" in protocol.PhoneError("NO_PRIVILEGE").text
    assert protocol.PhoneError("WEIRD").text == "WEIRD"
