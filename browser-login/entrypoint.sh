#!/usr/bin/env bash
set -euo pipefail

# The virtual display is shared by Chromium and x11vnc. noVNC only exposes the
# display through websockify; no host port is published by default.
Xvfb "${DISPLAY}" -screen 0 "${BROWSER_LOGIN_SCREEN:-1440x900x24}" -ac \
  -nolisten tcp >/tmp/xvfb.log 2>&1 &

for _ in $(seq 1 30); do
  if xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done

x11vnc -display "${DISPLAY}" -forever -shared -nopw -localhost \
  -rfbport 5900 >/tmp/x11vnc.log 2>&1 &

# Keep the WebSocket port closed until a valid view token has been checked by
# DS2API. Nginx handles that check; websockify itself only listens on loopback.
websockify --web=/usr/share/novnc 6080 127.0.0.1:5900 \
  >/tmp/websockify.log 2>&1 &

cleanup() {
  pkill -P $$ 2>/dev/null || true
}
trap cleanup EXIT INT TERM

exec python -m uvicorn browser_login:app --host 0.0.0.0 --port 8000
