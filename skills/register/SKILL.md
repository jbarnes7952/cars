---
name: register
description: Register or update this session's entry in the peer ledger directory (role, capabilities, status, project). Directory only — never a message channel.
argument-hint: "[role / focus, e.g. 'schema-owner: postgres migrations for webapp']"
disable-model-invocation: true
---

Register or update this session in the `ledger` MCP directory so peer sessions
can find it and address it with `SendMessage`.

## 1. Resolve this session's messaging name

The name must be the exact address peers see in `ListAgents` — it is the
primary key of the ledger entry.

Try in order:
1. `$CLAUDE_LEDGER_NAME` (check via Bash: `echo "$CLAUDE_LEDGER_NAME"`).
2. A session name already established in this conversation (e.g. the user told
   you, or a prior `/register` in this session used one).
3. Otherwise **ask the user** for the session's name (the `--name` it was
   launched with, or what they want it called). Do not guess or derive one —
   a wrong name breaks peer routing.

## 2. Upsert the entry

Use the `ledger` MCP server's tools (named `mcp__ledger__*` when registered
manually, `mcp__plugin_cars_ledger__*` when installed as the cars plugin —
use whichever form appears in your tool list).

Check `list_agents_detailed` (`include_stale: true`) for an existing row
under that name.

- **No row** → `register` with: `session_name`, `cwd` (current working
  directory), `project` (basename of the git repo root, else of cwd),
  and `role`, `capabilities`, `query_me_when`, `status`.
- **Row exists** → `update_registration` with only the fields that changed.

Fill the descriptive fields from `$ARGUMENTS` first, then from conversation
context (what is this session actually working on?). If the session's purpose
is still unclear, use `role: "unassigned"` and a short honest `status` rather
than inventing detail.

`query_me_when` is rendered as a *tool description* in every peer session, so
write it as a **trigger condition**, not a topic label — phrase it "message me
when/before ...", naming the specific files, operations, or decisions that
should make a peer stop and coordinate. Good: "message me before modifying
schema.sql or anything touching the ledger.db format — I own the schema".
Bad: "postgres questions". A vague topic gives peers no tripwire.

If an obviously stale or derived leftover entry for this same session exists
under a different name, ask the user before `mcp__ledger__deregister`-ing it.

## 3. Report

One or two sentences: the stored `session_name`, role, project, and whether it
was a fresh registration or an update. The tmux window relabels automatically
via a PostToolUse hook when `project` is set — do not rename it yourself, and
never use `/rename` on a registered session.
