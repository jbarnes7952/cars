#!/usr/bin/env bash
# PostToolUse hook (matcher: mcp__ledger__register|mcp__ledger__update_registration).
# Relabels the tmux window/pane from the `project` field of the tool call.
# LEDGER_TMUX_MODE=window|pane (default window). Exit 0 unconditionally.

[ -n "$TMUX" ] || exit 0
[ -n "$TMUX_PANE" ] || exit 0

PROJECT=$(python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    print(data.get("tool_input", {}).get("project") or "")
except Exception:
    pass
' 2>/dev/null)

[ -n "$PROJECT" ] || exit 0

if [ "${LEDGER_TMUX_MODE:-window}" = "pane" ]; then
    tmux select-pane -t "$TMUX_PANE" -T "$PROJECT" 2>/dev/null || true
else
    tmux rename-window -t "$TMUX_PANE" "$PROJECT" 2>/dev/null || true
    tmux set-option -w -t "$TMUX_PANE" automatic-rename off 2>/dev/null || true
fi
exit 0
