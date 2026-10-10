# family-wiki-mcp – driftanteckningar (2026-10-10)

## Drift
- Körs på cloud01 i `/root/family-wiki-mcp` med `docker compose` (bygger lokalt från Dockerfile). Containern `family-wiki-mcp` ligger i Docker-nätet `proxy` (172.20.0.8).
- Hemligheter ligger bara på servern, inte i repot: `.env` (MCP_API_KEY, ALLOWED_PREFIXES, ALLOWED_ORIGINS) och `/root/wiki/.api-token` (Wiki.js-nyckeln, monteras som `/run/secrets/wiki-api-token`). Mall: `.env.example`.
- Byte av MCP_API_KEY kräver `docker compose up -d --no-deps --force-recreate family-wiki-mcp`.

## Wiki.js
- Wiki.js **2.5.315** (`ghcr.io/requarks/wiki:2`), container `family-wiki` på Mac Minin, databas `family-wiki-db` (Postgres). Minin nås via ssh-alias `macmini-wan`.
- Bryggan autentiserar med `WIKI_TOKEN` eller `WIKI_TOKEN_FILE` (används: fil) som `Authorization: Bearer` mot `https://wiki.aldenback.com/graphql`.
- API-nyckel i Wiki.js: **grok-cloud01** (apiKeys id 3, aktiv, går ut 2036-08-27). Id 1 och 2 med samma namn är återkallade.
- Nyckelns grupp ligger i nyckelns JWT (`grp`) och lagras inte som kolumn. Den lästes inte, eftersom det kräver nyckeln. Grupper i Wiki.js: 1 Administrators (`manage:system`), 2 Guests (nekas allt), 3 Family (read/write/manage:pages, assets, comments, read:source, read:history).
- Författare: Wiki.js 2 kör en API-nyckel som användare **id 1** (`email: api@localhost`, `server/core/auth.js`). Sidor som bryggan skrivit får därför `authorId = creatorId = 1`, alltså användaren "Jonas" (lokalt konto). Exempel: `projekt/minecraft` och `studier/digital-tjansteutveckling/mysql`, båda 1/1, ändrade 2026-10-09.
- **Kan en annan författare sättas via API:t? Nej.** Mutationerna `pages.create` och `pages.update` saknar fält för författare. `server/models/pages.js` (v2.5.315, rad ~300) sätter `authorId` och `creatorId` till `opts.user.id`. Det enda alternativet är en egen Wiki.js-nyckel per grupp, vilket ändå blir id 1, eller att logga in som användaren.

## Proxykedja och klient-IP
1. Klient → Minins `edge-caddy` (`~/Projects/SpeedTest/deploy/compose/Caddyfile`). `wiki.aldenback.com` med `/mcp`, `/oauth*` och `/.well-known/oauth-*` skickas till `https://157.180.42.121` med `header_up Host wiki.aldenback.com` och TLS-SNI wiki.aldenback.com.
2. cloud01: `caddy-proxy` (172.20.0.2/16 i nätet `proxy`, subnät 172.20.0.0/16, gateway 172.20.0.1) → `family-wiki-mcp:8080`.
3. Caddy sätter `X-Forwarded-For`, `X-Forwarded-Proto` och `X-Forwarded-Host` som standard. Ingen av de två Caddy-instanserna har `trusted_proxies`. cloud01:s Caddy litar därför inte på Minins XFF och sätter XFF till **158.174.102.122** (hemmets IP).
4. uvicorn kör redan `--proxy-headers --forwarded-allow-ips=*` (Dockerfile). Bryggan ser alltså XFF från cloud01:s Caddy, det vill säga alltid 158.174.102.122.
- Förslag för att få riktig klient-IP:
  - cloud01:s Caddyfile: global `servers { trusted_proxies static 158.174.102.122/32 }`.
  - Minins Caddy är första hopp och ska inte lita på inkommande XFF. Inget behöver ändras där.
  - uvicorn: `--forwarded-allow-ips=172.20.0.2` (caddy-proxy) i stället för `*`. En fast IP kan behövas för caddy-proxy i compose, alternativt `172.20.0.0/16`.

## Klienter i dag (alla använder samma MCP_API_KEY)
- **Grok web:** OAuth 2.1 + PKCE (`/oauth/authorize`, redirect `grok.com/connectors-oauth-exchange-code`). Access-token = MCP_API_KEY. Omkopplad 2026-10-10 16:51.
- **Bots, "Aldenbäck wiki" (xAI-hostad):** OAuth, samma token som efter omkopplingen 16:51. Fungerar 16:55.
- **OpenWebUI** (chat.aldenback.com på Minin): tool server "aldenbäck wiki", statisk bearer lagrad i OpenWebUI:s DB.

## Kända brister (för Coder)
- `www-authenticate` i `app/main.py` (rad ~213) har en trasig parameter (`Bearer FAKESECRET_…="…"`). Den ska troligen vara `Bearer resource_metadata="…"`.
- Prefix kontrolleras bara vid skrivning. Läsning omfattar hela wikin.
- En enda nyckel, och OAuth lämnar ut den. Se förslaget om nycklar per klient.
