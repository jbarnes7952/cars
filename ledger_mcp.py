#!/usr/bin/env python3
"""ledger-mcp: MCP directory server for Claude Code peer sessions.

Directory only, never transport. Sessions register identity/role/capabilities;
peers query to decide WHO to message, then use native SendMessage.

Zero dependencies: Python stdlib only (sqlite3, json). Localhost only.

Usage:
    ledger_mcp.py [serve]          MCP server on stdio (default)
    ledger_mcp.py hook-register    SessionStart hook helper (hook JSON on stdin)
    ledger_mcp.py hook-deregister  SessionEnd hook helper (hook JSON on stdin)
    ledger_mcp.py hook-roster      UserPromptSubmit hook: every Nth prompt,
                                   emit the peer roster as additionalContext
    ledger_mcp.py hook-heartbeat   activity hook helper: bump this session's
                                   last_seen (hook JSON on stdin)
    ledger_mcp.py roster           print the roster text (debug/preview)
    ledger_mcp.py list [--stale]   pretty-print registered agents

Config (env, set in ~/.claude/settings.json "env" block):
    LEDGER_ROSTER_EVERY  roster push every N prompts (default 5; 1=every, 0=off)
    LEDGER_ROSTER_MAX    max agents per roster push / per tool list (default 15)
    LEDGER_AGENT_TOOLS   expose each fresh agent as an MCP tool (default 1; 0=off)
    LEDGER_TOOLS_POLL    seconds between registry polls for tools/list_changed
                         notifications (default 20; 0=off)
"""

import json
import os
import re
import socket
import sqlite3
import sys
import threading
from datetime import datetime, timezone

DB_PATH = os.environ.get(
    "CLAUDE_LEDGER_DB", os.path.expanduser("~/.claude-ledger/ledger.db")
)
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

STALE_SECONDS = 10 * 60          # older than this => flagged stale
EVICT_SECONDS = 24 * 60 * 60     # older than this => evicted (lazily)
HEARTBEAT_SAMPLE_SECONDS = 5 * 60  # at most one heartbeat event per session per 5 min
ROSTER_EVERY_DEFAULT = 5           # inject roster every N prompts (env LEDGER_ROSTER_EVERY)
ROSTER_MAX_DEFAULT = 15            # max agents per roster (env LEDGER_ROSTER_MAX)
ROSTER_STATE_MAX_AGE = 7 * 24 * 3600  # prune per-session counters older than this

REGISTER_NUDGE = (
    "[ledger] This session is NOT registered in the peer directory, so peers"
    " cannot discover it or see when to route questions here. If this"
    " session's purpose is clear, register now: call mcp__ledger__register"
    " with session_name = this session's SendMessage name (ask the user if"
    " you don't know it), a role, a status, and query_me_when phrased as a"
    " trigger condition ('message me when/before ...'). Keep the entry"
    " current with mcp__ledger__update_registration as focus shifts."
)

AGENT_COLUMNS = [
    "session_name", "session_id", "pid", "cwd", "project", "role",
    "capabilities", "query_me_when", "status", "tmux_pane", "machine",
    "registered_at", "last_seen",
]
UPDATABLE_FIELDS = ["role", "capabilities", "query_me_when", "status", "project"]
SEARCH_FIELDS = ["role", "capabilities", "query_me_when", "status", "project"]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def parse_iso(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def age_seconds(ts):
    dt = parse_iso(ts)
    if dt is None:
        return float("inf")
    return (datetime.now(timezone.utc) - dt).total_seconds()


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    return conn


def write_event(conn, session_name, session_id, event, payload):
    conn.execute(
        "INSERT INTO events (ts, session_name, session_id, event, payload)"
        " VALUES (?, ?, ?, ?, ?)",
        (now_iso(), session_name, session_id or "", event, json.dumps(payload)),
    )


def evict_stale(conn):
    """Lazy eviction, run at the start of every tool call."""
    rows = conn.execute("SELECT * FROM agents").fetchall()
    for row in rows:
        if age_seconds(row["last_seen"]) > EVICT_SECONDS:
            conn.execute(
                "DELETE FROM agents WHERE session_name = ?", (row["session_name"],)
            )
            write_event(
                conn, row["session_name"], row["session_id"], "evicted",
                {"last_seen": row["last_seen"]},
            )
    conn.commit()


def row_to_record(row):
    rec = {k: row[k] for k in AGENT_COLUMNS}
    try:
        rec["capabilities"] = json.loads(rec["capabilities"] or "[]")
    except (ValueError, TypeError):
        rec["capabilities"] = []
    rec["stale"] = age_seconds(row["last_seen"]) > STALE_SECONDS
    return rec


def get_record(conn, session_name):
    row = conn.execute(
        "SELECT * FROM agents WHERE session_name = ?", (session_name,)
    ).fetchone()
    return row_to_record(row) if row else None


# ---------------------------------------------------------------- operations

class ToolError(Exception):
    pass


def op_register(conn, args):
    name = args["session_name"]
    caps = args.get("capabilities") or []
    if not isinstance(caps, list):
        caps = [str(caps)]
    now = now_iso()
    fields = {
        "session_name": name,
        "session_id": args.get("session_id") or "",
        "pid": args.get("pid"),
        "cwd": args.get("cwd") or "",
        "project": args.get("project") or "",
        "role": args.get("role") or "unassigned",
        "capabilities": json.dumps(caps),
        "query_me_when": args.get("query_me_when") or "",
        "status": args.get("status") or "",
        "tmux_pane": args.get("tmux_pane") or "",
        "machine": socket.gethostname(),
        "registered_at": now,
        "last_seen": now,
    }
    cols = ", ".join(fields)
    ph = ", ".join("?" for _ in fields)
    # Re-registration of an existing name replaces the row (session restart).
    conn.execute(
        f"INSERT OR REPLACE INTO agents ({cols}) VALUES ({ph})",
        tuple(fields.values()),
    )
    payload = dict(fields, capabilities=caps)
    if args.get("name_source"):
        payload["name_source"] = args["name_source"]
    write_event(conn, name, fields["session_id"], "register", payload)
    conn.commit()
    return get_record(conn, name)


def op_update_registration(conn, args):
    name = args["session_name"]
    row = conn.execute(
        "SELECT * FROM agents WHERE session_name = ?", (name,)
    ).fetchone()
    if row is None:
        raise ToolError(
            f"'{name}' is not registered; call register first."
        )
    changed = {}
    for field in UPDATABLE_FIELDS:
        if field in args and args[field] is not None:
            value = args[field]
            if field == "capabilities":
                if not isinstance(value, list):
                    value = [str(value)]
                changed[field] = json.dumps(value)
            else:
                changed[field] = value
    changed["last_seen"] = now_iso()
    sets = ", ".join(f"{k} = ?" for k in changed)
    conn.execute(
        f"UPDATE agents SET {sets} WHERE session_name = ?",
        tuple(changed.values()) + (name,),
    )
    payload = {
        k: (json.loads(v) if k == "capabilities" else v) for k, v in changed.items()
    }
    write_event(conn, name, row["session_id"], "update", payload)
    conn.commit()
    return get_record(conn, name)


def op_heartbeat(conn, args):
    name = args["session_name"]
    sid = args.get("session_id") or ""
    now = now_iso()
    cur = conn.execute(
        "UPDATE agents SET last_seen = ? WHERE session_name = ?", (now, name)
    )
    if cur.rowcount:
        row = conn.execute(
            "SELECT session_id FROM agents WHERE session_name = ?", (name,)
        ).fetchone()
        # Manual registrations don't know their session_id; backfill it from
        # the hook so self-matching (roster nudge, exclusion) becomes exact.
        backfilled = bool(sid and not row["session_id"])
        if backfilled:
            conn.execute(
                "UPDATE agents SET session_id = ? WHERE session_name = ?",
                (sid, name),
            )
        # Sample heartbeat events: at most one per session per 5 minutes
        # (a backfill always writes one, so the audit log records it).
        last = conn.execute(
            "SELECT MAX(ts) AS ts FROM events"
            " WHERE session_name = ? AND event = 'heartbeat'",
            (name,),
        ).fetchone()["ts"]
        if backfilled or last is None or age_seconds(last) > HEARTBEAT_SAMPLE_SECONDS:
            payload = {"last_seen": now}
            if backfilled:
                payload["session_id_backfilled"] = sid
            write_event(conn, name, sid or row["session_id"], "heartbeat", payload)
    conn.commit()
    return {"session_name": name, "registered": bool(cur.rowcount), "last_seen": now}


def op_find_agents(conn, args):
    query = args["query"]
    include_stale = bool(args.get("include_stale", False))
    like = f"%{query}%"
    where = " OR ".join(f"{f} LIKE ? COLLATE NOCASE" for f in SEARCH_FIELDS)
    rows = conn.execute(
        f"SELECT * FROM agents WHERE {where} ORDER BY last_seen DESC",
        tuple(like for _ in SEARCH_FIELDS),
    ).fetchall()
    records = [row_to_record(r) for r in rows]
    if not include_stale:
        records = [r for r in records if not r["stale"]]
    return {"agents": records, "count": len(records)}


def op_list_agents_detailed(conn, args):
    include_stale = bool(args.get("include_stale", False))
    rows = conn.execute("SELECT * FROM agents ORDER BY last_seen DESC").fetchall()
    records = [row_to_record(r) for r in rows]
    if not include_stale:
        records = [r for r in records if not r["stale"]]
    return {"agents": records, "count": len(records)}


def op_deregister(conn, args):
    name = args["session_name"]
    row = conn.execute(
        "SELECT session_id FROM agents WHERE session_name = ?", (name,)
    ).fetchone()
    # Idempotent: deregistering an unknown name succeeds silently.
    if row is not None:
        conn.execute("DELETE FROM agents WHERE session_name = ?", (name,))
        write_event(conn, name, row["session_id"], "deregister", {})
        conn.commit()
    return {"session_name": name, "deregistered": True}


# ---------------------------------------------------------------- MCP server

STR = {"type": "string"}
BOOL_FALSE = {"type": "boolean", "default": False}
CAPS = {"type": "array", "items": {"type": "string"}}

TOOLS = [
    {
        "name": "register",
        "description": "Register this session in the peer directory. Upserts by session_name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_name": {**STR, "description": "Claude Code messaging name (the address peers use with SendMessage)"},
                "session_id": STR, "cwd": STR, "role": STR,
                "capabilities": CAPS, "query_me_when": STR, "status": STR,
                "project": STR, "tmux_pane": STR,
                "pid": {"type": "integer"}, "name_source": STR,
            },
            "required": ["session_name"],
        },
        "handler": op_register,
    },
    {
        "name": "update_registration",
        "description": "Partially update this session's directory entry (role, capabilities, query_me_when, status, project).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_name": STR, "role": STR, "capabilities": CAPS,
                "query_me_when": STR, "status": STR, "project": STR,
            },
            "required": ["session_name"],
        },
        "handler": op_update_registration,
    },
    {
        "name": "heartbeat",
        "description": "Mark this session as still alive (bumps last_seen).",
        "inputSchema": {
            "type": "object",
            "properties": {"session_name": STR, "session_id": STR},
            "required": ["session_name"],
        },
        "handler": op_heartbeat,
    },
    {
        "name": "find_agents",
        "description": "Find peer sessions to message. Free-text match on role/capabilities/query_me_when/status/project; returns each match's session_name for SendMessage.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": STR, "include_stale": BOOL_FALSE},
            "required": ["query"],
        },
        "handler": op_find_agents,
    },
    {
        "name": "list_agents_detailed",
        "description": "List all registered peer sessions with full records.",
        "inputSchema": {
            "type": "object",
            "properties": {"include_stale": BOOL_FALSE},
        },
        "handler": op_list_agents_detailed,
    },
    {
        "name": "deregister",
        "description": "Remove a session from the peer directory. Idempotent.",
        "inputSchema": {
            "type": "object",
            "properties": {"session_name": STR},
            "required": ["session_name"],
        },
        "handler": op_deregister,
    },
]
TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


# ------------------------------------------------- dynamic per-agent tools

def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _peer_tool_name(session_name):
    return "peer_" + re.sub(r"[^A-Za-z0-9_-]", "_", session_name)[:100]


def fresh_agents(limit=None):
    """Fresh (non-stale) records, newest first. Empty list on any DB failure."""
    try:
        conn = connect()
    except Exception:
        return []
    try:
        rows = conn.execute("SELECT * FROM agents ORDER BY last_seen DESC").fetchall()
    finally:
        conn.close()
    recs = [r for r in (row_to_record(x) for x in rows) if not r["stale"]]
    return recs[:limit] if limit else recs


def dynamic_agent_tools():
    """The registry rendered as MCP tools — one per fresh agent, description
    = its routing card. Calling one returns the contact card; messaging is
    always native SendMessage."""
    if _env_int("LEDGER_AGENT_TOOLS", 1) <= 0:
        return []
    tools = []
    for rec in fresh_agents(_env_int("LEDGER_ROSTER_MAX", ROSTER_MAX_DEFAULT)):
        desc = " — ".join(x for x in (rec["role"], rec["status"]) if x) or "no role set"
        proj = f" [{rec['project']}]" if rec["project"] else ""
        ask = f" Ask when: {rec['query_me_when']}." if rec["query_me_when"] else ""
        tools.append({
            "name": _peer_tool_name(rec["session_name"]),
            "description": (
                f"Peer Claude session '{rec['session_name']}'{proj}: {desc}.{ask}"
                f" Call for its contact card; to actually talk to it, use"
                f" SendMessage (to: '{rec['session_name']}') — the ledger never"
                f" delivers messages."
            )[:400],
            "inputSchema": {"type": "object", "properties": {}},
        })
    return tools


def call_peer_tool(tool_name):
    for rec in fresh_agents():
        if _peer_tool_name(rec["session_name"]) == tool_name:
            rec["contact"] = (
                f"Address SendMessage to '{rec['session_name']}'. The ledger is"
                " a directory only; it does not deliver messages."
            )
            return rec
    raise ToolError(
        f"{tool_name}: peer no longer registered (or stale); use find_agents"
        " or native ListAgents instead"
    )


def call_tool(name, args):
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        raise ToolError(f"unknown tool: {name}")
    for req in tool["inputSchema"].get("required", []):
        if not args.get(req):
            raise ToolError(f"missing required argument: {req}")
    conn = connect()
    try:
        evict_stale(conn)
        return tool["handler"](conn, args)
    finally:
        conn.close()


_STDOUT_LOCK = threading.Lock()


def _write_msg(obj):
    with _STDOUT_LOCK:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def rpc_response(msg_id, result=None, error=None):
    resp = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        resp["error"] = error
    else:
        resp["result"] = result
    _write_msg(resp)


MUTATING_TOOLS = {"register", "update_registration", "deregister"}


def serve():
    # sig state for tools/list_changed: "served" = dynamic-tool signature the
    # client last fetched; "notified" = signature we last notified about
    # (avoids re-notifying every poll if the client never re-fetches).
    state = {"served": None, "notified": None}

    def dyn_sig():
        return json.dumps(dynamic_agent_tools(), sort_keys=True)

    def notify_if_changed():
        try:
            sig = dyn_sig()
        except Exception:
            return
        if state["served"] is not None and sig != state["served"] \
                and sig != state["notified"]:
            state["notified"] = sig
            _write_msg({"jsonrpc": "2.0",
                        "method": "notifications/tools/list_changed"})

    poll = _env_int("LEDGER_TOOLS_POLL", 20)
    if poll > 0 and _env_int("LEDGER_AGENT_TOOLS", 1) > 0:
        def watcher():
            import time
            while True:
                time.sleep(poll)
                notify_if_changed()
        threading.Thread(target=watcher, daemon=True).start()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        if msg_id is None:  # notification — nothing to answer
            continue
        if method == "initialize":
            rpc_response(msg_id, {
                "protocolVersion": msg.get("params", {}).get(
                    "protocolVersion", "2025-06-18"
                ),
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "ledger", "version": "1.0.0"},
            })
        elif method == "ping":
            rpc_response(msg_id, {})
        elif method == "tools/list":
            static = [{k: t[k] for k in ("name", "description", "inputSchema")}
                      for t in TOOLS]
            dyn = dynamic_agent_tools()
            state["served"] = json.dumps(dyn, sort_keys=True)
            rpc_response(msg_id, {"tools": static + dyn})
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name") or ""
            try:
                if name.startswith("peer_") and name not in TOOLS_BY_NAME:
                    result = call_peer_tool(name)
                else:
                    result = call_tool(name, params.get("arguments") or {})
                rpc_response(msg_id, {
                    "content": [{"type": "text", "text": json.dumps(result)}],
                    "isError": False,
                })
                if name in MUTATING_TOOLS:
                    notify_if_changed()  # our own registration changed the list
            except ToolError as exc:
                rpc_response(msg_id, {
                    "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                    "isError": True,
                })
            except Exception as exc:  # never crash the server on one bad call
                rpc_response(msg_id, {
                    "content": [{"type": "text", "text": json.dumps({"error": f"internal: {exc}"})}],
                    "isError": True,
                })
        else:
            rpc_response(msg_id, error={"code": -32601, "message": f"method not found: {method}"})


# ---------------------------------------------------------------- hook helpers

def infer_project(cwd):
    """basename of the git repo root if cwd is inside a git repo, else basename(cwd)."""
    if not cwd:
        return ""
    path = os.path.abspath(cwd)
    probe = path
    while True:
        if os.path.exists(os.path.join(probe, ".git")):
            return os.path.basename(probe)
        parent = os.path.dirname(probe)
        if parent == probe:
            return os.path.basename(path)
        probe = parent


def read_hook_input():
    try:
        data = json.loads(sys.stdin.read() or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def resolve_name(hook):
    """Name resolution priority: env var, session_title from hook stdin, derived."""
    env_name = os.environ.get("CLAUDE_LEDGER_NAME", "").strip()
    if env_name:
        return env_name, "env"
    title = (hook.get("session_title") or "").strip()
    if title:
        return title, "session_title"
    cwd = hook.get("cwd") or os.getcwd()
    sid = hook.get("session_id") or ""
    return f"{os.path.basename(os.path.abspath(cwd))}-{sid[:4]}", "derived"


def hook_register():
    hook = read_hook_input()
    name, source = resolve_name(hook)
    cwd = hook.get("cwd") or os.getcwd()
    args = {
        "session_name": name,
        "session_id": hook.get("session_id") or "",
        "cwd": cwd,
        "project": infer_project(cwd),
        "role": "unassigned",
        "status": "starting",
        "tmux_pane": os.environ.get("TMUX_PANE", ""),
        "pid": os.getppid() or None,
        "name_source": source,
    }
    call_tool("register", args)


def hook_deregister():
    hook = read_hook_input()
    name, _ = resolve_name(hook)
    call_tool("deregister", {"session_name": name})


def find_own_row(hook):
    """Best-effort match of this session to its ledger row.

    A manually /register-ed session may use a name unrelated to the derived
    one and carry no session_id, so match by session_id first, then by unique
    cwd (session-per-worktree makes cwd a good key), then by resolved name.
    Returns the record dict or None.
    """
    sid = hook.get("session_id") or ""
    cwd = hook.get("cwd") or os.getcwd()
    try:
        conn = connect()
    except Exception:
        return None
    try:
        if sid:
            row = conn.execute(
                "SELECT * FROM agents WHERE session_id = ?", (sid,)
            ).fetchone()
            if row:
                return row_to_record(row)
        rows = conn.execute(
            "SELECT * FROM agents WHERE cwd = ?", (cwd,)
        ).fetchall()
        if len(rows) == 1:
            return row_to_record(rows[0])
        name, _ = resolve_name(hook)
        row = conn.execute(
            "SELECT * FROM agents WHERE session_name = ?", (name,)
        ).fetchone()
        return row_to_record(row) if row else None
    finally:
        conn.close()


def hook_heartbeat():
    """Bump last_seen for the ledger row belonging to this session (and let
    op_heartbeat backfill session_id on first match). No-op if this session
    has no row — the roster nudge handles getting it registered."""
    hook = read_hook_input()
    own = find_own_row(hook)
    if own is None:
        return
    call_tool("heartbeat", {"session_name": own["session_name"],
                            "session_id": hook.get("session_id") or ""})


def build_roster(exclude_session_id="", exclude_name=""):
    """Compact peer roster for context injection. Empty string if no fresh peers."""
    conn = connect()
    try:
        evict_stale(conn)
        rows = conn.execute("SELECT * FROM agents ORDER BY last_seen DESC").fetchall()
    finally:
        conn.close()
    max_agents = _env_int("LEDGER_ROSTER_MAX", ROSTER_MAX_DEFAULT)
    lines = []
    for row in rows:
        rec = row_to_record(row)
        if rec["stale"]:
            continue
        if exclude_session_id and rec["session_id"] == exclude_session_id:
            continue
        if exclude_name and rec["session_name"] == exclude_name:
            continue
        desc = " — ".join(x for x in (rec["role"], rec["status"]) if x)
        proj = f" [{rec['project']}]" if rec["project"] else ""
        ask = f"; ask about: {rec['query_me_when']}" if rec["query_me_when"] else ""
        lines.append(f"- {rec['session_name']}{proj}: {desc}{ask}"[:200])
        if len(lines) >= max_agents:
            break
    if not lines:
        return ""
    header = (
        "[ledger] Active peer Claude sessions on this machine. Before making "
        "changes to a project listed here, consider coordinating with its agent "
        "first (SendMessage to the name below — e.g. a question or a requirements "
        "spec). Details: mcp__ledger__find_agents. The ledger is a directory, "
        "never a message channel."
    )
    return header + "\n" + "\n".join(lines)


def _roster_state_dir():
    return os.path.join(os.path.dirname(DB_PATH), "roster-state")


def hook_roster():
    """UserPromptSubmit hook: emit roster as additionalContext every N prompts.

    Fires on the first prompt of a session, then every LEDGER_ROSTER_EVERY
    prompts (counted per session_id). 0 disables. Prints nothing on off-cycle
    prompts or when the roster is empty.
    """
    every = _env_int("LEDGER_ROSTER_EVERY", ROSTER_EVERY_DEFAULT)
    if every <= 0:
        return
    hook = read_hook_input()
    sid = (hook.get("session_id") or "unknown").replace(os.sep, "_")
    state_dir = _roster_state_dir()
    os.makedirs(state_dir, exist_ok=True)
    state_path = os.path.join(state_dir, sid)

    fire = True
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                count = int(f.read().strip() or 0)
        except (ValueError, OSError):
            count = 0
        count += 1
        fire = count >= every
    with open(state_path, "w") as f:
        f.write("0" if fire else str(count))

    # Opportunistic prune of counters from long-dead sessions.
    try:
        import time
        for name in os.listdir(state_dir):
            p = os.path.join(state_dir, name)
            if time.time() - os.path.getmtime(p) > ROSTER_STATE_MAX_AGE:
                os.unlink(p)
    except OSError:
        pass

    if not fire:
        return
    own = find_own_row(hook)
    roster = build_roster(
        exclude_session_id=hook.get("session_id") or "",
        exclude_name=own["session_name"] if own else "",
    )
    parts = [p for p in (roster,) if p]
    if own is None:
        parts.append(REGISTER_NUDGE)
    if parts:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "\n\n".join(parts),
            }
        }))


def cli_list(include_stale):
    result = call_tool("list_agents_detailed", {"include_stale": include_stale})
    agents = result["agents"]
    if not agents:
        print("no registered agents")
        return
    cols = ["session_name", "role", "project", "status", "last_seen", "stale"]
    widths = {
        c: max(len(c), max(len(str(a[c])) for a in agents)) for c in cols
    }
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for a in agents:
        print("  ".join(str(a[c]).ljust(widths[c]) for c in cols))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "hook-register":
        try:
            hook_register()
        except Exception:
            pass  # registration failure must never disturb session start
    elif cmd == "hook-deregister":
        try:
            hook_deregister()
        except Exception:
            pass
    elif cmd == "hook-heartbeat":
        try:
            hook_heartbeat()
        except Exception:
            pass  # a dead ledger must never disturb the session
    elif cmd == "hook-roster":
        try:
            hook_roster()
        except Exception:
            pass  # a broken roster must never block prompt submission
    elif cmd == "roster":
        print(build_roster() or "(empty roster: no fresh registered agents)")
    elif cmd == "list":
        cli_list("--stale" in sys.argv[2:])
    else:
        sys.stderr.write(__doc__)
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
