#!/usr/bin/env bash
# SessionStart hook: register this session in the peer ledger.
# Must exit 0 fast, and exit 0 even if the ledger is unreachable —
# registration failure is never allowed to disturb session start.

LEDGER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
timeout 5 python3 "$LEDGER_DIR/ledger_mcp.py" hook-register >/dev/null 2>&1 || true
exit 0
