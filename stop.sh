#!/usr/bin/env bash
# Stop Imagine started by ./start.sh (or anything listening on IMAGINE_PORT)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PID_FILE="$ROOT/.imagine-studio.pid"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ROOT/.env" | sed 's/\r$//')
  set +a
fi

PORT="${IMAGINE_PORT:-8070}"

stop_pid() {
  local pid="$1"
  local label="${2:-process}"
  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    return 1
  fi
  echo "Stopping $label (pid $pid)…"
  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "  stopped."
      return 0
    fi
    sleep 0.15
  done
  echo "  still running — kill -9"
  kill -KILL "$pid" 2>/dev/null || true
  sleep 0.1
  if kill -0 "$pid" 2>/dev/null; then
    echo "  warning: pid $pid still alive" >&2
    return 1
  fi
  echo "  force-stopped."
  return 0
}

if [[ -f "$PID_FILE" ]]; then
  pid="$(tr -d '[:space:]' <"$PID_FILE" || true)"
  stop_pid "$pid" "Imagine (pid file)" || true
  rm -f "$PID_FILE"
fi

if command -v lsof >/dev/null 2>&1; then
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    stop_pid "$pid" "listener on :$PORT" || true
  done < <(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
fi

echo "Imagine is not running on :$PORT"
