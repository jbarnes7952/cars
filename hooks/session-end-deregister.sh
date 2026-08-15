#!/usr/bin/env bash
# SessionEnd hook: deregister this session from the peer ledger.
# Exit 0 unconditionally.

LEDGER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
timeout 5 python3 "$LEDGER_DIR/ledger_mcp.py" hook-deregister >/dev/null 2>&1 || true
exit 0
