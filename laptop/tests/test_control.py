import asyncio

from lasso import control


async def test_roundtrip_and_errors(tmp_path):
    path = tmp_path / "lasso.sock"

    async def handler(req):
        if req["cmd"] == "boom":
            raise RuntimeError("kaput")
        return {"ok": True, "state": "IDLE", "detail": req["cmd"]}

    server = await control.serve(handler, path)
    try:
        ok = await asyncio.to_thread(control.request, {"cmd": "status"}, path, 5)
        assert ok == {"ok": True, "state": "IDLE", "detail": "status"}
        bad = await asyncio.to_thread(control.request, {"cmd": "boom"}, path, 5)
        assert bad == {"ok": False, "error": "kaput"}
    finally:
        server.close()


def test_no_daemon(tmp_path):
    import pytest
    with pytest.raises(ConnectionError):
        control.request({"cmd": "status"}, tmp_path / "missing.sock")
