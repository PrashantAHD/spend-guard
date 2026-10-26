#!/usr/bin/env bash
# Installs the Spend-Guard skill for this user (~/.copilot/skills/spend-guard).
# Then, in any project chat, type /spend-guard (or ask to "set up spend-guard").
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || { echo "Python 3.10+ is required." >&2; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || { echo "Python 3.10+ is required." >&2; exit 1; }
DEST="${COPILOT_HOME:-$HOME/.copilot}/skills/spend-guard"
mkdir -p "$DEST"
cp -R "$HERE/skill/spend-guard/." "$DEST/"
echo "Spend-Guard installed: $DEST"
echo "Open a project, start a new chat and type /spend-guard."
