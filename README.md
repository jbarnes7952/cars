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
test_ledger.py             test suite (stdlib unittest)
SPEC.md                    the contract: schema, tools, hooks, invariants
CHANGELOG.md               one entry per tagged release
CLAUDE.md                  notes for Claude sessions working on this repo
.claude-plugin/
  plugin.json                  plugin manifest (MCP server, version)
  marketplace.json             single-plugin marketplace for `plugin install cars@cars`
skills/
  register/SKILL.md            /register — manual register/update fallback
  deregister/SKILL.md          /deregister — manual removal
hooks/
  hooks.json                   plugin hook wiring (everything marked "wired" below)
  session-start-title.sh       SessionStart → title an untitled session, label tmux once (wired)
  roster-inject.sh <channel>   UserPromptSubmit/PostToolUse → periodic peer roster + register nudge (wired)
                               <channel> is `prompt` or `tool`; the cadence is gated in shell
                               so python3 only starts on a firing tick
  peer-message.sh              UserPromptSubmit → record that a peer message arrived (wired)
  heartbeat.sh                 UserPromptSubmit/PostToolUse/Stop → bump last_seen (wired)
  session-end-deregister.sh    SessionEnd → deregister own row (wired)
  session-start-register.sh    SessionStart → register from hooks (unwired, see below)
launch-session.sh          launcher: mints a name, exports it, runs claude --name
claude-md-snippet.md       protocol block to paste into CLAUDE.md
examples/settings.json     hook + env wiring for a manual install
examples/mcp.json          MCP registration example for a manual install
```

## Operating modes

**Model-driven registration (current).** Hooks never register a session.
Instead the roster hook nudges an unregistered session to register itself
(see *Register nudge* below), and the model calls the `register` tool once
its purpose is clear, asking an attended user first. `/register [role /
focus]` is the manual fallback and `/deregister` removes the entry. Both
skills ship with the plugin as `/cars:register` and `/cars:deregister`; a
manual install symlinks `skills/*` into `~/.claude/skills/`. The skill
resolves the name from the conversation or `$CLAUDE_LEDGER_NAME`, and
otherwise omits it so the server uses the transport address; it never
invents one. Missed deregisters are harmless: a row whose session is gone
is evicted on the next tool call (see [Staleness & eviction](#staleness--eviction)).

**Roster push (wired).** So sessions *know about* their peers without having
to think to call `find_agents`, `hooks/roster-inject.sh` runs on
UserPromptSubmit (`prompt` channel) and PostToolUse (`tool` channel) and
periodically injects a compact roster into context — one line per fresh
agent (`- name [project]: role — status; ask about: ...`, trimmed to
`LEDGER_ROSTER_LINE_MAX`) plus guidance to coordinate via `SendMessage` before touching a
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
own entry are excluded (matched the way heartbeats match, see
[Staleness & eviction](#staleness--eviction)). The roster is deliberately
terser than `list_agents_detailed`: it skips rows that have gone quiet even
when their session is still live. Preview the current roster text with
`python3 ledger_mcp.py roster`.

**Register nudge (self-populating directory).** If the receiving session has
no ledger row, the roster injection appends an instruction to register (with
a trigger-phrased `query_me_when`), so unregistered sessions recruit
themselves into the directory on the roster cadence. Manual registrations
can't know their own `session_id`; the first heartbeat backfills it into the
row (recorded as a `session_id_backfilled` heartbeat event), after which
self-matching is exact.

**Self-addressing (zero-touch registration).** A session usually cannot see
its own display name (`claude --name` reaches the UI and ListAgents but is
never injected into model context; only `/rename` produces an in-context
notice). With the session-title hook wired (the default, see
[Session title and tmux label](#session-title-and-tmux-label)) the register
nudge names this session's title, so the model passes it as `session_name`
and the entry matches the address peers see. Failing that, `register` treats
`session_name` as optional: omitted, the server derives the session's
**transport address** — `uds:<cc-socks socket>`, found by walking ancestor
processes — which is directly routable via SendMessage (field-verified) and
cannot be wrong. The name is just an ID; the entry's
value is its descriptive fields. When the session later learns its real name
(rename notice, user tells it), it re-registers under it and any other rows
with the same `session_id` are superseded (deleted with a
`superseded_by` deregister event). `ledger_mcp.py self-address` prints the
derived address; `LEDGER_SOCK_DIR` overrides the socket directory (tests).

**Agents as tools (wired, on by default).** The MCP server also renders each
fresh registered agent as a tool — `ask_<agent>__<role-slug>`, description = its routing
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
| `LEDGER_TOOLS_POLL` | `20` | seconds between registry polls for `list_changed`; `0` = notify only on own mutations. The poll holds one read connection open, so the WAL side files are not created and deleted every cycle |
| `LEDGER_PEER_DESC_MAX` | `420` | peer tool description budget, spent priority-first: contact instruction (never cut) > `query_me_when` > role > status (dropped when tight); word-boundary trims |
| `LEDGER_ROSTER_LINE_MAX` | `200` | roster line budget, same priority order |

**Deferred tool loading (expect it).** Claude Code defers MCP tool schemas by
default (tool-search mode): only tool *names* enter context; descriptions are
fetched on demand via ToolSearch. CARS is designed to work under deferral:

- The server sends MCP `instructions` at connect — these stay in context even
  when every tool is deferred, and teach the vocabulary→tool-name mapping.
- Peer tool names embed the role (`ask_<agent>__<role-slug>`), so the
  always-visible name carries routing info; legacy `ask_<agent>` calls
  still resolve after a role change.
- The injected roster is the authoritative live peer list under deferral, and
  says so when deferral is detected (any `ENABLE_TOOL_SEARCH` other than
  `false`). On the first injection of a session it also asks the user once
  whether to enable eager loading — unless `LEDGER_ALWAYS_LOAD` is already
  `core` or `all`, in which case the question is answered and it stays quiet.

Knobs:

| var | default | meaning |
|---|---|---|
| `LEDGER_ALWAYS_LOAD` | `none` | `core` marks the six directory tools with `_meta: {"anthropic/alwaysLoad": true}` (exempt from deferral); `all` includes `ask_*` peer tools. **Field-verified 2026-08-22: Claude Code honors the per-tool `_meta` flag** — with `core` set, the directory tools arrive fully loaded in fresh sessions |
| `ENABLE_TOOL_SEARCH` (client-side, not CARS) | unset = defer | `auto`/`auto:N` = defer past a context threshold; `false` = eager-load everything |

Work-machine playbook (many corporate MCP servers): update Claude Code to
≥ v2.1.214 (older builds could wipe the deferred-tool index on a failed
`list_changed` refresh — CARS's dynamic peer tools churn that), rely on the
roster + `find_agents`, and opt into `LEDGER_ALWAYS_LOAD=core` if the
directory tools themselves fail to load on demand.

**Hook-driven registration (unwired).** `hooks/session-start-register.sh`
registers a session from SessionStart with no model involvement. It works and
is tested, but stays out of `hooks.json`: a hook can name the session
correctly (see [Session name resolution](#session-name-resolution-hooks)),
yet it cannot write a useful `role` or `query_me_when`, and an entry without
those is noise in every peer's roster. Wire it yourself if bare presence is
what you want; `launch-session.sh` pairs with it. The SessionEnd deregister
hook *is* wired, since removing a row needs no description.

## Install as a plugin (recommended)

The repo is a Claude Code plugin: one install wires the MCP server, every
hook marked "wired" in [Layout](#layout) (session title + tmux label,
roster/nudge, peer-message log, heartbeat, SessionEnd deregister), and the
skills, with no path editing. Install is two steps, add a marketplace and
install from it. The Ark Project catalog is the usual source and pins tagged
releases; the repo also carries its own one-plugin marketplace for a local
checkout:

```bash
# from the Ark Project catalog (cars and its sibling plugins, pinned releases)
claude plugin marketplace add jbarnes7952/ark-marketplace
claude plugin install cars@ark-project

# or from a local checkout (a directory marketplace: sessions then run that
# working tree directly, see "Which version is actually running")
claude plugin marketplace add /path/to/cars
claude plugin install cars@cars
```

The same commands work in-session as `/plugin marketplace add …` and
`/plugin install …`. Requires `python3` (3.8+) on `PATH`; tmux is optional.

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

4. Link the skills so `/register` and `/deregister` exist:

   ```bash
   ln -s ~/tools/cars/skills/register ~/tools/cars/skills/deregister ~/.claude/skills/
   ```

5. Paste `claude-md-snippet.md` into your user or project `CLAUDE.md`.

## Launching sessions

A plain `claude` launch is enough: the session-title hook names the session
and the register nudge keys its entry by that name. `launch-session.sh` is
for when you want to choose the name yourself:

```bash
~/tools/cars/launch-session.sh                      # auto name: <project>-<rand>
~/tools/cars/launch-session.sh schema-owner        # explicit name
~/tools/cars/launch-session.sh api-worker -- -p "triage the queue"  # extra claude args
```

The wrapper exports `CLAUDE_LEDGER_NAME` and runs `claude --name <name>`, so
the ledger entry is guaranteed to match the address peers see in `ListAgents`.

Direct `claude` launches still work: the session-title hook gives an
untitled session the title `<project>-<session_id[:4]>` (git-root basename,
else cwd basename), and the auto-register hook, where wired, registers that
same string, flagged `name_source: "derived"` in the register event. With
`LEDGER_SESSION_TITLE=0` there is no title to share, and both paths fall back
to the transport address, which is routable but unreadable; re-register
under a real name once one is known and the address row is superseded.

### Registering on behalf of a spawned session

A session cannot see its own display name, which is why automatic
registration from hooks stays unwired. A **spawner** can: it chose the name,
it has the child's pid and cwd, and once the child has written
`<CLAUDE_CONFIG_DIR>/sessions/<pid>.json` it can read the child's
`sessionId` and `messagingSocketPath` too. So the spawner registers the child
with the `register` CLI verb (see [CLI](#cli)), passing `session_name`
explicitly:

- Use the child's transport address, `uds:<messagingSocketPath>`, as
  `session_name`. It is routable by `SendMessage` from any profile, and it is
  the same key the child would derive if it later self-registers without a
  name, so the two upserts land on one row instead of colliding.
- Pass the human label through `claude -n <label>` and `CLAUDE_LEDGER_NAME`
  in the child's environment, so `ListAgents`, the tmux window, and the
  hook-side name fallback all agree. It is a label, not the ledger key. The
  session-title hook sees that title (or the env var) and leaves it alone.
- Pass `session_id` when known. Heartbeats and the SessionEnd deregister
  match on it first, and a later registration under a different name with
  the same `session_id` supersedes the spawner's row cleanly.
- Set `name_source` (for example `seat`) so the register event records who
  chose the name.

Omitting `session_name` from the CLI verb derives the address of the
process running it, which for a spawner is its own parent session, never the
child.

### Session name resolution (hooks)

Priority order:

1. `$CLAUDE_LEDGER_NAME` (set by `launch-session.sh`) — authoritative.
2. `session_title` from hook stdin. Claude Code sends it to SessionStart
   hooks when a custom title exists (`--name`, `/rename`, or a hook's
   `sessionTitle` output); field-verified on 2.1.296.
3. With `LEDGER_SESSION_TITLE` on (default): the title the session-title hook
   gives an untitled session, `<project>-<first 4 of session_id>`, so the row
   and the title agree by construction. Flagged `name_source: "derived"`.
4. The transport address (`uds:…`).
5. Legacy derived `basename(cwd)-<first 4 of session_id>`.

`register` with no `session_name` goes `CLAUDE_LEDGER_NAME`, then the
transport address; it cannot see the title, which is why the register nudge
supplies it. Both registration paths therefore key a session the same way —
previously the hook used the env var and the tool used the address, so one
session could appear under two names.

## Which version is actually running

A `serve` process holds whatever Python loaded when it started, for its whole
life — on this machine that is measured in days. `/reload-plugins` does not
restart it, and `claude plugin update` does not reach it. So **releasing a
version and running it are different facts**, and for eleven releases there
was no way to tell them apart from outside.

Ask the directory what answered you:

```bash
python3 ledger_mcp.py list --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["server_version"])'
```

`list_agents_detailed` and `find_agents` both report `server_version`. If it
is older than the version on disk, that session is running stale code, and
only restarting the session replaces it. A missing `server_version` means the
server predates 1.16.0.

Note for directory-marketplace installs (`source: directory` pointing at a
checkout, which is how a development install works): the server, the hooks
and the skills all run from that working tree, not from the plugin cache.
`claude plugin update` only rewrites the install record, so pulling the
checkout *is* the release, and a dirty tree or a checked-out branch goes live
to every session — hooks on their next firing, the server on its next start.
Develop in a separate worktree and try it with `claude --plugin-dir <worktree>`.

## Staleness & eviction

- `last_seen` > 10 min ⇒ entry flagged `"stale": true`.
- Every row has a `presence`: `live`, `gone`, or `unknown`. `"live": true` is
  kept as the derived boolean, meaning `presence == "live"`. A live row is
  listed even when stale — an agent idle by design is still somewhere you can
  send a message.
- Presence prefers positive evidence: a declared `pid` that is still running
  (checked with `os.kill(pid, 0)`, which works on Linux and macOS), else
  the transport socket (a `<digits>.sock` name resolves by pid; any other name
  is settled by connecting to it). A service peer that publishes only a pid is
  therefore judged correctly, which the old filename-based check could not do.
- Not yet supported on Windows: there `os.kill(pid, 0)` does not probe, it
  sends Ctrl-C (`CTRL_C_EVENT` is 0) to the target. The pid check needs a
  Windows path (`OpenProcess` + `GetExitCodeProcess` via `ctypes`) first.
- `unknown` means "could not look", not "dead", and only `unknown` rows fall
  back to the 24-hour activity rule.
- Eviction follows presence: `gone` evicts at once, `live` never does, and
  only `unknown` uses the 24-hour `last_seen` fallback. Eviction runs lazily on
  every tool call — no background daemon.
- A missing socket still evicts at once. A backfilled `address` can only ever
  protect a row from eviction, never cause one.
- Any tool call carrying a `session_name` bumps that session's `last_seen`.
- A process whose source file has changed on disk since it started does not
  evict at all. Long-lived `serve` processes keep their code for days, and
  eviction runs in whichever process makes a tool call, so one straggler could
  otherwise undo a release for the whole fleet.

Departure cleanup, in layers:

1. **SessionEnd → deregister (wired):** on clean exit the hook finds the
   session's own row by `session_id`, transport address or resolved name and
   removes it. It never deletes on a shared `cwd`, which could be a peer's
   row. A manual registration is still covered, because its first heartbeat
   backfills the `session_id`. No-op for unregistered sessions.
2. **Presence eviction:** any tool call judges every row's presence (above);
   a row whose session is `gone` is evicted immediately (`evicted` event,
   reason `transport-socket-gone`). This covers crashes and kill -9, which
   never fire SessionEnd.
3. **24 h idle eviction:** the backstop, applied only to rows whose presence
   is `unknown` (no pid or address to check, or another machine's socket).

To keep active sessions fresh, wire `hooks/heartbeat.sh` into the
UserPromptSubmit, PostToolUse, and Stop events (async, timeout 10). It calls
`ledger_mcp.py hook-heartbeat`, which matches this session's row by
`session_id`, then transport address, then unique `cwd`, then the resolved
name — so manually `/register`-ed sessions with custom names are covered
too. A row that carries another session's `session_id` is never matched. Throttled to one
DB write per `LEDGER_HEARTBEAT_EVERY` seconds (default 60) per session, keyed
on the `session_id` in the hook payload and held in `roster-state/<id>.hb`
alongside the roster counters, so the same 7-day prune reaps it.
A session idle longer than the stale threshold still goes stale — staleness
is informational, not fatal.

The key must not be `$PPID`: hooks are spawned under a fresh parent on every
invocation, so a PPID key is unique per call and the throttle never engages.
Before this was fixed the hook heartbeat on *every* prompt, tool call and
stop, and left a state file behind each time.

## MCP tools

| tool | purpose |
|---|---|
| `register` | upsert full record (re-registering a name replaces the row); `session_name` optional, defaults to the transport address |
| `update_registration` | partial update of `role`/`capabilities`/`query_me_when`/`status`/`project` |
| `heartbeat` | bump `last_seen` only; optional `via` marks a supervisor heartbeating for a child |
| `find_agents` | every word must appear in some descriptive field → returns `session_name` addresses |
| `list_agents_detailed` | all records |
| `deregister` | delete row (idempotent) |
| `ask_<agent>__<role-slug>` | one per fresh peer, when `LEDGER_AGENT_TOOLS` is on; returns that peer's contact card |

All return JSON. Every mutation writes an append-only `events` row in the same
transaction (`register`/`update`/`heartbeat`/`deregister`/`evicted`; the
peer-message hook adds `peer_message`); heartbeat
events are sampled to at most one per 5 minutes per (session, `via`) pair, so a
supervisor's proxied heartbeats and the session's own stay separately visible.
Unmarked `heartbeat` events predating `via` mean *self-reported or proxied,
unknown*: the marker starts the record rather than repairing it, and nothing
recovers the distinction for rows written before it.

A supervisor that heartbeats for a child which cannot run hooks itself (a
non-Claude process, say) should pass `via` naming itself:

```bash
python3 ledger_mcp.py heartbeat --json '{"session_name": "…", "via": "seat"}'
```

`via` lands in the event payload only — there is no `agents` column for it, and
freshness is unaffected: `last_seen` is bumped on every heartbeat, sampled or
not. Sampling thins the audit log, never the liveness signal.

## Message traffic (`peer_message`)

`hooks/peer-message.sh` records that a cross-session message arrived, so a
consumer can draw traffic between agents. It stores **endpoints and a timestamp
only** — `{"from": …, "to": …}`. Never the body, never its length, never a
subject.

**This hook reads prompt content.** Every other cars hook looks only at
`session_id`, `cwd`, `session_title` and the event name. This one pattern-matches
the start of each submitted prompt to spot the `<cross-session-message` wrapper.
Nothing from the prompt is stored beyond the sender's directory address, but the
hook does inspect what you type. Remove it from `hooks.json` if that is not a
trade you want; nothing else in cars depends on it.

The wrapper must be the first thing in the prompt, barring the one line the
harness writes above it, and the sender must resolve in the directory — by
`session_name` **or** `address` — or have a live socket on this machine. A
sender that is neither writes no row. See `SPEC.md` for why both guards are
load-bearing.

A message names its sender by transport address, so matching `session_name`
alone recorded nothing for sessions registered under a chosen name. The
`address` column closes that, and a resolved sender is reported as `from_name`
so a consumer needs no join.

**A mid-turn delivery is deferred, not dropped.** When a session is idle, an
inbound message becomes its next prompt and this hook sees it immediately. When
a session is busy, the message is queued and submitted as its own prompt once
the turn ends — so it is still recorded, just later. Traffic between busy
sessions is present in the table.

What is lost is arrival time, not the row: `ts` is when the delivery was
recorded, which for a deferred message is the next prompt boundary. Treat `ts`
as "when this was seen", not "when this arrived". A queued batch flushing at
once shows up as several rows milliseconds apart.

Read it back over the CLI:

```bash
python3 ledger_mcp.py events --event peer_message --since 2026-09-09T00:00:00Z --json
# {"events": [{"ts": "…", "from": "…", "to": "…"}], "count": 1, "truncated": false}

python3 ledger_mcp.py events --event peer_message --limit 3
# 2026-09-09T17:25:04.304Z  from=… to=…
# 2026-09-09T17:25:51.995Z  from=… to=…
# 2026-09-09T17:36:50.313Z  from=… to=…
# -- more rows match: this is the oldest window, not the newest. Continue with --since 2026-09-09T17:36:50.313Z

python3 ledger_mcp.py events --event peer_message --newest --limit 1 --json
# {"events": [{"ts": "…newest row…", "from": "…", "to": "…"}], "count": 1, "truncated": true}
```

Rows come oldest first, which is what a cursor wants: pass the last `ts` you
saw back as `--since` and drain in order with nothing skipped. `--since` takes
any ISO 8601 instant at any precision, with `Z` or a numeric offset, and
normalises it, so a synthesised bound like "one minute ago" selects the same
rows however you format it. It also means
`--limit` drops the *newest* rows, so `--limit 20` gives the twenty least
recent — check `truncated` to tell a partial answer from a quiet fleet. The
plain form says so itself, with a trailing `-- more rows match…` line naming the
cursor, so a capped read cannot be mistaken for a quiet one by someone reading
the terminal rather than the JSON.

**`--newest` sheds the other end**: the last `limit` rows, still oldest-to-newest
so the window's last row is the newest one and works as the cursor for every
poll after. It is what a cold reader wants — `--newest --limit 1` starts a
cursor at the present in one call, instead of walking the whole backlog to reach
it, which on an append-only table costs a call per page forever. It is a flag
rather than a count, so `--limit` keeps saying how many. `--newest` and `--since`
together exit 2: that combination would mean "the newest N after this cursor"
and silently drop everything in between, so it is refused rather than
interpreted.

Only `peer_message` is readable this way — `register` and `update` payloads
carry free-text status, so the verb takes an allowlist rather than any event
name. There is no MCP tool for it: that would spend every session's context on
a niche its consumer reaches over the CLI anyway.

## CLI

```bash
python3 ledger_mcp.py list           # pretty-print live agents
python3 ledger_mcp.py list --stale   # include stale entries
python3 ledger_mcp.py list --json    # the list_agents_detailed record as JSON
python3 ledger_mcp.py roster         # preview the roster text a session would get
python3 ledger_mcp.py self-address   # this session's uds: transport address
python3 ledger_mcp.py events --event peer_message --newest --limit 5   # see Message traffic
python3 ledger_mcp.py serve          # the MCP server on stdio (what the plugin runs)
sqlite3 ~/.claude-ledger/ledger.db 'SELECT * FROM events ORDER BY id'  # full history
```

The `hook-*` verbs are the hook entry points and read hook JSON on stdin;
`python3 ledger_mcp.py --help` prints the full usage.

The four mutating tools are also CLI verbs, for spawners and scripts that
are not themselves MCP clients. The tool arguments are one JSON object, read
from stdin or given as `--json '{...}'`; the resulting record is printed as
JSON; the ledger's rejection reason goes to stderr with exit 1:

```bash
echo '{"session_name": "uds:/run/user/1000/cc-socks/4242.sock", "session_id": "…",
       "pid": 4242, "cwd": "/repo", "project": "repo", "role": "worker",
       "name_source": "seat"}' | python3 ledger_mcp.py register
python3 ledger_mcp.py update --json '{"session_name": "…", "status": "busy"}'
python3 ledger_mcp.py heartbeat --json '{"session_name": "…"}'
python3 ledger_mcp.py deregister --json '{"session_name": "…"}'
```

## Session title and tmux label

At SessionStart, `hooks/session-start-title.sh` (`ledger_mcp.py
hook-session-start`) does two things, each behind its own switch:

| var | default | meaning |
|---|---|---|
| `LEDGER_SESSION_TITLE` | `1` | if the session has no custom title, set it to `<project>-<session_id[:4]>` (git-root basename, else cwd basename) via the hook's `sessionTitle` output. A title from `--name`, `/rename` or `CLAUDE_LEDGER_NAME` is kept. `0` = off |
| `LEDGER_TMUX_RELABEL` | `1` | label the tmux window (or pane) with the session title, **once**: only if nobody has named it yet, or the current name is one this hook set (tracked by the `@ledger_named` tmux user option). A hand-named window is never touched; `/clear` keeps the label in step. `0` = off |
| `LEDGER_TMUX_MODE` | `window` | `window` renames the window and turns `automatic-rename` off; `pane` sets the pane title instead (multi-pane layouts). "Unnamed" means `automatic-rename` still on, or a pane title that is empty or the hostname |

The title is also recorded under `roster-state/<session_id>.title`, which is
how the register nudge can tell the model the exact `session_name` to pass.
The hook never registers, never writes the ledger, is a no-op outside tmux,
bounds every tmux call by one second (and stops at the first hang), and
exits 0 unconditionally. `project` no longer drives the label; it is only a
searchable field. Resume with a title set is a no-op; `/clear` starts a new
session id, so it gets a fresh title and the label follows.

## Fallback behavior

The ledger is best-effort by design:

- **Ledger down at session start:** the SessionStart title hook exits 0
  regardless (5 s timeout, all errors swallowed). The session starts
  normally; at worst it is left untitled.
- **Ledger down mid-session:** `find_agents` fails ⇒ Claude falls back to
  native `ListAgents` and messages peers directly.
- **Stale entry / failed delivery:** same fallback — `ListAgents` is ground
  truth for reachability; the ledger only adds routing metadata.
- **Killed sessions (no SessionEnd):** the row is evicted on the next tool
  call once its socket or pid is gone, with an `evicted` event. Only a row
  whose presence cannot be checked waits out the 24 h fallback.

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

Stdlib `unittest`, no fixtures to install; every test uses a temporary
database and socket directory, so the suite never touches your live ledger.
Covers: schema idempotency, register/upsert and
supersession, partial update, heartbeat throttling and `via` sampling,
find/list with staleness and presence, eviction, the roster and register
nudge, peer tools and `list_changed`, the peer-message hook and `events`
reader, hook name resolution, the session-title hook (with a faked tmux),
the shell wrappers, the CLI verbs, and the MCP stdio handshake end to end.
Run it before every commit.
