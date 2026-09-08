#!/usr/bin/env bash
# Install ksc onto PATH by symlinking proxy/ksc.py.
#
# Usage:
#   ./install-ksc.sh                 # symlink to ~/.local/bin/ksc
#   ./install-ksc.sh /usr/local/bin  # symlink to /usr/local/bin/ksc
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KSC="$SCRIPT_DIR/proxy/ksc.py"

TARGET_DIR="${1:-$HOME/.local/bin}"
TARGET="$TARGET_DIR/ksc"

if [[ ! -f "$KSC" ]]; then
  echo "error: ksc.py not found at $KSC" >&2
  exit 1
fi

mkdir -p "$TARGET_DIR"
ln -sf "$KSC" "$TARGET"
echo "ok  $TARGET -> $KSC"

if ! echo "$PATH" | tr ':' '\n' | grep -qx "$TARGET_DIR"; then
  echo ""
  echo "Add to your shell profile:"
  echo "  export PATH=\"$TARGET_DIR:\$PATH\""
fi
