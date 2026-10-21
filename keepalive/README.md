# DeepSeek Session Keepalive (protocol-based)

Keeps a DeepSeek Chat web session alive **on the server** and pushes refreshed
cookies back into the DS2API account pool — without a browser.

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
2. `GET /api/v0/users/current` with those credentials (the cheap probe). A
   `200` proves the token is alive; curl_cffi merges any refreshed
   `Set-Cookie` into the jar.
3. If the jar changed, `PUT /admin/api/accounts/{id}` to persist the new
   cookie string.
4. On `40003`, print `SESSION_INVALID` and stop — a dead login needs a human.

It never creates a chat session and never sends a prompt, so it consumes no
upstream quota and does not look like automation.

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
| `SESSION_INVALID` | 2 | login is dead (`40003`) | sign in again, update the account |
