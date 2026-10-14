# CARS — Peer Session Ledger

MCP directory server ("DNS") for Claude Code peer sessions. Read `SPEC.md`
for the contract and `README.md` for wiring; this file is for sessions
working on the code.

## Commands

```bash
python3 test_ledger.py         # full test suite — run before every commit
python3 ledger_mcp.py list     # inspect live registry (~/.claude-ledger/ledger.db)
python3 ledger_mcp.py roster   # preview the injected roster text
```

## Hard constraints (from SPEC.md — do not violate)

1. **Directory only, never transport.** No message queuing, routing, or
   delivery. Native `SendMessage` is the transport. Reject features that
   imply delivery.
2. **Zero dependencies.** Python stdlib only. No pip installs, ever.
3. **Ledger reads nothing, calls nothing.** The `events` table is append-only
   and is the integration surface for future consumers.
4. **Every `agents` mutation writes an `events` row in the same transaction.**
5. **Hooks always exit 0, fast.** A broken ledger must never disturb a
   session. Keep hook helpers under ~200ms warm.
6. **Tool/roster descriptions load into every session's context** — keep them
   short, and keep `query_me_when` guidance trigger-phrased
   ("message me when/before ..."), not topic-labeled.

## Coordination

Multiple Claude sessions co-develop this repo. Before modifying
`ledger_mcp.py`, `schema.sql`, `hooks/`, or `skills/`, check
`mcp__ledger__find_agents` for the `cars` / ledger-maintainer session and
message it via `SendMessage` first. Never use `/rename` on a registered
session — the name is its address.

## Conventions

- Single-file server: `ledger_mcp.py` holds the MCP loop, tool ops, hook
  helpers, and CLI. Keep it that way; no package structure.
- Schema changes go in `schema.sql` (idempotent `CREATE ... IF NOT EXISTS`)
  and must upgrade an existing `~/.claude-ledger/ledger.db` in place.
- New config = env var with a `LEDGER_` prefix, defaulted in code, documented
  in the module docstring, `README.md`, and `examples/settings.json`.
- Live wiring lives in `~/.claude/settings.json` (hooks, `env`) and
  `~/.claude.json` (`mcpServers.ledger`); `examples/` must mirror it with
  `/path/to/cars` placeholders.
- Update `README.md` in the same commit as any behavior change.

## Releases

This machine runs CARS as an installed plugin (`cars@cars`) from a
**directory marketplace pointing at this checkout** — every session's
`CLAUDE_PLUGIN_ROOT` is this checkout, and the cache copy that
`claude plugin update` writes is never executed. So this working tree **is**
the fleet: an uncommitted edit, or a checked-out branch, goes live to every
session at its next server start. Keep the checkout on `main` and clean, and
do session work in a worktree (see the global instructions).

A running `serve` process keeps the code it loaded; `/reload-plugins` does
not restart it. Compare `server_version` from `ledger_mcp.py list --json`
against the version on disk to tell what a session is actually running.

To release:

1. Tests pass (`python3 test_ledger.py`).
2. Bump `version` in `.claude-plugin/plugin.json` AND
   `.claude-plugin/marketplace.json`, and add a `CHANGELOG.md` entry, in
   the same commit as the change.
3. Merge to `main` and pull the checkout. Hooks and skills pick it up on
   their next firing, a `serve` process on its next start.
   `claude plugin update cars` only refreshes the install record.
4. Tag the squash commit on `main` and push the tag:
   `git tag -a vX.Y.Z <sha> -m "cars X.Y.Z: <summary>" && git push origin vX.Y.Z`.
   The tag matches the manifest version. Downstream catalogs (the
   `ark-project` marketplace) pin a tag plus sha, so an untagged release
   cannot be pinned cleanly. Tags start at v1.18.0; earlier releases are
   untagged.

To try work before merging, `claude --plugin-dir <worktree>` in a scratch
session runs that worktree instead of the checkout.
