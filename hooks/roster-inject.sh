#!/usr/bin/env bash
# UserPromptSubmit hook: every LEDGER_ROSTER_EVERY prompts (default 5; 1=every
# prompt, 0=off), inject the live peer roster into context. stdout carries the
# hook JSON payload — do not redirect it. Exit 0 unconditionally.

LEDGER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
timeout 5 python3 "$LEDGER_DIR/ledger_mcp.py" hook-roster 2>/dev/null || true
exit 0
