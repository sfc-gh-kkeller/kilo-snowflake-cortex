#!/usr/bin/env bash
# Install ksc (Kilo x Snowflake Cortex) on macOS or Linux.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/sfc-gh-kkeller/kilo-snowflake-cortex/main/install.sh | bash
#
set -euo pipefail

REPO="https://github.com/sfc-gh-kkeller/kilo-snowflake-cortex.git"
INSTALL_DIR="${KSC_INSTALL_DIR:-$HOME/.local/share/kilo-snowflake-cortex}"
BIN_DIR="${KSC_BIN_DIR:-$HOME/.local/bin}"

GREEN=$'\033[32m' YELLOW=$'\033[33m' RED=$'\033[31m' BLUE=$'\033[34m' BOLD=$'\033[1m' RESET=$'\033[0m'
ok()   { printf '%s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
info() { printf '%s→%s %s\n' "$BLUE" "$RESET" "$*"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$RESET" "$*"; }
fail() { printf '%s✗%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

printf '\n%s%s Kilo x Snowflake Cortex — Installer%s\n\n' "$BOLD" "$BLUE" "$RESET"

# --- Check Python 3.9+ ---
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then
    ver="$("$c" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
    major="${ver%%.*}"
    minor="${ver##*.}"
    if [ "${major:-0}" -ge 3 ] && [ "${minor:-0}" -ge 9 ]; then
      PY="$c"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  fail "Python 3.9+ is required but not found."
  echo "  Install: https://www.python.org/downloads/"
  exit 1
fi
ok "Python: $PY ($("$PY" --version 2>&1))"

# --- Check / install Kilo ---
KILO=""
if command -v kilo >/dev/null 2>&1; then
  KILO="$(command -v kilo)"
elif [ -x "$HOME/.kilo/bin/kilo" ]; then
  KILO="$HOME/.kilo/bin/kilo"
fi

if [ -n "$KILO" ]; then
  ok "Kilo: $KILO"
else
  info "Kilo not found — installing..."
  if command -v npm >/dev/null 2>&1; then
    npm install -g @kilocode/cli 2>&1 | tail -3
  elif command -v curl >/dev/null 2>&1; then
    curl -fsSL https://kilo.ai/cli/install | bash
  else
    warn "Could not install Kilo automatically."
    echo "  Install manually: npm install -g @kilocode/cli"
    echo "  Then re-run this installer."
  fi

  if command -v kilo >/dev/null 2>&1; then
    ok "Kilo installed: $(command -v kilo)"
  elif [ -x "$HOME/.kilo/bin/kilo" ]; then
    ok "Kilo installed: $HOME/.kilo/bin/kilo"
  else
    warn "Kilo installation may need a shell restart to take effect."
  fi
fi

# --- Download / update ksc ---
if [ -d "$INSTALL_DIR/.git" ]; then
  info "updating ksc..."
  git -C "$INSTALL_DIR" pull --quiet 2>/dev/null || true
  ok "updated $INSTALL_DIR"
else
  info "downloading ksc..."
  if command -v git >/dev/null 2>&1; then
    git clone --quiet "$REPO" "$INSTALL_DIR"
  else
    # Fallback: download tarball
    mkdir -p "$INSTALL_DIR"
    curl -fsSL "https://github.com/sfc-gh-kkeller/kilo-snowflake-cortex/archive/refs/heads/main.tar.gz" \
      | tar xz --strip-components=1 -C "$INSTALL_DIR"
  fi
  ok "installed to $INSTALL_DIR"
fi

# --- Symlink ksc onto PATH ---
mkdir -p "$BIN_DIR"
KSC="$INSTALL_DIR/proxy/ksc.py"
chmod +x "$KSC"
ln -sf "$KSC" "$BIN_DIR/ksc"
ok "ksc -> $BIN_DIR/ksc"

# Check if BIN_DIR is on PATH
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
  warn "$BIN_DIR is not on your PATH"
  echo ""
  echo "  Add to your shell profile (~/.bashrc, ~/.zshrc, etc.):"
  echo "    export PATH=\"$BIN_DIR:\$PATH\""
  echo ""
fi

# --- Write model catalog into kilo.json ---
info "configuring Snowflake Cortex models in kilo.json..."
"$PY" "$KSC" setup

# --- Done ---
echo ""
printf '%s\n' "════════════════════════════════════════════════════"
ok "Installation complete"
printf '%s\n' "════════════════════════════════════════════════════"
echo ""
echo "  Next steps:"
echo "    ksc add myaccount     # add your Snowflake auth profile"
echo "    ksc myaccount         # start proxy + launch Kilo"
echo ""
