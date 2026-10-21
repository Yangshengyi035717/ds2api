# Docker Deployment

Target layout:

```text
/www/dk_project/dk_app/dsapi
├── docker-compose.yml
├── Dockerfile
├── .env
└── data/
```

The container exposes `8080` internally and binds host port `3011`.

## Prepare

```bash
cd /www/dk_project/dk_app/dsapi
mkdir -p data
cp .env.example .env
chmod 700 data
chmod 600 .env
```

Required production settings:

```env
HOST=0.0.0.0
PORT=8080
DEEPSEEK_ADMIN_PASSWORD=<strong-random-password>
ALLOW_INSECURE_PUBLIC_DEFAULTS=false
KEEPALIVE_SHARED_SECRET=<random-shared-secret>
```

The application refuses to start with a weak admin password on a public bind.

Keep the following runtime files on the persistent volume:

```text
data/accounts.json
data/api_keys.sqlite3
data/runtime_config.json
data/proxies.json
data/admin_auth.json
```

`data/admin_auth.json` is created when the password is changed in WebUI.
It contains only a salted PBKDF2-SHA256 verifier; removing it restores the
`DEEPSEEK_ADMIN_PASSWORD` fallback.

If an encrypted account store is used, the same `DEEPSEEK_ENCRYPTION_KEY` must be present in `.env`.

## Start

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f --tail=100 dsapi
```

## Verify

```bash
curl -fsS http://127.0.0.1:3011/health
curl -fsS https://dsapi.ysyun.xyz/health
```

Expected:

```json
{"status":"ok"}
```

For Nginx, ensure SSE buffering is disabled. A reference config is provided in
`deploy/nginx-dsapi.conf`; at minimum configure:

```nginx
proxy_buffering off;
proxy_cache off;
proxy_request_buffering off;
proxy_read_timeout 300s;
proxy_send_timeout 300s;
```

## Browser login sidecar (noVNC)

The optional `browser-login` service runs Chromium under Xvfb and exposes it
through noVNC. It is bound to `127.0.0.1:6080` only.

1. Generate a shared secret and put it in `.env`:

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
   # BROWSER_LOGIN_SHARED_SECRET=...
   ```

2. Start both services:

   ```bash
   docker compose up -d --build
   ```

3. Install the two Nginx files (the BT panel build lacks
   `ngx_http_auth_request_module`, so the check is done locally with
   `ngx_lua` + `ngx.hmac_sha1`):

   ```bash
   install -m 644 deploy/nginx-browser-login.lua /www/server/nginx/conf/browser-login.lua
   # same value as BROWSER_LOGIN_SHARED_SECRET
   install -m 640 -o root -g www /dev/stdin /www/server/nginx/conf/browser-login.secret <<< "$BROWSER_LOGIN_SHARED_SECRET"
   install -m 644 deploy/nginx-browser-login.conf \
     /www/server/panel/vhost/nginx/extension/dsapi.ysyun.xyz/browser-login.conf
   nginx -t && systemctl reload nginx
   ```

   The token is `<exp>.<hmac_sha1_hex(secret, exp)>`; Nginx verifies it
   before proxying any noVNC asset or WebSocket, without an auth subrequest
   (which would break on HTTP/2 with this build).

4. Open WebUI → 浏览器登录 → 启动登录. After the operator finishes the
   DeepSeek login, the sidecar reads `userToken`, the device id and cookies
   from the browser context and posts them to the internal ingest endpoint.

   If the noVNC screen shows `403 ERROR / Request blocked` from the upstream
   CDN, the server's datacenter IP is blocked. Set an egress proxy for
   Chromium and restart the sidecar:

   ```env
   BROWSER_LOGIN_PROXY=http://user:pass@host:port
   # or socks5://host:port
   ```

   ```bash
   docker compose up -d --force-recreate browser-login
   ```

The sidecar never receives the DS2API admin session token; the ingest endpoint
uses `BROWSER_LOGIN_SHARED_SECRET` and is not exposed through Nginx.

## Update

```bash
docker compose up -d --build
```

Persistent data under `./data` is not removed by rebuilds.
