#!/usr/bin/env python3
"""Spara en Wiki.js-användar-JWT som krypterad identitet.

JWT läses från stdin, aldrig från argv. Valideras med users.profile
mot Wiki.js. Skriver ut användar-id, namn och e-post, aldrig tokenet.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.identity import IdentityError, IdentityStore, identity_name_ok, validate_user_jwt  # noqa: E402
from app.wiki_client import WikiClient  # noqa: E402


def _looks_like_jwt(value: str) -> bool:
    parts = value.split(".")
    return len(parts) == 3 and all(len(part) >= 8 for part in parts)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raw_args = list(sys.argv[1:] if argv is None else argv)
    for arg in raw_args:
        if _looks_like_jwt(arg):
            print("skicka inte JWT som argument", file=sys.stderr)
            return 2

    parser = argparse.ArgumentParser(description="Kryptera och spara en Wiki.js-identitet.")
    parser.add_argument("--identity", required=True)
    parser.add_argument("--wiki-url", default=os.environ.get("WIKI_URL", ""))
    parser.add_argument("--key-file", default=os.environ.get("IDENTITY_KEY_FILE", ""))
    parser.add_argument("--store", default=os.environ.get("IDENTITY_STORE_DIR", "/data/identities"))
    args = parser.parse_args(raw_args)

    if not identity_name_ok(args.identity):
        print("ogiltigt identitetsnamn", file=sys.stderr)
        return 2
    if not args.wiki_url or not args.key_file:
        print("WIKI_URL och IDENTITY_KEY_FILE krävs", file=sys.stderr)
        return 2
    if sys.stdin.isatty():
        print("JWT måste pipas in via stdin, inte skrivas i terminalen.", file=sys.stderr)
        return 2

    raw = sys.stdin.read(16385)
    if len(raw) > 16384:
        print("JWT är för lång", file=sys.stderr)
        return 2
    jwt = raw.strip()
    if not jwt or any(char.isspace() for char in jwt):
        print("JWT ska vara en enda rad på stdin", file=sys.stderr)
        return 2

    wiki = WikiClient(args.wiki_url, "unused")
    try:
        profile, stored = validate_user_jwt(wiki, jwt)
        IdentityStore(args.store, args.key_file, wiki).save_validated(args.identity, stored, profile)
    except IdentityError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        wiki.close()

    print(f"id: {profile['id']}")
    print(f"namn: {profile['name']}")
    print(f"e-post: {profile['email']}")
    print(f"identitet: {args.identity}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
