from __future__ import annotations

import hashlib
import json
import subprocess
import sys


def test_new_key_prints_secret_once_and_json_entry():
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/new_key.py",
            "--name",
            "nuria",
            "--scope",
            "read",
            "--author",
            "Nuria",
            "--read-prefixes",
            "",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    secret = lines[1]
    assert secret not in lines[0]
    assert proc.stdout.count(secret) == 1
    entry = json.loads(proc.stdout[proc.stdout.index("{") :])
    assert secret not in json.dumps(entry)
    assert entry["name"] == "nuria"
    assert entry["scope"] == "read"
    assert entry["author"] == "Nuria"
    assert entry["prefixes"] == []
    assert entry["read_prefixes"] == []
    assert entry["disabled"] is False
    assert entry["sha256"] == hashlib.sha256(secret.encode("utf-8")).hexdigest()
    assert "nyckel" not in proc.stderr.lower() or secret not in proc.stderr


def test_new_key_write_requires_prefix():
    proc = subprocess.run(
        [sys.executable, "scripts/new_key.py", "--name", "jonas", "--scope", "write"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "prefixes" in proc.stderr
