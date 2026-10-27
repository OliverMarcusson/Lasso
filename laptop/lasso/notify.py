"""Desktop notifications through notify-send, one notification updated in place per attempt."""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("lasso.notify")


class Notifier:
    def __init__(self) -> None:
        self._id: int | None = None

    def new_attempt(self) -> None:
        """The next show() creates a fresh notification instead of replacing the last one."""
        self._id = None

    async def show(self, summary: str, body: str = "", urgency: str = "normal") -> None:
        cmd = ["notify-send", "--print-id", "--app-name=Lasso", f"--urgency={urgency}"]
        if self._id is not None:
            cmd.append(f"--replace-id={self._id}")
        cmd += [summary, body] if body else [summary]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 5)
        except (OSError, TimeoutError) as e:
            log.warning("notify-send failed: %s", e)
            return
        if out.strip().isdigit():
            self._id = int(out.strip())
