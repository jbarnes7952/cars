#!/usr/bin/env python3
"""Smoke tests for ledger-mcp. Stdlib only. Run: python3 test_ledger.py"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "ledger_mcp.py")


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "ledger.db")
        os.environ["CLAUDE_LEDGER_DB"] = self.db
        sys.path.insert(0, HERE)
        for mod in ("ledger_mcp",):
            sys.modules.pop(mod, None)
        import ledger_mcp
        ledger_mcp.DB_PATH = self.db
        self.ledger = ledger_mcp

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("CLAUDE_LEDGER_DB", None)
        os.environ.pop("CLAUDE_LEDGER_NAME", None)

    def call(self, tool, **args):
        return self.ledger.call_tool(tool, args)

    def events(self, **where):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        clause = " AND ".join(f"{k} = ?" for k in where) or "1=1"
        rows = conn.execute(
            f"SELECT * FROM events WHERE {clause} ORDER BY id", tuple(where.values())
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- basics

    def test_register_and_list(self):
        rec = self.call(
            "register", session_name="alpha", session_id="u-1", cwd="/tmp/x",
            role="schema-owner", capabilities=["sql", "migrations"],
            query_me_when="schema questions", status="working", project="projx",
        )
        self.assertEqual(rec["session_name"], "alpha")
        self.assertEqual(rec["capabilities"], ["sql", "migrations"])
        self.assertFalse(rec["stale"])
        out = self.call("list_agents_detailed")
        self.assertEqual(out["count"], 1)
        self.assertEqual(self.events(event="register")[0]["session_name"], "alpha")

    def test_reregister_upserts_no_duplicates(self):
        self.call("register", session_name="alpha", session_id="u-1", role="a")
        self.call("register", session_name="alpha", session_id="u-2", role="b")
        out = self.call("list_agents_detailed")
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["agents"][0]["role"], "b")
        self.assertEqual(len(self.events(event="register")), 2)

    def test_update_partial_keeps_untouched_fields(self):
        self.call(
            "register", session_name="alpha", role="dev",
            capabilities=["x"], query_me_when="stuff", status="starting",
        )
        rec = self.call("update_registration", session_name="alpha", status="reviewing")
        self.assertEqual(rec["status"], "reviewing")
        self.assertEqual(rec["role"], "dev")
        self.assertEqual(rec["capabilities"], ["x"])
        payload = json.loads(self.events(event="update")[0]["payload"])
        self.assertIn("status", payload)
        self.assertNotIn("role", payload)

    def test_update_unregistered_errors(self):
        with self.assertRaises(self.ledger.ToolError) as ctx:
            self.call("update_registration", session_name="ghost", status="x")
        self.assertIn("register first", str(ctx.exception))

    def test_deregister_idempotent(self):
        self.call("register", session_name="alpha")
        self.call("deregister", session_name="alpha")
        self.call("deregister", session_name="alpha")  # unknown: succeeds silently
        self.call("deregister", session_name="never-existed")
        self.assertEqual(self.call("list_agents_detailed")["count"], 0)
        self.assertEqual(len(self.events(event="deregister")), 1)

    # -------------------------------------------------------------- find

    def test_find_agents_matches_descriptive_fields(self):
        self.call("register", session_name="a", role="schema-owner",
                  capabilities=["postgres"], query_me_when="db migrations")
        self.call("register", session_name="b", role="frontend",
                  status="building dashboard", project="webapp")
        hit = self.call("find_agents", query="MIGRATION")
        self.assertEqual([a["session_name"] for a in hit["agents"]], ["a"])
        hit = self.call("find_agents", query="webapp")
        self.assertEqual([a["session_name"] for a in hit["agents"]], ["b"])
        self.assertEqual(self.call("find_agents", query="nothing-matches")["count"], 0)

    # -------------------------------------------------------------- staleness

    def _age(self, name, seconds):
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE agents SET last_seen = ? WHERE session_name = ?",
                     (old, name))
        conn.commit()
        conn.close()

    def test_stale_flag_and_filtering(self):
        self.call("register", session_name="old")
        self.call("register", session_name="fresh")
        self._age("old", 11 * 60)
        self.assertEqual(self.call("list_agents_detailed")["count"], 1)
        both = self.call("list_agents_detailed", include_stale=True)
        self.assertEqual(both["count"], 2)
        flags = {a["session_name"]: a["stale"] for a in both["agents"]}
        self.assertTrue(flags["old"])
        self.assertFalse(flags["fresh"])

    def test_eviction_after_24h(self):
        self.call("register", session_name="dead")
        self._age("dead", 25 * 3600)
        self.call("list_agents_detailed", include_stale=True)  # any call triggers
        self.assertEqual(
            self.call("list_agents_detailed", include_stale=True)["count"], 0)
        self.assertEqual(len(self.events(event="evicted", session_name="dead")), 1)

    def test_heartbeat_bumps_and_samples(self):
        self.call("register", session_name="hb")
        r1 = self.call("heartbeat", session_name="hb")
        r2 = self.call("heartbeat", session_name="hb")
        self.assertTrue(r1["registered"] and r2["registered"])
        # register wrote no heartbeat; two rapid heartbeats -> one sampled event
        self.assertEqual(len(self.events(event="heartbeat", session_name="hb")), 1)
        unknown = self.call("heartbeat", session_name="ghost")
        self.assertFalse(unknown["registered"])

    # -------------------------------------------------------------- hooks

    def test_hook_register_env_name_priority(self):
        os.environ["CLAUDE_LEDGER_NAME"] = "minted-name"
        proc = subprocess.run(
            [sys.executable, SERVER, "hook-register"],
            input=json.dumps({"session_id": "abcd1234", "cwd": self.tmp.name}),
            capture_output=True, text=True, env=os.environ.copy(),
        )
        self.assertEqual(proc.returncode, 0)
        rec = self.call("list_agents_detailed")["agents"][0]
        self.assertEqual(rec["session_name"], "minted-name")
        self.assertEqual(rec["role"], "unassigned")
        self.assertEqual(rec["status"], "starting")
        payload = json.loads(self.events(event="register")[0]["payload"])
        self.assertEqual(payload["name_source"], "env")

    def test_hook_register_derived_name(self):
        os.environ.pop("CLAUDE_LEDGER_NAME", None)
        cwd = os.path.join(self.tmp.name, "myproj")
        os.makedirs(cwd)
        proc = subprocess.run(
            [sys.executable, SERVER, "hook-register"],
            input=json.dumps({"session_id": "abcd1234-x", "cwd": cwd}),
            capture_output=True, text=True, env=os.environ.copy(),
        )
        self.assertEqual(proc.returncode, 0)
        rec = self.call("list_agents_detailed")["agents"][0]
        self.assertEqual(rec["session_name"], "myproj-abcd")
        payload = json.loads(self.events(event="register")[0]["payload"])
        self.assertEqual(payload["name_source"], "derived")

    def test_hook_deregister(self):
        os.environ["CLAUDE_LEDGER_NAME"] = "minted-name"
        self.call("register", session_name="minted-name")
        proc = subprocess.run(
            [sys.executable, SERVER, "hook-deregister"],
            input="{}", capture_output=True, text=True, env=os.environ.copy(),
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.call("list_agents_detailed")["count"], 0)

    def test_hook_register_survives_bad_input(self):
        os.environ["CLAUDE_LEDGER_NAME"] = "resilient"
        proc = subprocess.run(
            [sys.executable, SERVER, "hook-register"],
            input="this is not json", capture_output=True, text=True,
            env=os.environ.copy(),
        )
        self.assertEqual(proc.returncode, 0)

    def test_infer_project_git_root(self):
        repo = os.path.join(self.tmp.name, "repo")
        sub = os.path.join(repo, "src", "deep")
        os.makedirs(os.path.join(repo, ".git"))
        os.makedirs(sub)
        self.assertEqual(self.ledger.infer_project(sub), "repo")
        bare = os.path.join(self.tmp.name, "norepo")
        os.makedirs(bare)
        self.assertEqual(self.ledger.infer_project(bare), "norepo")

    # -------------------------------------------------------------- roster hook

    def _roster_hook(self, session_id="sess-a", every="3"):
        env = dict(os.environ, LEDGER_ROSTER_EVERY=every)
        proc = subprocess.run(
            [sys.executable, SERVER, "hook-roster"],
            input=json.dumps({"session_id": session_id}),
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 0)
        return proc.stdout.strip()

    def test_roster_fires_first_prompt_then_every_n(self):
        self.call("register", session_name="peer-1", session_id="other",
                  role="schema-owner", project="webapp", status="migrating db",
                  query_me_when="db schema questions")
        first = self._roster_hook()          # prompt 1: fires
        payload = json.loads(first)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"],
                         "UserPromptSubmit")
        self.assertIn("peer-1", ctx)
        self.assertIn("webapp", ctx)
        self.assertIn("db schema questions", ctx)
        self.assertEqual(self._roster_hook(), "")   # prompt 2: quiet
        self.assertEqual(self._roster_hook(), "")   # prompt 3: quiet
        self.assertNotEqual(self._roster_hook(), "")  # prompt 4: fires again

    def test_roster_excludes_self_and_stale(self):
        self.call("register", session_name="me", session_id="sess-a")
        self.call("register", session_name="old-peer", session_id="other-1")
        self.call("register", session_name="fresh-peer", session_id="other-2",
                  role="tester")
        self._age("old-peer", 11 * 60)
        ctx = json.loads(self._roster_hook())["hookSpecificOutput"][
            "additionalContext"]
        self.assertIn("fresh-peer", ctx)
        self.assertNotIn("old-peer", ctx)
        self.assertNotIn("- me", ctx)

    def test_roster_disabled_and_empty(self):
        self.call("register", session_name="peer-1", session_id="other")
        self.assertEqual(self._roster_hook(every="0"), "")  # disabled
        self.call("deregister", session_name="peer-1")
        self.assertEqual(self._roster_hook(session_id="sess-b"), "")  # empty

    def test_roster_max_cap(self):
        for i in range(5):
            self.call("register", session_name=f"peer-{i}", session_id=f"o{i}")
        env = dict(os.environ, LEDGER_ROSTER_EVERY="1", LEDGER_ROSTER_MAX="2")
        proc = subprocess.run(
            [sys.executable, SERVER, "hook-roster"],
            input=json.dumps({"session_id": "sess-cap"}),
            capture_output=True, text=True, env=env,
        )
        ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(sum(1 for l in ctx.splitlines() if l.startswith("- ")), 2)

    # -------------------------------------------------------------- agent tools

    def _stdio(self, msgs, **env_extra):
        env = dict(os.environ, LEDGER_TOOLS_POLL="0", **env_extra)
        proc = subprocess.run(
            [sys.executable, SERVER, "serve"],
            input="".join(json.dumps(m) + "\n" for m in msgs),
            capture_output=True, text=True, env=env, timeout=30,
        )
        lines = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
        responses = {m["id"]: m for m in lines if "id" in m}
        notifications = [m["method"] for m in lines if "id" not in m]
        return responses, notifications

    INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "0"}}}

    def test_agent_tools_appear_and_notify_in_stream(self):
        msgs = [
            self.INIT,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "register",
                        "arguments": {"session_name": "pg-owner",
                                      "role": "schema-owner",
                                      "project": "webapp",
                                      "query_me_when": "before schema changes"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "peer_pg-owner", "arguments": {}}},
        ]
        responses, notifications = self._stdio(msgs)
        self.assertTrue(
            responses[1]["result"]["capabilities"]["tools"]["listChanged"])
        first = {t["name"] for t in responses[2]["result"]["tools"]}
        self.assertNotIn("peer_pg-owner", first)
        self.assertIn("notifications/tools/list_changed", notifications)
        second = {t["name"]: t for t in responses[4]["result"]["tools"]}
        self.assertIn("peer_pg-owner", second)
        self.assertIn("before schema changes", second["peer_pg-owner"]["description"])
        card = json.loads(responses[5]["result"]["content"][0]["text"])
        self.assertEqual(card["session_name"], "pg-owner")
        self.assertIn("SendMessage", card["contact"])

    def test_agent_tools_disabled(self):
        self.call("register", session_name="pg-owner")
        msgs = [self.INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        responses, notifications = self._stdio(msgs, LEDGER_AGENT_TOOLS="0")
        names = {t["name"] for t in responses[2]["result"]["tools"]}
        self.assertEqual(len(names), 6)
        self.assertEqual(notifications, [])

    def test_peer_tool_gone_after_deregister(self):
        self.call("register", session_name="pg-owner")
        msgs = [
            self.INIT,
            {"jsonrpc": "2.0", "id": 10, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "deregister",
                        "arguments": {"session_name": "pg-owner"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "peer_pg-owner", "arguments": {}}},
        ]
        responses, notifications = self._stdio(msgs)
        self.assertIn("notifications/tools/list_changed", notifications)
        self.assertTrue(responses[3]["result"]["isError"])

    def test_watcher_notifies_on_external_registration(self):
        import threading
        env = dict(os.environ, LEDGER_TOOLS_POLL="1")
        proc = subprocess.Popen(
            [sys.executable, SERVER, "serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env,
        )
        got = threading.Event()
        try:
            proc.stdin.write(json.dumps(self.INIT) + "\n")
            proc.stdin.write(json.dumps(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
            proc.stdin.flush()
            # wait until tools/list has been SERVED before mutating, else the
            # served snapshot already contains the newcomer and nothing changes
            for _ in range(2):
                self.assertIn("\"id\"", proc.stdout.readline())
            def reader():
                for line in proc.stdout:
                    if "notifications/tools/list_changed" in line:
                        got.set()
            threading.Thread(target=reader, daemon=True).start()
            # external mutation: another session registers against the same DB
            self.call("register", session_name="late-arrival", role="tester")
            self.assertTrue(got.wait(timeout=10),
                            "no list_changed within 10s of external register")
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)

    # -------------------------------------------------------------- MCP stdio

    def test_mcp_stdio_end_to_end(self):
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "register",
                        "arguments": {"session_name": "stdio-agent",
                                      "role": "tester",
                                      "capabilities": ["testing"]}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "find_agents", "arguments": {"query": "test"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "update_registration",
                        "arguments": {"session_name": "nope", "status": "x"}}},
            {"jsonrpc": "2.0", "id": 6, "method": "no/such/method"},
        ]
        proc = subprocess.run(
            [sys.executable, SERVER, "serve"],
            input="".join(json.dumps(m) + "\n" for m in msgs),
            capture_output=True, text=True, env=os.environ.copy(), timeout=30,
        )
        parsed = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
        responses = {r["id"]: r for r in parsed if "id" in r}
        self.assertEqual(len(responses), 6)  # notification got no response
        init = responses[1]["result"]
        self.assertEqual(init["serverInfo"]["name"], "ledger")
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        tools = {t["name"] for t in responses[2]["result"]["tools"]}
        self.assertEqual(tools, {"register", "update_registration", "heartbeat",
                                 "find_agents", "list_agents_detailed", "deregister"})
        reg = json.loads(responses[3]["result"]["content"][0]["text"])
        self.assertEqual(reg["session_name"], "stdio-agent")
        found = json.loads(responses[4]["result"]["content"][0]["text"])
        self.assertEqual(found["agents"][0]["session_name"], "stdio-agent")
        self.assertTrue(responses[5]["result"]["isError"])
        self.assertEqual(responses[6]["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main(verbosity=2)
