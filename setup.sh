#!/usr/bin/env bash
# KiCad MCP Server — setup script
# Installs dependencies and registers the MCP server with Claude Code.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="$SCRIPT_DIR/.claude/settings.json"
SERVER="$SCRIPT_DIR/kicad_mcp_server.py"

echo "==> Installing Python dependencies..."
pip install mcp

echo "==> Writing Claude Code MCP config: $SETTINGS"
mkdir -p "$SCRIPT_DIR/.claude"
PYTHON="$(which python)"

cat > "$SETTINGS" <<EOF
{
  "mcpServers": {
    "kicad": {
      "command": "$PYTHON",
      "args": ["$SERVER"]
    }
  }
}
EOF

echo ""
echo "Done."
echo ""
echo "Open Claude Code in this directory:"
echo "  cd $SCRIPT_DIR && claude"
echo ""
echo "All KiCad tools will be available automatically."
echo "Just describe what you want to design and Claude will use them."
