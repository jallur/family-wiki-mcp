# Driftsättning på cloud01

Bryggan körs i `/root/family-wiki-mcp` med `docker compose`. Containern heter `family-wiki-mcp` och ligger i Docker-nätet `proxy`. Wiki.js-token ligger kvar i `/root/wiki/.api-token` och monteras read-only. Den hör inte hemma i git.

## 1. Backup först

```bash
cd /root/family-wiki-mcp
ts=$(date -u +%Y%m%dT%H%M%SZ)
dest="/root/family-wiki-mcp-backup-$ts"
mkdir -p "$dest"
cp -a docker-compose.yml Dockerfile .env app scripts "$dest/"
if [ -f keys.json ]; then cp -a keys.json "$dest/"; fi
if [ -d keys ]; then cp -a keys "$dest/"; fi
if [ -d data ]; then cp -a data "$dest/"; fi
```

Kontrollera att `$dest` innehåller `.env` och, om de redan finns, `keys.json` eller `keys/` samt `data/` innan något byggs om.

## 2. Skapa keys/keys.json

Nyckelfilen ligger i en katalog på värden: `/root/family-wiki-mcp/keys/keys.json`, mode **600**. Katalogen monteras read-only. En bind-mount av själva filen gör att ett atomiskt `mv` byter inode som containern inte ser. Hemligheten skrivs ut en gång av skriptet och ska inte in i filen, repot eller loggen. Poster har `name`, `sha256`, `scope` (`read` eller `write`), `prefixes` (skriv), `read_prefixes` (valfritt), `expires`, `disabled`, `created` och `author`. Tom `read_prefixes` betyder att läsning omfattar hela wikin.

Om `8429cb7` redan kör och `/root/family-wiki-mcp/keys.json` är en ensam fil, flytta den in i `keys/` innan ombyggnaden. Skapa inte nya nycklar om filen redan har de fem posterna.

```bash
cd /root/family-wiki-mcp
mkdir -p keys
if [ -f keys.json ] && [ ! -L keys.json ]; then
  mv keys.json keys/keys.json
fi
chmod 700 keys
if [ -f keys/keys.json ]; then chmod 600 keys/keys.json; fi
```

Omladdning vid mtime och `SIGHUP` är oförändrad. Byt lista atomiskt inuti katalogen:

```bash
install -m 600 keys/keys.json keys/keys.json.new
# redigera keys/keys.json.new, sedan:
mv -f keys/keys.json.new keys/keys.json
```

Skrivnycklar får serverns prefix `projekt/`, `familj/`, `studier/`, `handbok/`. Läsncyklar har tomma `prefixes`.

Ny installation, när `keys/keys.json` saknas:

```bash
cd /root/family-wiki-mcp
umask 077
mkdir -p keys
chmod 700 keys
install -m 600 /dev/null keys/keys.json
python3 scripts/new_key.py --name grok-web --scope write --author Grok \
  --prefixes projekt/,familj/,studier/,handbok/ >> /tmp/keys-once.txt
python3 scripts/new_key.py --name bots --scope write --author Bots \
  --prefixes projekt/,familj/,studier/,handbok/ >> /tmp/keys-once.txt
python3 scripts/new_key.py --name openwebui --scope read --author OpenWebUI >> /tmp/keys-once.txt
python3 scripts/new_key.py --name jonas --scope write --author Jonas \
  --prefixes projekt/,familj/,studier/,handbok/ >> /tmp/keys-once.txt
python3 scripts/new_key.py --name nuria --scope read --author Nuria >> /tmp/keys-once.txt
```

Klistra in de fem JSON-posterna som en lista i `keys/keys.json`. Spara hemligheterna från `/tmp/keys-once.txt` i lösenordshanteraren och radera filen:

```bash
chmod 600 keys/keys.json
shred -u /tmp/keys-once.txt || rm -f /tmp/keys-once.txt
```

| namn | scope | användning |
| --- | --- | --- |
| grok-web | write | Grok web, OAuth |
| bots | write | xAI-bots, OAuth |
| openwebui | read | OpenWebUI, statisk bearer |
| jonas | write | Jonas, namngiven nyckel |
| nuria | read | Nuria, namngiven nyckel |

`keys.example.json` i repot är avstängd och har inga riktiga hashvärden. Montera den inte.

## 3. Tokenvolym och compose

OAuth-tokens lagras som sha256 i `/root/family-wiki-mcp/data` (i containern `/data/oauth-tokens.json`). Volymen måste överleva omstart, annars måste klienterna logga in igen.

```bash
mkdir -p /root/family-wiki-mcp/data
chmod 700 /root/family-wiki-mcp/data
```

`docker-compose.yml` ska ha:

```yaml
environment:
  KEYS_FILE: /run/secrets/keys/keys.json
  TOKEN_STORE_PATH: /data/oauth-tokens.json
  ALLOW_LEGACY_API_KEY: "true"
  FORWARDED_ALLOW_IPS: "172.20.0.2"
volumes:
  - /root/wiki/.api-token:/run/secrets/wiki-api-token:ro
  - /root/family-wiki-mcp/keys:/run/secrets/keys:ro
  - /root/family-wiki-mcp/data:/data
```

`ALLOW_LEGACY_API_KEY=true` behåller den gamla `MCP_API_KEY` med full skrivrätt och en varning i loggen tills klienterna är omkopplade. Sätt sedan `false` i `.env` eller compose och återskapa containern.

## 4. Ombyggnad

```bash
cd /root/family-wiki-mcp
docker compose up -d --build --force-recreate --no-deps family-wiki-mcp
docker compose ps
docker compose logs --tail=50 family-wiki-mcp
```

Uvicorn startar som en process, utan `--workers`, med `--proxy-headers --forwarded-allow-ips`. Standard är `172.20.0.2` (caddy-proxy). Ändra med `FORWARDED_ALLOW_IPS`. Flera workers skulle få `SIGHUP` att starta om processen i stället för att ladda om nycklarna.

Omladdning av `keys/keys.json` sker när mtime ändras, och direkt på `SIGHUP`. Katalogen är monterad, så bytet syns i containern:

```bash
docker kill --signal HUP family-wiki-mcp
```

Trasig JSON loggas som fel och den tidigare listan ligger kvar.

## 5. Caddy på cloud01

cloud01:s Caddy (`/root/proxy/Caddyfile`) ska lita på hemmets IP så att klientens `X-Forwarded-For` från Minins edge inte ersätts. Lägg detta i den globala Caddy-sektionen och ladda om Caddy:

```caddy
{
	servers {
		trusted_proxies static 158.174.102.122/32
	}
}
```

Minins Caddy är första hoppet och ska inte lita på inkommande `X-Forwarded-For`. uvicorn litar bara på `172.20.0.2`.

## 6. Koppla om klienterna

Gamla åtkomsttoken var själva `MCP_API_KEY`. Nya OAuth-token är opaka, lagras som hash och är kopplade till nyckelns namn. Formuläret tar emot nyckelns hemlighet, inte nyckelns namn som lösenord.

`client_id` styr bara vilka redirect-URI:er som godtas. Grok web skickar `family-wiki-mcp`. Det namnet är ett alias med samma värdar som redan används: suffix `grok.com` (även `www.grok.com`, sökväg `/connectors-oauth-exchange-code`) och `x.ai`. `grok-web` godtar bara `grok.com`. `bots` godtar `x.ai` och `grok.com`.

Namnet i loggen (`client=`) och i tokenposten (`client_id`) är nyckeln som klistrades in. Då går grok-web, bots och jonas att skilja åt även när Grok skickar `family-wiki-mcp`.

### Grok web (OAuth)

1. Öppna anslutningen i Grok och autentisera igen om den gamla token slutade fungera. Befintlig `client_id=family-wiki-mcp` ska vara kvar.
2. Redirect-värd är `grok.com`.
3. Klistra in hemligheten för nyckeln `grok-web` (eller Jonas egen write-nyckel om den personen ska stå som författare).
4. Spara inte om den gamla delade nyckeln.

### Bots (OAuth)

1. Öppna anslutningen "Aldenbäck wiki" och autentisera igen.
2. `client_id` kan vara `bots` eller `family-wiki-mcp`. Redirect-värd är `x.ai` eller `grok.com`.
3. Klistra in hemligheten för nyckeln `bots`.

### OpenWebUI (statisk bearer)

OpenWebUI använder inte OAuth. I tool server "aldenbäck wiki" på chat.aldenback.com, byt bearer till hemligheten för nyckeln `openwebui`. Den har scope `read` och kan inte skapa eller uppdatera sidor.

### Legacy

Tills flaggan stängs av godtas `MCP_API_KEY` som `key=legacy` med full skrivrätt inom serverns prefix. Varje träff loggas som varning, utan att nyckeln skrivs ut.

## 7. Författare

Wiki.js 2.5.315 kör API-nyckeln som användare 1. `pages.create` och `pages.update` saknar fält för författare. Bryggan skriver därför nyckelns `author` (annars `name`) i `comments.create.content`, till exempel `MCP: sidan skapades av Jonas (nyckel grok-web).` Sidans `content` och `description` ändras inte. Se NOTES.md.

Mutationssvaret ber bara om `page { id }`. `locale` och andra fält som Wiki.js kan lämna null trots att schemat säger att de är obligatoriska tas inte med, eftersom GraphQL då svarar `Cannot return null for non-nullable field Page.locale` och nollar sidan även när skrivningen lyckades. Saknas id hämtas det med `singleByPath` och urvalet `{ id }`. Kommentaren körs alltid efter en lyckad skrivning. Misslyckas kommentaren får create och update ändå `result=ok` och svaret innehåller `change_comment_error`.

## 8. Logg

En rad per verktygsanrop:

```text
anrop key=<namn> client=<nyckelns namn eller -> tool=<verktyg> path=<sökväg eller -> result=<ok|denied|error|not_found> ip=<käll-ip>
```

Statisk bearer har `client=-`. OAuth har nyckelns namn, till exempel `grok-web`, `bots` eller `jonas`, inte OAuth-`client_id`. Nyckel och token ska aldrig finnas i raden.

## 9. Verifiering

- [ ] Backupen från steg 1 finns och `.env` ligger bara där, inte i git.
- [ ] `stat -c %a /root/family-wiki-mcp/keys/keys.json` är `600`, och compose monterar katalogen `keys`, inte filen ensam.
- [ ] `keys/keys.json` innehåller grok-web (write), bots (write), openwebui (read), jonas (write) och nuria (read).
- [ ] Containern är healthy och `GET /health` svarar utan nyckel.
- [ ] Anrop utan nyckel ger 401 och `www-authenticate` börjar med `Bearer resource_metadata="`.
- [ ] grok-web kan skapa en sida under `projekt/` och kommentaren nämner författaren. Sidans innehåll är det som skickades.
- [ ] openwebui kan läsa `get_page` men får `write scope required` på `create_page`.
- [ ] nuria kan läsa. jonas kan skriva under sina prefix.
- [ ] En OAuth-token för grok-web och en för bots ligger i `data/oauth-tokens.json` som hash. `key_name` och `client_id` i posten är nyckelns namn, även om Grok skickade `client_id=family-wiki-mcp`. Filen är mode 600 och innehåller inte hemligheten.
- [ ] När jonas sätts till `disabled` och filens mtime ändras (eller `SIGHUP` skickas) slutar både rånyckeln och utfärdade tokens att fungera.
- [ ] Trasig JSON i en kopia ger en fellogg och den gamla listan fungerar fortfarande. Återställ filen efteråt.
- [ ] Loggen har `key=`, `client=`, `tool=`, `path=`, `result=` och `ip=` och innehåller inte nyckeln eller token.
- [ ] Efter Caddy-omladdningen är `ip=` klientens adress, inte bara `158.174.102.122`.
- [ ] `ALLOW_LEGACY_API_KEY=false` när de tre klienterna är omkopplade, och den gamla nyckeln då ger 401.
