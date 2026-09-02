#!/usr/bin/env bash
# First-time setup: generate ~/.config/kilo/kilo.json for the Snowflake
# Cortex proxy.
#
#   ./setup.sh                       interactive
#   ./setup.sh --account X --user Y  non-interactive (needs SNOWFLAKE_PAT env)
#
# Safe to re-run: backs up existing config, preserves other providers/MCP servers.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROXY="$SCRIPT_DIR/proxy/snowflake-cortex-proxy.py"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/kilo"
CONFIG="$CONFIG_DIR/kilo.json"
PORT="${PROXY_PORT:-8080}"

GREEN=$'\033[32m' YELLOW=$'\033[33m' RED=$'\033[31m' BLUE=$'\033[34m' BOLD=$'\033[1m' RESET=$'\033[0m'
ok()   { printf '%s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
info() { printf '%s→%s %s\n' "$BLUE" "$RESET" "$*"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$RESET" "$*"; }
fail() { printf '%s✗%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

# --- Parse args --------------------------------------------------------------
ACCOUNT="" USER_NAME="" PAT="" WAREHOUSE="" ROLE="" NON_INTERACTIVE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --account)   ACCOUNT="$2"; shift 2 ;;
    --user)      USER_NAME="$2"; shift 2 ;;
    --pat)       PAT="$2"; shift 2 ;;
    --warehouse) WAREHOUSE="$2"; shift 2 ;;
    --role)      ROLE="$2"; shift 2 ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    -h|--help)
      echo "Usage: ./setup.sh [--account X] [--user Y] [--warehouse W] [--role R] [--non-interactive]"
      echo "       PAT can be passed via --pat or SNOWFLAKE_PAT env var."
      exit 0 ;;
    *) shift ;;
  esac
done

# --- Resolve from env --------------------------------------------------------
ACCOUNT="${ACCOUNT:-${SNOWFLAKE_ACCOUNT:-}}"
USER_NAME="${USER_NAME:-${SNOWFLAKE_USER:-}}"
PAT="${PAT:-${SNOWFLAKE_PAT:-}}"
WAREHOUSE="${WAREHOUSE:-${SNOWFLAKE_WAREHOUSE:-}}"
ROLE="${ROLE:-${SNOWFLAKE_ROLE:-}}"

# --- Try reading from existing config ----------------------------------------
if [[ -f "$CONFIG" ]]; then
  info "found existing config at $CONFIG"
  PY="$(command -v python3 || command -v python || true)"
  if [[ -n "$PY" ]]; then
    ACCOUNT="${ACCOUNT:-$("$PY" -c "import json;c=json.load(open('$CONFIG'));print(c.get('provider',{}).get('snowflake-cortex',{}).get('account',''))" 2>/dev/null || true)}"
    USER_NAME="${USER_NAME:-$("$PY" -c "import json;c=json.load(open('$CONFIG'));print(c.get('provider',{}).get('snowflake-cortex',{}).get('user',''))" 2>/dev/null || true)}"
    PAT="${PAT:-$("$PY" -c "import json;c=json.load(open('$CONFIG'));print(c.get('provider',{}).get('snowflake-cortex',{}).get('auth',{}).get('pat',''))" 2>/dev/null || true)}"
    WAREHOUSE="${WAREHOUSE:-$("$PY" -c "import json;c=json.load(open('$CONFIG'));print(c.get('provider',{}).get('snowflake-cortex',{}).get('warehouse',''))" 2>/dev/null || true)}"
    ROLE="${ROLE:-$("$PY" -c "import json;c=json.load(open('$CONFIG'));print(c.get('provider',{}).get('snowflake-cortex',{}).get('role',''))" 2>/dev/null || true)}"
  fi
fi

# --- Interactive prompts if needed -------------------------------------------
if [[ "$NON_INTERACTIVE" == "0" ]]; then
  printf '\n%s%s Kilo x Snowflake Cortex — Setup%s\n\n' "$BOLD" "$BLUE" "$RESET"

  if [[ -z "$ACCOUNT" ]]; then
    printf 'Snowflake account identifier (e.g. MYORG-MYACCOUNT): '
    read -r ACCOUNT
  else
    info "account: $ACCOUNT"
  fi

  if [[ -z "$USER_NAME" ]]; then
    printf 'Snowflake user (login name / email): '
    read -r USER_NAME
  else
    info "user: $USER_NAME"
  fi

  if [[ -z "$PAT" ]]; then
    printf 'Programmatic access token (PAT, input hidden): '
    read -rs PAT
    echo
  else
    info "PAT: ****${PAT: -4}"
  fi

  if [[ -z "$WAREHOUSE" ]]; then
    printf 'Warehouse (blank to skip): '
    read -r WAREHOUSE
  else
    info "warehouse: $WAREHOUSE"
  fi

  if [[ -z "$ROLE" ]]; then
    printf 'Role (blank for default): '
    read -r ROLE
  else
    info "role: $ROLE"
  fi
fi

# --- Validate ----------------------------------------------------------------
[[ -n "$ACCOUNT" ]]   || fail "account is required (--account or SNOWFLAKE_ACCOUNT)"
[[ -n "$USER_NAME" ]] || fail "user is required (--user or SNOWFLAKE_USER)"
[[ -n "$PAT" ]]       || fail "PAT is required (--pat or SNOWFLAKE_PAT)"

# --- Check dependencies ------------------------------------------------------
PY="$(command -v python3 || command -v python || true)"
[[ -n "$PY" ]] || fail "python3 is required"

KILO="$(command -v kilo 2>/dev/null || echo "${HOME}/.kilo/bin/kilo")"
if [[ ! -x "$KILO" ]]; then
  warn "Kilo CLI not found. Install: npm install -g @kilocode/cli"
fi

# --- Generate model catalog from proxy ---------------------------------------
info "generating model catalog from proxy..."
MODELS_JSON="$("$PY" "$PROXY" --print-kilo-models 2>/dev/null)" || fail "could not read model catalog from proxy"

# --- Build kilo.json ---------------------------------------------------------
info "writing config..."

mkdir -p "$CONFIG_DIR"

# Back up existing config
if [[ -f "$CONFIG" ]]; then
  BACKUP="${CONFIG}.bak-$(date +%Y%m%d-%H%M%S)"
  cp "$CONFIG" "$BACKUP"
  info "backed up existing config to $(basename "$BACKUP")"
fi

# Use python to merge into existing config (preserves other providers, mcp servers, etc.)
"$PY" - "$CONFIG" "$ACCOUNT" "$USER_NAME" "$PAT" "$WAREHOUSE" "$ROLE" "$PORT" "$MODELS_JSON" <<'PYEOF'
import json, sys, os
from collections import OrderedDict

config_path, account, user, pat, warehouse, role, port, models_json = sys.argv[1:]
base_url = f"http://127.0.0.1:{port}/v1"
models = json.loads(models_json, object_pairs_hook=OrderedDict)

# Load existing or start fresh
cfg = OrderedDict()
if os.path.exists(config_path):
    try:
        cfg = json.loads(open(config_path).read(), object_pairs_hook=OrderedDict)
    except Exception:
        pass

cfg.setdefault("$schema", "https://app.kilo.ai/config.json")
cfg["model"] = "openai/claude-opus-5"
cfg["small_model"] = "openai/claude-haiku-4-5"

provider = cfg.setdefault("provider", OrderedDict())

# Snowflake Cortex credentials (read by the proxy)
provider["snowflake-cortex"] = OrderedDict([
    ("account", account),
    ("user", user),
    ("auth", OrderedDict([("type", "pat"), ("pat", pat)])),
    ("warehouse", warehouse),
    ("role", role),
])

# OpenAI provider pointed at the local proxy
provider["openai"] = OrderedDict([
    ("name", "Snowflake Cortex"),
    ("api", base_url),
    ("options", OrderedDict([("baseURL", base_url), ("apiKey", "sk-local-proxy")])),
    ("models", models),
])

with open(config_path, "w") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.chmod(config_path, 0o600)
PYEOF

ok "wrote $CONFIG (mode 600)"

# --- Summary -----------------------------------------------------------------
echo
printf '%s\n' "════════════════════════════════════════════════════"
ok "Setup complete"
printf '%s\n' "════════════════════════════════════════════════════"
echo
echo "  Account:   $ACCOUNT"
echo "  User:      $USER_NAME"
echo "  Warehouse: ${WAREHOUSE:-<default>}"
echo "  Role:      ${ROLE:-<default>}"
echo "  Config:    $CONFIG"
echo "  Proxy:     http://127.0.0.1:$PORT"
echo
echo "  Next: ./run.sh"
echo
