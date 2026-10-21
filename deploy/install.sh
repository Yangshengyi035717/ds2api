#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/www/dk_project/dk_app/dsapi}"

cd "$APP_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required" >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose plugin is required" >&2
  exit 1
fi

if [[ ! -f Dockerfile || ! -f docker-compose.yml ]]; then
  echo "deployment files are missing in $APP_DIR" >&2
  exit 1
fi

mkdir -p data
chmod 700 data

if [[ ! -f .env ]]; then
  echo ".env is missing; copy and configure .env.example first" >&2
  exit 1
fi

chmod 600 .env

docker compose up -d --build

for _ in $(seq 1 60); do
  if python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3011/health', timeout=3).read()" >/dev/null 2>&1; then
    echo "DS2API is healthy on 127.0.0.1:3011"
    exit 0
  fi
  sleep 2
done

echo "DS2API did not become healthy in time" >&2
docker compose ps >&2
docker compose logs --tail=200 dsapi >&2
exit 1
