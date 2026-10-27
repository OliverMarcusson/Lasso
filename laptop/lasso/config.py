"""Config dataclass and loader for ~/.config/lasso/config.toml."""
from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class Config:
    phone_address: str | None = None      # set by `lasso pair`
    auto_connect: bool = True
    idle_minutes: int = 5
    offline_grace_s: int = 20
    tun_name: str = "lasso0"
    deny_names: list[str] = field(default_factory=list)   # extra demand denylist entries (suffix match)
    deny_units: list[str] = field(default_factory=list)   # user units whose traffic never counts as demand
    phone_name: str = "phone"             # extra (not in the contract): shown in notifications, set by `lasso pair`


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "lasso" / "config.toml"


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return Config()
    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in data.items() if k in known})


def _set_key(text: str, key: str, value: str) -> str:
    line = f"{key} = {json.dumps(value)}"
    pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(lambda _: line, text, count=1)
    # Top-level keys must come before any [table], so put new ones first.
    return line + "\n" + text


def save_phone(address: str, name: str, path: Path | None = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text() if path.exists() else ""
    text = _set_key(text, "phone_address", address)
    text = _set_key(text, "phone_name", name)
    path.write_text(text)
