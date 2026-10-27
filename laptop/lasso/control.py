"""Local control socket: one JSON request line in, one JSON response line out."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("lasso.control")

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "lasso.sock"


async def serve(handler: Handler, path: Path | None = None) -> asyncio.Server:
    path = path or socket_path()
    path.unlink(missing_ok=True)

    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            try:
                req = json.loads(line)
                if not isinstance(req, dict):
                    raise ValueError
            except ValueError:
                resp: dict[str, Any] = {"ok": False, "error": "bad request"}
            else:
                try:
                    resp = await handler(req)
                except Exception as e:  # noqa: BLE001 - report to the client instead of dropping it
                    log.exception("control command failed")
                    resp = {"ok": False, "error": str(e) or type(e).__name__}
            writer.write(json.dumps(resp).encode() + b"\n")
            await writer.drain()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_unix_server(on_client, str(path))
    path.chmod(0o600)
    return server


def request(cmd: dict[str, Any], path: Path | None = None, timeout: float = 150) -> dict[str, Any]:
    """Blocking client. Raises ConnectionError if the daemon is not running."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect(str(path or socket_path()))
        except (FileNotFoundError, ConnectionRefusedError) as e:
            raise ConnectionError("lassod is not running (systemctl --user start lassod)") from e
        s.sendall(json.dumps(cmd).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf)
