# DeepSeek / z.ai Session Keepalive (protocol-based)

Keeps a DeepSeek Chat or z.ai web session alive **on the server** and pushes
refreshed cookies back into the DS2API account pool — without a browser.
The worker reads `provider` from the account and selects the matching probe.

## Why the copied token decays

The DeepSeek web token is an opaque 64-character string bound to the browser
session that created it. The session cookies include Huawei WAF cookies
(`HWWAFSESID`, `HWWAFSESTIME`) plus `ds_session_id`, which are renewed only
while the originating browser keeps talking to the site. Copying them from a
laptop means:

1. the WAF cookie stops being renewed the moment that browser closes;
2. the upstream sees one session used from two different egress IPs.

## Why not a browser

Measured against the live site:

| Request | Result |
|---|---|
| `GET /` | **202** — WAF challenge page, needs JS |
| `POST /api/v0/chat/create_pow_challenge` | **200** — API path, no challenge |
| `GET /api/v0/users/current` | `40003 Authorization Failed` on a dead token, `200` on a live one |

The API surface is reachable without executing JavaScript — which is exactly
what `adapter.py` already relies on. A headless Chrome would add ~400 MB RSS and
a 2 GB image for no functional gain.

## What it does per cycle

1. Read the account's stored token + cookies from DS2API.
2. Probe with the provider-specific read-only endpoint:
   - DeepSeek: `GET /api/v0/users/current`
   - z.ai: `GET /api/v1/chats/?page=1&type=all&channel=zai-web`
3. A `200` proves the login is alive; curl_cffi merges refreshed WAF/session
   cookies into the jar.
4. If the cookie set changed, update the account through
   `PUT /admin/api/accounts/{id}`.
5. DeepSeek code `40003` or z.ai HTTP `401/403` means the login is dead:
   print `SESSION_INVALID` and stop.

It never creates a chat session and never sends a prompt, so it consumes no
generation quota and does not look like automation. z.ai requests reuse the
account's bound proxy when one is configured.

## Layout

```text
/www/dk_project/dk_app/dsapi/keepalive
├── docker-compose.yml
├── Dockerfile
├── keepalive.py
└── requirements.txt
```

## Run

```bash
cd /www/dk_project/dk_app/dsapi/keepalive
export DS2API_ADMIN_TOKEN='<DS2API admin session token>'
export DS2API_ACCOUNT_ID='<account id>'
docker compose up -d --build
docker compose logs -f
```

Single cycle (useful for debugging):

```bash
docker compose run --rm keepalive python keepalive.py --once
```

## Output semantics

| Output | Exit | Meaning | Action |
|---|---|---|---|
| `ALIVE` | 0 | token valid, cookies unchanged | none |
| `REFRESHED` | 0 | token valid, new cookies persisted | none |
| `TRANSIENT` | 3 | WAF challenge / upstream hiccup | retried next cycle |
| `SESSION_INVALID` | 2 | login is dead (`40003` / z.ai `401`/`403`) | sign in again, update the account |
