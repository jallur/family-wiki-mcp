from __future__ import annotations

from pathlib import Path


def test_dockerfile_and_compose_match_cloud01():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    deploy = Path("DEPLOY.md").read_text(encoding="utf-8")
    assert "--proxy-headers" in dockerfile
    assert "--forwarded-allow-ips=${FORWARDED_ALLOW_IPS:-172.20.0.2}" in dockerfile
    assert "FORWARDED_ALLOW_IPS=172.20.0.2" in dockerfile
    assert "/root/family-wiki-mcp/keys.json:/run/secrets/keys.json:ro" in compose
    assert "/root/family-wiki-mcp/data:/data" in compose
    assert 'FORWARDED_ALLOW_IPS: "172.20.0.2"' in compose
    assert "trusted_proxies static 158.174.102.122/32" in deploy
    assert "grok-web" in deploy and "openwebui" in deploy and "nuria" in deploy
    assert "comments.create.content" in Path("NOTES.md").read_text(encoding="utf-8")
