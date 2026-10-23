#!/usr/bin/env bash
# Start Rally Builder (NiceGUI) on RALLY_HOST:RALLY_PORT from .env
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PID_FILE="$ROOT/.rally-builder.pid"
LOG_FILE="$ROOT/.rally-builder.log"

# Load .env (simple KEY=VALUE; ignores comments / blanks)
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # bash 3.2 (macOS) cannot source a process substitution, so RALLY_PORT
  # never loaded and this app tried to bind 8080, which Rally Builder uses.
  _env_tmp="$(mktemp)"
  grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ROOT/.env" | sed 's/\r$//' > "$_env_tmp"
  # shellcheck disable=SC1090
  source "$_env_tmp"
  rm -f "$_env_tmp"
  unset _env_tmp
  set +a
fi

HOST="${RALLY_HOST:-127.0.0.1}"
PORT="${RALLY_PORT:-8080}"
# Default: foreground. Pass --bg / -d / --daemon to background.
BACKGROUND=0
for arg in "$@"; do
  case "$arg" in
    --bg|-d|--daemon|background) BACKGROUND=1 ;;
    --help|-h)
      cat <<EOF
Usage: ./start.sh [--bg]

  (default)   Run in the foreground. Ctrl+C stops cleanly.
  --bg / -d   Run in the background; logs → .rally-builder.log
              Stop with ./stop.sh

Env (from .env if present):
  RALLY_HOST          default 127.0.0.1
  RALLY_PORT          default 8080
  RALLY_OPEN_BROWSER  1 reuse/reload one Chrome tab (0 = don't open)
  AWS_PROFILE         used by S3 publish
EOF
      exit 0
      ;;
  esac
done

# Prefer project venv
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  echo "error: no python3 found (create .venv with: python3 -m venv .venv && .venv/bin/pip install -e .)" >&2
  exit 1
fi

# Already running?
if [[ -f "$PID_FILE" ]]; then
  old="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${old:-}" ]] && kill -0 "$old" 2>/dev/null; then
    echo "Rally Builder already running (pid $old) → http://${HOST}:${PORT}"
    echo "Stop with: ./stop.sh"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

if command -v lsof >/dev/null 2>&1; then
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "error: port $PORT is already in use:" >&2
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2 || true
    echo "Stop it with: ./stop.sh   (or free the port manually)" >&2
    exit 1
  fi
fi

# Ensure package is importable
if ! "$PYTHON" -c "import app" 2>/dev/null; then
  echo "Installing package into venv (pip install -e .)…"
  "$PYTHON" -m pip install -e . -q
fi

echo "Starting Rally Builder"
echo "  python: $PYTHON"
echo "  url:    http://${HOST}:${PORT}"
echo "  cwd:    $ROOT"
if [[ -n "${AWS_PROFILE:-}" ]]; then
  echo "  aws:    profile=${AWS_PROFILE}"
  if command -v aws >/dev/null 2>&1; then
    if ! aws sts get-caller-identity --profile "${AWS_PROFILE}" >/dev/null 2>&1; then
      echo ""
      echo "warning: AWS SSO for profile '${AWS_PROFILE}' is expired or missing."
      echo "  Video staging + publish need a valid session. Run:"
      echo "    aws sso login --profile ${AWS_PROFILE}"
      echo "    aws sts get-caller-identity --profile ${AWS_PROFILE}"
      echo "  Then start again (or continue and fix before generating video)."
      echo ""
    else
      echo "  aws:    SSO identity ok"
    fi
  fi
fi

open_or_reload_studio() {
  # Reuse a Chrome tab on this URL (reload it, close extras). Open one if none.
  # Set RALLY_OPEN_BROWSER=0 to skip.
  if [[ "${RALLY_OPEN_BROWSER:-1}" == "0" ]]; then
    return 0
  fi
  if [[ "$(uname -s)" != "Darwin" ]]; then
    return 0
  fi
  local url="http://${HOST}:${PORT}"
  osascript - "$url" <<'APPLESCRIPT' >/dev/null 2>&1 || true
on run argv
  set target to item 1 of argv
  tell application "Google Chrome"
    if it is not running then
      activate
      open location target
      return
    end if
    set kept to false
    set winCount to count of windows
    repeat with w from winCount to 1 by -1
      set tabCount to count of tabs of window w
      repeat with t from tabCount to 1 by -1
        try
          set u to URL of tab t of window w as string
          if u starts with target then
            if kept is false then
              set active tab index of window w to t
              set index of window w to 1
              tell tab t of window w to reload
              set kept to true
            else
              close tab t of window w
            end if
          end if
        end try
      end repeat
    end repeat
    if kept is false then
      open location target
    end if
  end tell
end run
APPLESCRIPT
}

export PYTHONUNBUFFERED=1

if [[ "$BACKGROUND" -eq 1 ]]; then
  # Detach: nohup + setsid when available
  if command -v setsid >/dev/null 2>&1; then
    setsid nohup "$PYTHON" -m app.main >>"$LOG_FILE" 2>&1 &
  else
    nohup "$PYTHON" -m app.main >>"$LOG_FILE" 2>&1 &
  fi
  pid=$!
  echo "$pid" >"$PID_FILE"
  # Wait briefly for listen
  for _ in $(seq 1 30); do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "error: process exited early — see $LOG_FILE" >&2
      tail -n 40 "$LOG_FILE" >&2 || true
      rm -f "$PID_FILE"
      exit 1
    fi
    if command -v lsof >/dev/null 2>&1; then
      if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        break
      fi
    fi
    sleep 0.2
  done
  echo "Running in background (pid $pid)"
  echo "  log:  $LOG_FILE"
  echo "  stop: ./stop.sh"
  echo "  open: http://${HOST}:${PORT}"
  open_or_reload_studio
  exit 0
fi

# Foreground: write pid for stop.sh, trap cleanup
echo $$ >"$PID_FILE"
cleanup() {
  rm -f "$PID_FILE"
}
trap cleanup EXIT INT TERM

echo "Foreground mode — Ctrl+C stops cleanly."
# Wait for listen, then reuse/reload one Chrome tab (NiceGUI will not open its own).
(
  for _ in $(seq 1 50); do
    if command -v lsof >/dev/null 2>&1; then
      if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        open_or_reload_studio
        exit 0
      fi
    fi
    sleep 0.2
  done
) >/dev/null 2>&1 &
exec "$PYTHON" -m app.main
