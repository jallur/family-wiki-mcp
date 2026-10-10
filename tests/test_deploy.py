from __future__ import annotations

from pathlib import Path


def test_dockerfile_and_compose_match_cloud01():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    deploy = Path("DEPLOY.md").read_text(encoding="utf-8")
    assert "--proxy-headers" in dockerfile
    assert "--forwarded-allow-ips=${FORWARDED_ALLOW_IPS:-172.20.0.2}" in dockerfile
    assert "FORWARDED_ALLOW_IPS=172.20.0.2" in dockerfile
    assert "/root/family-wiki-mcp/keys:/run/secrets/keys:ro" in compose
    assert "KEYS_FILE: /run/secrets/keys/keys.json" in compose
    assert "keys.json:/run/secrets/keys.json" not in compose
    assert "/root/family-wiki-mcp/data:/data" in compose
    assert 'FORWARDED_ALLOW_IPS: "172.20.0.2,158.174.102.122"' in compose
    assert "IDENTITY_KEY_FILE: /run/secrets/identity-key" in compose
    assert "IDENTITY_STORE_DIR: /data/identities" in compose
    assert "COPY scripts" in dockerfile
    assert "scripts/revoke_token.py" in deploy
    assert "scripts/set_identity.py" in deploy
    assert "docker exec -i" in deploy
    assert "Ingen HTTP" in deploy or "ingen HTTP" in deploy
    assert "/root/family-wiki-mcp/keys:/run/secrets/keys:ro" in deploy
    assert "KEYS_FILE: /run/secrets/keys/keys.json" in deploy
    assert "family-wiki-mcp" in deploy
    assert "trusted_proxies static 158.174.102.122/32" in deploy
    assert "grok-web" in deploy and "openwebui" in deploy and "nuria" in deploy
    assert "comments.create.content" in Path("NOTES.md").read_text(encoding="utf-8")
