# Family Wiki MCP

HTTPS-brygga så att Grok, bots och OpenWebUI kan läsa och skriva familjens Wiki.js utan att få Wiki.js-token.

Drift på cloud01: [DEPLOY.md](DEPLOY.md). Bakgrund om Wiki.js 2.5 och proxykedjan: [NOTES.md](NOTES.md).

## Ändpunkter

- MCP: `https://wiki.aldenback.com/mcp`
- Hälsa (utan nyckel): `https://wiki.aldenback.com/health`
- OAuth: `https://wiki.aldenback.com/oauth/authorize` och `/oauth/token`
- Metadata: `/.well-known/oauth-authorization-server` och `/.well-known/oauth-protected-resource`

## Nycklar

`keys/keys.json` har mode 600. Katalogen monteras read-only, så att ett atomiskt `mv` syns i containern. Varje post har:

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
| `wiki_identity` | Valfritt. Namn på en Wiki.js-användar-JWT, till exempel `jonas`. Utan fältet används API-nyckeln |

`create_page` och `update_page` kräver `write` och att sökvägen träffar nyckelns `prefixes` och serverns allowlist. Det finns inget raderingsverktyg.

Skriptet `scripts/new_key.py` skriver ut hemligheten en gång och en JSON-post. Exempelfilen `keys.example.json` är avstängd och ska inte monteras.

## OAuth

PKCE (S256). Formuläret tar emot en namngiven nyckel och ger ut en egen opak access token. Token lagras bara som sha256, kopplad till nyckelns namn, i volymen `TOKEN_STORE_PATH` (standard `/data/oauth-tokens.json`).

Tillåtna `client_id`:

- `grok-web` — redirect-värd `grok.com` (även underdomäner, till exempel `www.grok.com`)
- `bots` — redirect-värd `x.ai` eller `grok.com`
- `family-wiki-mcp` — alias som Grok web skickar. Samma redirect-värdar som redan används: `grok.com` (Grok, sökväg `/connectors-oauth-exchange-code`) och `x.ai` (bots)

Klientnamnet i loggen och i tokenposten är namnet på nyckeln som klistrades in i formuläret. Då går grok-web, bots och jonas att skilja åt även när `client_id` är `family-wiki-mcp`.

OpenWebUI använder statisk `Authorization: Bearer` eller `X-API-Key` med nyckeln `openwebui`, inte OAuth.

Återkallning gäller per token, via `scripts/revoke_token.py` (`--hash`, `--key` eller `--client`) mot tokenfilen. Det finns ingen HTTP-endpoint. Filen läses om när mtime ändras, utan omstart. När nyckeln sätts till `disabled` (eller tas bort) blir alla dess tokens ogiltiga direkt och ligger kvar som återkallade om nyckeln slås på igen. En avstängd nyckel går inte att använda som bearer heller.

## Författare

Wiki.js 2.5.315 kör alla anrop med API-nyckel som användare 1. Mutationerna `pages.create` och `pages.update` har inget fält för författare, och inte heller för en ändringskommentar. `description` är sidans sammanfattning och lämnas som anroparen skickade den. `content` ändras inte.

Det fält GraphQL stöder för en ändringskommentar är `comments.create.content`. Mutationssvaret ber bara om `page { id }`. `locale` och `updatedAt` kan vara null i Wiki.js trots att schemat säger att de är obligatoriska, och då avbryts hela svaret innan kommentaren hinner skrivas. Saknas id hämtas det med `singleByPath` och urvalet `{ id }`. Misslyckas själva kommentaren syns det som `change_comment_error`. Sidskrivningen räknas ändå som lyckad.

Efter lyckad create och update skriver bryggan en sidkommentar:

```text
MCP: sidan skapades av Jonas (nyckel grok-web).
```

`author` används om det är satt, annars `name`.

Om nyckeln har `wiki_identity` går skrivning och läsning med den användarens JWT i stället för API-nyckeln. Wiki.js sätter då `authorId` från användaren, och ändringskommentaren hoppas över. Loggraden finns kvar. JWT:n ligger krypterad i `data/identities/` och förnyas via headern `new-jwt`. Ogiltig identitet faller tillbaka till API-nyckel och en varningskommentar om `IDENTITY_FALLBACK` inte är `false`. Se DEPLOY.md.

## Logg och klient-IP

En rad per anrop:

```text
anrop key=grok-web client=grok-web tool=get_page path=projekt/a result=ok ip=203.0.113.5
```

Nyckel och token loggas inte. Statisk bearer har `client=-`. OAuth har nyckelns namn som `client`, till exempel `grok-web`, `bots` eller `jonas`.

Käll-IP kommer från uvicorn `--proxy-headers --forwarded-allow-ips`. Compose sätter `172.20.0.2,158.174.102.122` (caddy-proxy och hemmets edge). cloud01:s Caddy behöver `trusted_proxies static 158.174.102.122/32`, se DEPLOY.md.

## Legacy

`MCP_API_KEY` godtas med full skrivrätt inom serverns prefix om `ALLOW_LEGACY_API_KEY` inte är `false`. Varje träff loggas som varning, utan nyckelvärdet. Nyckeln syns som `key=legacy`.

## Omladdning

`keys/keys.json` läses om när filens mtime ändras, och direkt på `SIGHUP`. Katalogen monteras, inte filen ensam, så att ett atomiskt `mv` syns i containern. Om JSON är trasig behålls den gamla listan och ett fel loggas. OAuth-tokenfilen läses om på samma sätt när dess mtime ändras. `SIGHUP` behövs inte för den.

## Tester

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

Wiki.js är mockad. Testerna täcker scope, prefix, läsfilter, utgång, disabled, återkallning, omladdning, trasig JSON, legacy, OAuth per klient, författare i ändringskommentaren och att nyckeln inte loggas.
