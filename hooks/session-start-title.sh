#!/usr/bin/env bash
# SessionStart hook: give an untitled session the title <project>-<sid[:4]>
# (LEDGER_SESSION_TITLE) and label its tmux window/pane with it once
# (LEDGER_TMUX_RELABEL). stdout carries the hook JSON payload — do not
# redirect it. Never registers. Exit 0 unconditionally.
LEDGER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
timeout 5 python3 "$LEDGER_DIR/ledger_mcp.py" hook-session-start 2>/dev/null || true
exit 0
