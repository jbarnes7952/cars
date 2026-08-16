# Peer Session Ledger — paste this block into your user or project CLAUDE.md

## Peer session ledger

This machine runs a session directory (the `ledger` MCP server). It tells you
**who** to message; native `SendMessage` does the messaging.

1. **Register early.** Once this session's purpose is clear (usually the first
   real prompt), call `mcp__ledger__register` with: your SendMessage name (ask
   the user if you don't know it — never guess or derive one), `role`,
   `status`, `project`, and `query_me_when` written as a **trigger condition**
   ("message me when/before ...", naming specific files or operations) — it is
   rendered verbatim as your tool description for peers.
2. **Keep it current.** Call `mcp__ledger__update_registration` when focus
   meaningfully shifts; the tmux label follows `project`. Never `/rename` a
   registered session — the name is its address.
3. **Route before messaging.** Check the injected roster, `peer_*` tools, or
   `mcp__ledger__find_agents` before messaging a peer about a topic; address
   `SendMessage` with the returned `session_name`. If the ledger is down or an
   entry is stale, fall back to native `ListAgents`.
4. **Directory only.** Never use the ledger to leave notes or deliver
   messages — no "note in the ledger" patterns, ever.
