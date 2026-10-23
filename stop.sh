#!/usr/bin/env bash
# Stop Imagine Studio started by ./start.sh (or anything listening on RALLY_PORT)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PID_FILE="$ROOT/.imagine-studio.pid"
LOG_FILE="$ROOT/.imagine-studio.log"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # bash 3.2 (macOS) cannot source a process substitution, so RALLY_PORT
  # never loaded and stop.sh would use the wrong port.
  _env_tmp="$(mktemp)"
  grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ROOT/.env" | sed 's/\r$//' > "$_env_tmp"
  # shellcheck disable=SC1090
  source "$_env_tmp"
  rm -f "$_env_tmp"
  unset _env_tmp
  set +a
fi

PORT="${RALLY_PORT:-8070}"
stopped=0

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

# 1) PID file from start.sh
if [[ -f "$PID_FILE" ]]; then
  pid="$(tr -d '[:space:]' <"$PID_FILE" || true)"
  if stop_pid "$pid" "Imagine Studio"; then
    stopped=1
  fi
  rm -f "$PID_FILE"
fi

# 2) Anything still listening on the app port (orphans / other starts)
if command -v lsof >/dev/null 2>&1; then
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    if stop_pid "$pid" "listener on :$PORT"; then
      stopped=1
    fi
  done < <(lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
fi

# 3) Fallback: python -m app.main (best-effort, macOS-safe pgrep)
if command -v pgrep >/dev/null 2>&1; then
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    # Only match this repo's cwd if possible
    if command -v lsof >/dev/null 2>&1; then
      cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
      if [[ -n "$cwd" && "$cwd" != "$ROOT" ]]; then
        continue
      fi
    fi
    if stop_pid "$pid" "python -m app.main"; then
      stopped=1
    fi
  done < <(pgrep -f '[Pp]ython.*-m app\.main' 2>/dev/null || true)
fi

if [[ "$stopped" -eq 1 ]]; then
  echo "Imagine Studio is stopped."
  if [[ -f "$LOG_FILE" ]]; then
    echo "  log kept at $LOG_FILE"
  fi
  exit 0
fi

echo "Nothing to stop (no pid file, nothing on :$PORT)."
exit 0
