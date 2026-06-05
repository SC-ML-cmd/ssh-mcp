#!/usr/bin/env bash
set -euo pipefail

PREFIX="$HOME/ssh-mcp-lab"
AUTO_START=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix)
      PREFIX="$2"
      shift 2
      ;;
    --auto-start)
      AUTO_START=1
      shift
      ;;
    -h|--help)
      echo "Usage: bash install.sh [--prefix PATH] [--auto-start]"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX/#\~/$HOME}"

command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required on the target VM." >&2
  exit 1
}

mkdir -p "$PREFIX/bin" "$PREFIX/runtime"
install -m 0755 "$SCRIPT_DIR/cmsm.py" "$PREFIX/cmsm.py"
install -m 0755 "$SCRIPT_DIR/bin/kubectl" "$PREFIX/bin/kubectl"

if [ "$AUTO_START" -eq 1 ]; then
  BASHRC="$HOME/.bashrc"
  touch "$BASHRC"

  START_MARK="# >>> ssh-mcp-lab cmsm simulator >>>"
  END_MARK="# <<< ssh-mcp-lab cmsm simulator <<<"
  TMP_FILE="$(mktemp)"

  awk -v start="$START_MARK" -v end="$END_MARK" '
    $0 == start { skipping = 1; next }
    $0 == end { skipping = 0; next }
    skipping != 1 { print }
  ' "$BASHRC" > "$TMP_FILE"

  cat >> "$TMP_FILE" <<EOF
$START_MARK
if [ -n "\${SSH_CONNECTION:-}" ] && [ -z "\${SSH_MCP_LAB_INSIDE:-}" ] && [ -t 0 ]; then
  export SSH_MCP_LAB_INSIDE=1
  export SSH_MCP_LAB_HOME="$PREFIX"
  exec /usr/bin/env python3 "$PREFIX/cmsm.py"
fi
$END_MARK
EOF

  cat "$TMP_FILE" > "$BASHRC"
  rm -f "$TMP_FILE"
fi

echo "Installed CMSM simulator to: $PREFIX"
echo "Manual start: python3 \"$PREFIX/cmsm.py\""
if [ "$AUTO_START" -eq 1 ]; then
  echo "Auto-start enabled for interactive SSH logins in: $HOME/.bashrc"
fi

