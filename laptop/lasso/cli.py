"""`lasso` command line: talks to lassod over the control socket (scan runs directly)."""
from __future__ import annotations

import argparse
import asyncio
import sys

from lasso import ble, control


def _ask(cmd: dict) -> int:
    try:
        resp = control.request(cmd)
    except ConnectionError as e:
        print(f"lasso: {e}", file=sys.stderr)
        return 2
    if not resp.get("ok"):
        print(f"lasso: {resp.get('error', 'failed')}", file=sys.stderr)
        return 1
    print(f"{resp['state']} {resp.get('detail', '')}".rstrip())
    return 0


def _scan(timeout: float) -> int:
    print(f"Scanning {timeout:.0f} s for devices advertising the Lasso service...", file=sys.stderr)
    found = asyncio.run(ble.scan(timeout))
    for address, name in found:
        print(f"{address}  {name}")
    if not found:
        print("lasso: nothing found (is the pairing screen open on the phone?)", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="lasso", description="Start a phone hotspot over Bluetooth and join it.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("connect", "disconnect", "status"):
        sub.add_parser(name)
    pair = sub.add_parser("pair", help="pair with the phone (open its pairing screen first)")
    pair.add_argument("address", nargs="?", help="Bluetooth address from `lasso scan`; optional if already paired")
    scan = sub.add_parser("scan", help="list nearby phones advertising Lasso")
    scan.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args(argv)

    if args.cmd == "scan":
        sys.exit(_scan(args.timeout))
    if args.cmd == "pair":
        print("Open the pairing screen in the Lasso app. When Bluetooth asks, confirm the same numeric code "
              "on both screens.", file=sys.stderr)
        sys.exit(_ask({"cmd": "pair", "address": args.address}))
    sys.exit(_ask({"cmd": args.cmd}))
