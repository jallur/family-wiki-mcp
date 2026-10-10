# Family Wiki MCP

HTTPS-brygga så att Grok, bots och OpenWebUI kan läsa och skriva familjens Wiki.js utan att få Wiki.js-token.

Drift på cloud01: [DEPLOY.md](DEPLOY.md). Bakgrund om Wiki.js 2.5 och proxykedjan: [NOTES.md](NOTES.md).

## Ändpunkter

- MCP: `https://wiki.aldenback.com/mcp`
- Hälsa (utan nyckel): `https://wiki.aldenback.com/health`
- OAuth: `https://wiki.aldenback.com/oauth/authorize` och `/oauth/token`
- Metadata: `/.well-known/oauth-authorization-server` och `/.well-known/oauth-protected-resource`

## Nycklar

`keys.json` har mode 600 och monteras read-only. Varje post har:

| fält | betydelse |
| --- | --- |
| `name` | Namn i logg och ändringskommentar |
| `sha256` | Hex-hash av hemligheten. Själva nyckeln lagras inte |
| `scope` | `read` eller `write` |
| `prefixes` | Sökvägar för `create_page` och `update_page` |
| `read_prefixes` | Filter för `get_page`, `search_pages` och `list_pages`. Tom lista betyder hela wikin |
| `expires` | ISO-tid eller `null` |
| `disabled` | `true` stänger nyckeln och återkallar dess OAuth-tokens |
| `created` | ISO-tid |
| `author` | Text i ändringskommentaren. Tomt fält faller tillbaka på `name` |

`create_page` och `update_page` kräver `write` och att sökvägen träffar nyckelns `prefixes` och serverns allowlist. Det finns inget raderingsverktyg.

Skriptet `scripts/new_key.py` skriver ut hemligheten en gång och en JSON-post. Exempelfilen `keys.example.json` är avstängd och ska inte monteras.

## OAuth

PKCE (S256). Formuläret tar emot en namngiven nyckel och ger ut en egen opak access token. Token lagras bara som sha256, kopplad till nyckelns namn, i volymen `TOKEN_STORE_PATH` (standard `/data/oauth-tokens.json`).

Ett `client_id` per klient:

- `grok-web` — redirect-värd `grok.com`
- `bots` — redirect-värd `x.ai` eller `grok.com`

OpenWebUI använder statisk `Authorization: Bearer` eller `X-API-Key` med nyckeln `openwebui`, inte OAuth.

Återkallning gäller per token. När nyckeln sätts till `disabled` (eller tas bort) återkallas alla tokens som är kopplade till namnet. En avstängd nyckel går inte att använda som bearer heller.

## Författare

Wiki.js 2.5.315 kör alla anrop med API-nyckel som användare 1. Mutationerna `pages.create` och `pages.update` har inget fält för författare, och inte heller för en ändringskommentar. `description` är sidans sammanfattning och lämnas som anroparen skickade den. `content` ändras inte.

Det fält GraphQL stöder för en ändringskommentar är `comments.create.content`. Efter lyckad create och update skriver bryggan en sidkommentar:

```text
MCP: sidan skapades av Jonas (nyckel grok-web).
```

`author` används om det är satt, annars `name`.

## Logg och klient-IP

En rad per anrop:

```text
anrop key=grok-web client=grok-web tool=get_page path=projekt/a result=ok ip=203.0.113.5
```

Nyckel och token loggas inte. Statisk bearer har `client=-`.

Käll-IP kommer från uvicorn `--proxy-headers --forwarded-allow-ips`. Standard är `172.20.0.2` (cloud01:s caddy-proxy) och styrs med `FORWARDED_ALLOW_IPS`. cloud01:s Caddy behöver `trusted_proxies static 158.174.102.122/32`, se DEPLOY.md.

## Legacy

`MCP_API_KEY` godtas med full skrivrätt inom serverns prefix om `ALLOW_LEGACY_API_KEY` inte är `false`. Varje träff loggas som varning, utan nyckelvärdet. Nyckeln syns som `key=legacy`.

## Omladdning

`keys.json` läses om när filens mtime ändras, och direkt på `SIGHUP`. Om JSON är trasig behålls den gamla listan och ett fel loggas.

## Tester

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

Wiki.js är mockad. Testerna täcker scope, prefix, läsfilter, utgång, disabled, återkallning, omladdning, trasig JSON, legacy, OAuth per klient, författare i ändringskommentaren och att nyckeln inte loggas.
