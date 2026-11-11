#!/usr/bin/env bash
# Legion Control - portable launcher.  Run it as your normal user (NOT with sudo):
#   ./start.sh          start the app (asks for your password once)
#   ./start.sh --probe  print what hardware controls were detected (no root needed)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$DIR/app/backend.py"

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "python3 is required (sudo dnf install python3)" >&2
  exit 1
fi
if [ ! -f "$BACKEND" ]; then
  echo "Missing $BACKEND - the app folder is incomplete." >&2
  exit 1
fi

if [ "${1:-}" = "--probe" ]; then
  exec "$PY" "$BACKEND" --probe
fi

if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ]; then
  echo "Please run ./start.sh without sudo; it asks for your password itself." >&2
  exit 1
fi

TOKEN="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(24))')"
PORT="$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"

if [ "$(id -u)" -eq 0 ]; then
  ELEVATE=()
elif command -v pkexec >/dev/null 2>&1 && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  ELEVATE=(pkexec)
else
  ELEVATE=(sudo)
fi

# The token is passed on stdin so it never shows up in the process list.
printf '%s\n' "$TOKEN" | "${ELEVATE[@]}" "$PY" "$BACKEND" --port "$PORT" &
BACKEND_PID=$!

# The backend runs as root after pkexec/sudo, so `kill -0` from this (user) shell
# fails with EPERM even while it is alive. Check /proc instead.
alive() { [ -d "/proc/$1" ]; }

echo "Waiting for authorization..."
for _ in $(seq 1 240); do
  if ! alive "$BACKEND_PID"; then
    echo "Backend did not start (authorization cancelled or failed)." >&2
    exit 1
  fi
  if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
    break
  fi
  sleep 0.5
done

URL="http://127.0.0.1:$PORT/#$TOKEN"
echo "Opening $URL"
echo "Use the 'Close app' button (or close the tab; it stops on its own after 3 minutes)."
xdg-open "$URL" >/dev/null 2>&1 || echo "Open this URL in your browser: $URL"

wait "$BACKEND_PID" || true
