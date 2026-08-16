# CARS — Claude Agent Registration Server (Peer Session Ledger)
# (this is an experimental project, use with caution)

A discovery directory ("DNS") for dynamic Claude Code peer sessions, served
over MCP. Sessions register their identity, role, and capabilities; other
sessions query the ledger to decide **who** to message, then use Claude Code's
native cross-session `SendMessage` to do the messaging.

**The ledger never transports messages.** It is a directory, not a channel.
If it's down, sessions fall back to native `ListAgents` — nothing in Claude
Code's operation depends on it.

- Zero dependencies: Python 3.8+ stdlib only (`sqlite3`, `json`).
- Storage: SQLite (WAL) at `~/.claude-ledger/ledger.db` (override with
  `CLAUDE_LEDGER_DB`). Schema in `schema.sql`, applied idempotently on startup.
- Localhost only, stdio transport, no auth, no network.

## Layout

```
ledger_mcp.py              MCP server + hook helpers + CLI (single file)
schema.sql                 agents (live state) + events (append-only audit log)
skills/
  register/SKILL.md            /register — in-session manual upsert (current mode)
  deregister/SKILL.md          /deregister — in-session manual removal
hooks/
  roster-inject.sh             UserPromptSubmit → periodic peer-roster context push (wired)
  tmux-relabel.sh              PostToolUse → tmux window/pane label (wired)
  heartbeat.sh                 UserPromptSubmit/PostToolUse/Stop → bump last_seen (wired)
  session-start-register.sh    SessionStart → register (auto mode, unwired)
  session-end-deregister.sh    SessionEnd → deregister (auto mode, unwired)
launch-session.sh          auto-mode launcher: mints name, exports it, runs claude --name
claude-md-snippet.md       protocol block to paste into CLAUDE.md
examples/settings.json     hook wiring example (auto mode)
examples/mcp.json          MCP registration example
```

## Operating modes

**Manual (current).** Registration and updates are triggered *in session* by
the user: `/register [role / focus]` upserts this session's entry —
resolving the session name from `$CLAUDE_LEDGER_NAME`, the conversation, or
by asking the user, never by deriving one — and `/deregister` removes it.
The skills live in `skills/` and are symlinked into `~/.claude/skills/`.
Missed deregisters are harmless: stale after 10 min, evicted after 24 h.
The PostToolUse tmux-relabel hook stays wired since it triggers off the
in-session ledger tool calls.

**Roster push (wired).** So sessions *know about* their peers without having
to think to call `find_agents`, a `UserPromptSubmit` hook
(`hooks/roster-inject.sh`) periodically injects a compact live roster into
context — one line per fresh agent (`name [project]: role — status; ask
about: ...`) plus guidance to coordinate via `SendMessage` before touching a
peer's project. Cadence and size are configured via env vars in the `env`
block of `~/.claude/settings.json`:

| var | default | meaning |
|---|---|---|
| `LEDGER_ROSTER_EVERY` | `5` | inject on the first event of a session, then every Nth prompt; `1` = every prompt, `0` = off |
| `LEDGER_ROSTER_TOOLS_EVERY` | `25` | also inject every Nth *tool call* (hook wired to PostToolUse) — reaches autonomous sessions that rarely see user prompts; `0` = off |
| `LEDGER_ROSTER_MAX` | `15` | max agents per injection (freshest first) |

The two cadences share one counter file; a firing on either channel resets
both, so a chatty session never gets double-injected.

Prompts are counted per session (state files under
`~/.claude-ledger/roster-state/`, pruned after 7 days). Off-cycle prompts and
empty rosters emit nothing — zero context cost. Stale agents and the session's
own entry are excluded (matched by `session_id`, then unique `cwd`, then
resolved name). Preview the current roster text with
`python3 ledger_mcp.py roster`.

**Register nudge (self-populating directory).** If the receiving session has
no ledger row, the roster injection appends an instruction to register (with
a trigger-phrased `query_me_when`), so unregistered sessions recruit
themselves into the directory on the roster cadence. Manual registrations
can't know their own `session_id`; the first heartbeat backfills it into the
row (recorded as a `session_id_backfilled` heartbeat event), after which
self-matching is exact.

**Agents as tools (wired, on by default).** The MCP server also renders each
fresh registered agent as a tool — `peer_<name>`, description = its routing
card (`role — status. Ask when: <query_me_when>`) — so peers appear in the
tool list exactly like MCP capabilities. Calling a peer tool returns its
contact card; actual messaging is always native `SendMessage`. The server
advertises `listChanged` and emits `notifications/tools/list_changed` when the
registry shifts: immediately when the session's own register/update/deregister
changes it, and via a background poll (default every 20 s) when *another*
session's registration changes the shared DB. Whether a given Claude Code
build re-fetches tools mid-session on that notification is client behavior —
verify empirically; the tool list is always correct at session start
regardless. Because `query_me_when` becomes a tool description, write it as a
trigger condition ("message me before touching schema.sql"), not a topic.

| var | default | meaning |
|---|---|---|
| `LEDGER_AGENT_TOOLS` | `1` | expose peers as tools; `0` = static six tools only |
| `LEDGER_TOOLS_POLL` | `20` | seconds between registry polls for `list_changed`; `0` = notify only on own mutations |

**Automatic registration (future, currently unwired).** The SessionStart/SessionEnd hooks
and `launch-session.sh` implement lifecycle-driven auto registration. They
work (tested) but are deliberately not wired into settings — hooks can't see
the session's messaging name (see "Session name resolution" below), so auto
mode depends on the launch wrapper or a first-turn self-correction protocol.
How to trigger registration automatically and correctly is an open design
question.

## Install as a plugin (recommended)

The repo is a Claude Code plugin: one install wires the MCP server, all
current-mode hooks (roster/nudge, heartbeat, tmux relabel), and the skills,
with no path editing.

```bash
# from a local checkout
claude plugin install /path/to/cars

# or via the bundled marketplace (.claude-plugin/marketplace.json);
# repo is currently private, so the machine needs GitHub auth:
/plugin marketplace add jbarnes7952/cars
/plugin install cars@cars
```

Plugin installs differ from manual installs in two visible ways:

- **Tool names** become `mcp__plugin_cars_ledger__*` instead of
  `mcp__ledger__*`. The plugin manifest sets `LEDGER_TOOL_PREFIX` so the
  roster/nudge text and hook matchers use the right names automatically.
- **Skills** surface as `/cars:register` and `/cars:deregister`.

Do not combine plugin install with the manual hook/MCP wiring below — you'd
get duplicate injections and heartbeats. Pick one mode; remove the manual
`ledger` entries from `~/.claude/settings.json` and `~/.claude.json` when
switching to the plugin.

## Manual install

1. Clone anywhere, e.g. `~/tools/cars`. Make scripts executable:

   ```bash
   chmod +x hooks/*.sh launch-session.sh ledger_mcp.py
   ```

2. Register the MCP server (user scope so every session gets it):

   ```bash
   claude mcp add --scope user ledger -- python3 ~/tools/cars/ledger_mcp.py serve
   ```

   or copy `examples/mcp.json` to a project root as `.mcp.json` (fix the path).

3. Wire the hooks: merge `examples/settings.json` into `~/.claude/settings.json`
   (fix the paths).

4. Paste `claude-md-snippet.md` into your user or project `CLAUDE.md`.

## Launching sessions

Recommended:

```bash
~/tools/cars/launch-session.sh                      # auto name: <project>-<rand>
~/tools/cars/launch-session.sh schema-owner        # explicit name
~/tools/cars/launch-session.sh api-worker -- -p "triage the queue"  # extra claude args
```

The wrapper exports `CLAUDE_LEDGER_NAME` and runs `claude --name <name>`, so
the ledger entry is guaranteed to match the address peers see in `ListAgents`.

Direct `claude` launches still work: the SessionStart hook registers a
**derived** name (`<cwd-basename>-<session_id[:4]>`) flagged with
`name_source: "derived"` in the register event. A derived name may not match
the real messaging name, so the CLAUDE.md snippet instructs Claude to correct
its registration on the first turn.

### Session name resolution (hooks)

Priority order:

1. `$CLAUDE_LEDGER_NAME` (set by `launch-session.sh`) — authoritative.
2. `session_title` from hook stdin — **per current Claude Code docs this field
   is not delivered to SessionStart hooks** (stdin carries `session_id`, `cwd`,
   `hook_event_name`, but no name field, and no env var carries the `--name`
   value either). The code still honors it if a future version adds it.
3. Derived `basename(cwd)-<first 4 of session_id>`, flagged as above.

## Staleness & eviction

- `last_seen` > 10 min ⇒ entry flagged `"stale": true` (still returned when
  `include_stale: true`).
- `last_seen` > 24 h ⇒ evicted (row deleted, `evicted` event written).
  Eviction runs lazily on every tool call — no background daemon.
- Any tool call carrying a `session_name` bumps that session's `last_seen`.

To keep active sessions fresh, wire `hooks/heartbeat.sh` into the
UserPromptSubmit, PostToolUse, and Stop events (async, timeout 10). It calls
`ledger_mcp.py hook-heartbeat`, which matches this session's row by
`session_id`, then unique `cwd`, then the resolved name — so manually
`/register`-ed sessions with custom names are covered too. Throttled to one
DB write per `LEDGER_HEARTBEAT_EVERY` seconds (default 60) per CLI process.
A session idle longer than the stale threshold still goes stale — staleness
is informational, not fatal.

## MCP tools

| tool | purpose |
|---|---|
| `register` | upsert full record (re-registering a name replaces the row) |
| `update_registration` | partial update of `role`/`capabilities`/`query_me_when`/`status`/`project` |
| `heartbeat` | bump `last_seen` only |
| `find_agents` | free-text match across descriptive fields → returns `session_name` addresses |
| `list_agents_detailed` | all records |
| `deregister` | delete row (idempotent) |

All return JSON. Every mutation writes an append-only `events` row in the same
transaction (`register`/`update`/`heartbeat`/`deregister`/`evicted`); heartbeat
events are sampled to at most one per session per 5 minutes.

## CLI

```bash
python3 ledger_mcp.py list           # pretty-print live agents
python3 ledger_mcp.py list --stale   # include stale entries
sqlite3 ~/.claude-ledger/ledger.db 'SELECT * FROM events ORDER BY id'  # full history
```

## tmux relabeling

When a session calls `register` or `update_registration` with a `project`, the
PostToolUse hook renames the tmux window (and disables automatic-rename).
Set `LEDGER_TMUX_MODE=pane` to set the pane title instead (multi-pane layouts).
No-op outside tmux.

## Fallback behavior

The ledger is best-effort by design:

- **Ledger down at session start:** the SessionStart hook exits 0 regardless
  (5 s timeout, all errors swallowed). The session starts normally.
- **Ledger down mid-session:** `find_agents` fails ⇒ Claude falls back to
  native `ListAgents` and messages peers directly.
- **Stale entry / failed delivery:** same fallback — `ListAgents` is ground
  truth for reachability; the ledger only adds routing metadata.
- **Killed sessions (no SessionEnd):** entry goes stale after 10 min, is
  evicted after 24 h with an `evicted` event.

## Design constraints (do not violate)

1. Directory only, never transport — no queuing, routing, or delivery.
2. Session name is a stable opaque address; meaning lives in the ledger.
   Never `/rename` a registered session.
3. Boring and always up: localhost, SQLite, no deps, no auth.
4. `events` is a one-way seam: future consumers read it; the ledger reads
   nothing and calls nothing.

## Tests

```bash
python3 test_ledger.py
```

Covers: schema idempotency, register/upsert, partial update, heartbeat
sampling, find/list with staleness flags, lazy eviction, idempotent
deregister, hook name resolution, and the MCP stdio handshake end-to-end.
