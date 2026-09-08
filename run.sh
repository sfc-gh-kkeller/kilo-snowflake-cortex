#!/usr/bin/env bash
# Launch the Snowflake Cortex proxy and Kilo Code together.
#
#   ./run.sh                start proxy + kilo
#   ./run.sh --no-kilo      start proxy only
#   ./run.sh --stop         stop the proxy
#   ./run.sh --status       show proxy health
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROXY="$SCRIPT_DIR/proxy/snowflake-cortex-proxy.py"
PORT="${PROXY_PORT:-8080}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/kilo-snowflake-cortex"
PIDFILE="$STATE_DIR/proxy.pid"
LOG="$STATE_DIR/proxy.log"

GREEN=$'\033[32m' YELLOW=$'\033[33m' RED=$'\033[31m' BLUE=$'\033[34m' BOLD=$'\033[1m' RESET=$'\033[0m'
ok()   { printf '%s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
info() { printf '%s→%s %s\n' "$BLUE" "$RESET" "$*"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$RESET" "$*"; }
fail() { printf '%s✗%s %s\n' "$RED" "$RESET" "$*" >&2; }

mkdir -p "$STATE_DIR"

# --- helpers -----------------------------------------------------------------

find_python() {
  for c in python3 python; do
    command -v "$c" >/dev/null 2>&1 && { echo "$c"; return; }
  done
  fail "python3 is required but not found on PATH"; exit 1
}

find_kilo() {
  [[ -x "$HOME/.kilo/bin/kilo" ]] && { echo "$HOME/.kilo/bin/kilo"; return; }
  command -v kilo 2>/dev/null && return
  return 1
}

proxy_pid() {
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1
}

is_healthy() {
  curl -fsS -m 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1
}

start_proxy() {
  if is_healthy; then
    ok "proxy already running on port $PORT"
    return 0
  fi

  local occupant
  occupant="$(proxy_pid || true)"
  if [[ -n "$occupant" ]]; then
    fail "port $PORT is in use by PID $occupant but not answering /health"
    fail "kill it first or set PROXY_PORT=8081"
    return 1
  fi

  [[ -f "$PROXY" ]] || { fail "proxy not found at $PROXY"; return 1; }

  local PY
  PY="$(find_python)"
  info "starting proxy on port $PORT (log: $LOG)"
  PORT="$PORT" nohup "$PY" "$PROXY" >"$LOG" 2>&1 &
  echo $! >"$PIDFILE"

  for _ in $(seq 1 40); do
    if is_healthy; then
      ok "proxy up on http://127.0.0.1:$PORT"
      return 0
    fi
    if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      fail "proxy exited during startup. Last lines:"
      tail -20 "$LOG" >&2
      return 1
    fi
    sleep 0.5
  done

  fail "proxy did not become healthy in 20s"
  tail -10 "$LOG" >&2
  return 1
}

stop_proxy() {
  local pid
  pid="$(proxy_pid || true)"
  if [[ -z "$pid" ]]; then
    warn "proxy not running"
    rm -f "$PIDFILE"
    return 0
  fi
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.2
  done
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
  rm -f "$PIDFILE"
  ok "stopped proxy (PID $pid)"
}

show_status() {
  if is_healthy; then
    ok "proxy healthy on port $PORT"
    curl -fsS -m 3 "http://127.0.0.1:$PORT/health" 2>/dev/null | python3 -m json.tool 2>/dev/null || true
  else
    local pid; pid="$(proxy_pid || true)"
    if [[ -n "$pid" ]]; then
      fail "port $PORT occupied (PID $pid) but /health fails"
    else
      warn "proxy not running"
    fi
    return 1
  fi
}

# --- main --------------------------------------------------------------------

LAUNCH_KILO=1
ACTION="start"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-kilo) LAUNCH_KILO=0; shift ;;
    --stop)    ACTION="stop"; shift ;;
    --status)  ACTION="status"; shift ;;
    -h|--help)
      sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) shift ;;
  esac
done

printf '%s\n' "════════════════════════════════════════════════════"
printf '%s Kilo x Snowflake Cortex%s\n' "$BOLD" "$RESET"
printf '%s\n' "════════════════════════════════════════════════════"

case "$ACTION" in
  stop)   stop_proxy; exit 0 ;;
  status) show_status; exit $? ;;
esac

# --- config check: run setup if missing or incomplete -------------------------
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/kilo/kilo.json"
needs_setup=0

if [[ ! -f "$CONFIG" ]]; then
  needs_setup=1
else
  PY="$(find_python)"
  "$PY" -c "
import json, sys
cfg = json.load(open('$CONFIG'))
p = cfg.get('provider', {})
sf = p.get('snowflake-cortex', {})
oai = p.get('openai', {})
assert sf.get('account'), 'no account'
assert (sf.get('auth') or {}).get('pat'), 'no PAT'
assert '127.0.0.1' in (oai.get('options') or {}).get('baseURL', ''), 'not pointed at proxy'
" 2>/dev/null || needs_setup=1
fi

if [[ "$needs_setup" == "1" ]]; then
  info "config missing or incomplete — running setup..."
  "$SCRIPT_DIR/setup.sh" || { fail "setup failed"; exit 1; }
fi

start_proxy || exit 1

if [[ "$LAUNCH_KILO" == "0" ]]; then
  ok "proxy is up. Skipping Kilo (--no-kilo)."
  info "stop later: $0 --stop"
  exit 0
fi

KILO="$(find_kilo || true)"
if [[ -z "$KILO" ]]; then
  warn "Kilo CLI not found."
  echo
  echo "  Install:  npm install -g @kilocode/cli"
  echo "  Or:       curl -fsSL https://kilo.ai/cli/install | bash"
  echo
  echo "  The proxy is running; install Kilo then run:  kilo"
  exit 0
fi

ok "launching Kilo"
echo
exec "$KILO"
