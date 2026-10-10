"""Hashning och konstanttidsjämförelse. Hemligheter loggas inte här."""
from __future__ import annotations

import hashlib
import secrets


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def const_eq(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return secrets.compare_digest(left, right)
