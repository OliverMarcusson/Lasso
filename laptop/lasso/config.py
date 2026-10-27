"""Config dataclass and loader for ~/.config/lasso/config.toml."""
from __future__ import annotations

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
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Config()
    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in data.items() if k in known})


def toml_string(value: str) -> str:
    """A TOML basic string: escape backslash, quote and control characters, keep other Unicode as is."""
    out = []
    for ch in value:
        n = ord(ch)
        if ch in '"\\':
            out.append("\\" + ch)
        elif n < 0x20 or n == 0x7F:
            out.append(f"\\u{n:04X}")
        elif 0xD800 <= n <= 0xDFFF:  # lone surrogate (possible from JSON), not encodable
            out.append("\uFFFD")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _set_key(text: str, key: str, value: str) -> str:
    line = f"{key} = {toml_string(value)}"
    pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(lambda _: line, text, count=1)
    # Top-level keys must come before any [table], so put new ones first.
    return line + "\n" + text


def save_phone(address: str, name: str, path: Path | None = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    text = _set_key(text, "phone_address", address)
    text = _set_key(text, "phone_name", name)
    path.write_text(text, encoding="utf-8")
