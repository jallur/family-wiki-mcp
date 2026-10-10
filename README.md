# Family Wiki MCP Bridge (Streamable HTTP)

Custom Python MCP bridge so Grok.com connectors can read/write the family Wiki.js
without ever receiving the Wiki GraphQL token.

## Live endpoints (HTTPS)

- MCP (use this in Grok): https://wiki.aldenback.com/mcp-api/mcp
- Health: https://wiki.aldenback.com/mcp-api/health
- Preferred subdomain (DNS pending): https://mcp.wiki.aldenback.com/mcp

## Auth

- Header: `Authorization: Bearer <MCP_API_KEY>`
- Also accepted: `X-API-Key: <MCP_API_KEY>`
- Key file (mode 600): `/root/family-wiki-mcp/.env` and `/root/family-wiki-mcp/.mcp-api-key`
- Wiki token stays on cloud01 via bind-mount `/root/wiki/.api-token` (never in the report / never sent to Grok)

## Write policy (v1)

- Allowed path prefixes only: `projekt/`, `familj/`, `studier/`
- No delete tools
- Locale default: `sv`, editor: `markdown`
- `update_page` requires `tags` (preserve from `get_page`)

## Deploy paths

- Code/compose: `/root/family-wiki-mcp/`
- Container: `family-wiki-mcp` on docker network `proxy`
- Caddy: `/root/proxy/Caddyfile` (`handle_path /mcp-api/*` + future `mcp.wiki.aldenback.com`)

## Grok custom connector

1. https://grok.com/connectors → New → Custom
2. URL: `https://wiki.aldenback.com/mcp-api/mcp`
3. Auth: Bearer token = value of `MCP_API_KEY` from `/root/family-wiki-mcp/.env`

## DNS needed (Jonas/CoreIT)

Create Cloudflare A record (DNS-only / grey cloud OK for Caddy ACME):

- Name: `mcp.wiki`
- Type: A
- Value: `157.180.42.121`
- Proxy: DNS only (grey) recommended so Caddy can issue the cert

Until then use the `/mcp-api` path under `wiki.aldenback.com`.
