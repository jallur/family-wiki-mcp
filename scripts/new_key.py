#!/usr/bin/env python3
"""Skapa en namngiven nyckel. Hemligheten skrivs ut en gång, aldrig till fil."""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
from datetime import datetime, timezone


def _prefixes(raw: str) -> list[str]:
    out: list[str] = []
    for part in raw.split(","):
        prefix = part.strip().lstrip("/")
        if not prefix:
            continue
        if not prefix.endswith("/"):
            prefix += "/"
        out.append(prefix)
    return out


def build_entry(args: argparse.Namespace, secret: str) -> dict:
    return {
        "name": args.name,
        "sha256": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
        "scope": args.scope,
        "prefixes": _prefixes(args.prefixes or ""),
        "read_prefixes": _prefixes(args.read_prefixes or ""),
        "expires": args.expires,
        "disabled": False,
        "created": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "author": args.author if args.author is not None else args.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Skriv ut en nyckel en gång och en post för keys.json.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--scope", required=True, choices=("read", "write"))
    parser.add_argument("--author", default=None)
    parser.add_argument("--prefixes", default="", help="Kommaseparerade skrivprefix, till exempel projekt/,familj/")
    parser.add_argument("--read-prefixes", default="", help="Tomt betyder att läsning omfattar hela wikin")
    parser.add_argument("--expires", default=None, help="ISO-tid eller utelämnad")
    args = parser.parse_args(argv)
    prefixes = _prefixes(args.prefixes or "")
    if args.scope == "write" and not prefixes:
        print("scope write kräver minst ett --prefixes", file=sys.stderr)
        return 2
    secret = secrets.token_urlsafe(32)
    entry = build_entry(args, secret)
    print("Nyckel (visas bara den här gången):")
    print(secret)
    print()
    print("Post för keys.json. Spara inte nyckeln i filen. chmod 600 keys.json")
    print(json.dumps(entry, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
