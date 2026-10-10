#!/usr/bin/env python3
"""Återkalla OAuth-tokens i tokenfilen. Ingen HTTP-endpoint.

Välj ett av --hash (sha256 av access-token), --key (key_name) eller
--client (client_id i tokenposten). Processen som kör bryggan läser om
filen när mtime ändras, så containern behöver inte startas om.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tokens import TokenStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Återkalla OAuth-tokens i oauth-tokens.json.")
    parser.add_argument("--file", required=True, help="Sökväg till oauth-tokens.json")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--hash", help="sha256 av access-token, 64 hextecken")
    group.add_argument("--key", help="nyckelnamn, fältet key_name")
    group.add_argument("--client", help="klient i tokenposten, fältet client_id")
    args = parser.parse_args(argv)

    if args.hash is not None:
        digest = args.hash.strip().lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            print("ogiltig token-hash, ska vara 64 hextecken", file=sys.stderr)
            return 2
        selector = ("hash", digest)
    elif args.key is not None:
        name = args.key.strip()
        if not name:
            print("tomt nyckelnamn", file=sys.stderr)
            return 2
        selector = ("nyckel", name)
    else:
        client = (args.client or "").strip()
        if not client:
            print("tomt klientnamn", file=sys.stderr)
            return 2
        selector = ("klient", client)

    store = TokenStore(args.file)
    if selector[0] == "hash":
        count = 1 if store.revoke_hash(selector[1]) else 0
    elif selector[0] == "nyckel":
        count = store.revoke_key(selector[1])
    else:
        count = store.revoke_client(selector[1])

    word = "token" if count == 1 else "tokens"
    message = f"Återkallade {count} {word} via {selector[0]} {selector[1]}."
    if count == 0:
        print(message, file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
