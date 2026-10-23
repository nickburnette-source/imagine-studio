#!/usr/bin/env bash
# Start Imagine on IMAGINE_HOST:IMAGINE_PORT from .env
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PID_FILE="$ROOT/.imagine-studio.pid"
LOG_FILE="$ROOT/.imagine-studio.log"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ROOT/.env" | sed 's/\r$//')
  set +a
fi

HOST="${IMAGINE_HOST:-127.0.0.1}"
PORT="${IMAGINE_PORT:-8070}"
BACKGROUND=0
for arg in "$@"; do
  case "$arg" in
    --bg|-d|--daemon|background) BACKGROUND=1 ;;
    --help|-h)
      cat <<EOF
Usage: ./start.sh [--bg]

  (default)   Run in the foreground. Ctrl+C stops cleanly.
  --bg / -d   Run in the background; logs → .imagine-studio.log
              Stop with ./stop.sh

Env (from .env if present):
  IMAGINE_HOST          default 127.0.0.1
  IMAGINE_PORT          default 8070
  IMAGINE_OPEN_BROWSER  1 opens one Chrome tab (default 0, do not open)
EOF
      exit 0
      ;;
  esac
done

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  echo "error: no python3 found (python3 -m venv .venv && .venv/bin/pip install -e .)" >&2
  exit 1
fi

if [[ -f "$PID_FILE" ]]; then
  old="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${old:-}" ]] && kill -0 "$old" 2>/dev/null; then
    echo "Imagine already running (pid $old) → http://${HOST}:${PORT}"
    echo "Stop with: ./stop.sh"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

if command -v lsof >/dev/null 2>&1; then
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "error: port $PORT is already in use:" >&2
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2 || true
    echo "Stop it with: ./stop.sh" >&2
    exit 1
  fi
fi

if ! "$PYTHON" -c "import app" 2>/dev/null; then
  echo "Installing package into venv (pip install -e .)…"
  "$PYTHON" -m pip install -e . -q
fi

echo "Starting Imagine"
echo "  python: $PYTHON"
echo "  url:    http://${HOST}:${PORT}"
echo "  cwd:    $ROOT"

open_browser() {
  if [[ "${IMAGINE_OPEN_BROWSER:-0}" != "1" ]]; then
    return 0
  fi
  if [[ "$(uname -s)" != "Darwin" ]]; then
    return 0
  fi
  open "http://${HOST}:${PORT}" >/dev/null 2>&1 || true
}

if [[ "$BACKGROUND" == "1" ]]; then
  nohup "$PYTHON" -m app.main >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  echo "  pid:    $(cat "$PID_FILE")"
  echo "  log:    $LOG_FILE"
  open_browser
  exit 0
fi

open_browser
exec "$PYTHON" -m app.main
