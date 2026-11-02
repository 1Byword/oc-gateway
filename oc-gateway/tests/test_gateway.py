import json
import logging
import os
import sys
import unittest

import requests

sys.path.insert(0, os.path.dirname(__file__))
from harness import ADMIN_KEY, Clock, Fake, GW, GW_KEY      # noqa: E402

logging.getLogger("werkzeug").setLevel(logging.ERROR)
TOOLS = [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]


class Base(unittest.TestCase):
    strategy = "priority"
    extra = None

    def setUp(self):
        self.a, self.b = Fake("a"), Fake("b")
        self.gw = GW([self.a, self.b], strategy=self.strategy, extra_cfg=self.extra)
        self.a.calls = self.b.calls = 0          # ignore probe traffic

    def tearDown(self):
        self.gw.stop(); self.a.stop(); self.b.stop()

    def served_by(self, r):
        return r.headers.get("X-Gateway-Provider")


class TestBasics(Base):
    def test_01_successful_request(self):
        r = self.gw.chat()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.served_by(r), "a")
        self.assertEqual(r.json()["choices"][0]["message"]["content"], "hello from a")
        self.assertEqual(self.gw.prov("a")["today"]["ok"] >= 1, True)

    def test_auth_required_and_throttled(self):
        self.assertEqual(self.gw.chat(key="wrong").status_code, 401)
        self.assertEqual(requests.post(self.gw.base + "/v1/chat/completions", json={}).status_code, 401)
        self.assertEqual(requests.get(self.gw.base + "/admin/api/status", headers={"Authorization": "Bearer " + GW_KEY}).status_code, 401)  # client key != admin
        codes = [self.gw.chat(key="wrong").status_code for _ in range(15)]
        self.assertIn(429, codes)                                 # brute force throttled

    def test_input_validation(self):
        h = {"Authorization": "Bearer " + GW_KEY}
        for bad in ({"model": "coder"}, {"model": "coder", "messages": []}, {"model": "coder", "messages": ["x"]},
                    {"messages": [{"role": "user", "content": "x"}]}, {"model": "coder", "messages": [{"role": "user"}], "temperature": "hot"}):
            self.assertEqual(requests.post(self.gw.base + "/v1/chat/completions", json=bad, headers=h).status_code, 400, bad)
        self.assertEqual(self.gw.chat(model="nope").status_code, 404)

    def test_secrets_never_exposed(self):
        self.gw.chat()
        blob = json.dumps(self.gw.status()) + self.gw.admin("GET", "/admin/api/events").text + self.gw.chat().text
        self.assertNotIn("sk-fake", blob)
        self.assertIn("****0000", blob)
        # at rest: ciphertext in DB must not contain the plaintext key
        raw = open(self.gw.db, "rb").read() + (open(self.gw.db + "-wal", "rb").read() if os.path.exists(self.gw.db + "-wal") else b"")
        self.assertNotIn(b"sk-fake-a-0000", raw)

    def test_upstream_receives_provider_key_not_gateway_key(self):
        self.gw.chat()
        self.assertEqual(self.a.seen[-1]["auth"], "Bearer sk-fake-a-0000")
        self.assertNotIn(GW_KEY, json.dumps(self.a.seen[-1]))


class TestFailures(Base):
    def test_02_http_429_rate_limit(self):
        self.a.mode = "429"
        r = self.gw.chat()
        self.assertEqual(self.served_by(r), "b")
        h = self.gw.prov("a")["health"]
        self.assertEqual(h["state"], "rate_limited")
        self.assertGreaterEqual(h["cooldown_remaining_s"], 59)
        n = self.a.calls
        for _ in range(3):
            self.gw.chat()
        self.assertEqual(self.a.calls, n, "provider in cooldown must not be called again")

    def test_03_quota_exhaustion(self):
        self.a.mode = "quota"
        self.assertEqual(self.served_by(self.gw.chat()), "b")
        h = self.gw.prov("a")["health"]
        self.assertEqual(h["state"], "quota_exhausted")
        self.assertGreaterEqual(h["cooldown_remaining_s"], 3500)
        self.assertEqual(self.gw.prov("a")["totals"]["qe"], 1)
        n = self.a.calls
        self.gw.chat(); self.gw.chat()
        self.assertEqual(self.a.calls, n)

    def test_04_invalid_key(self):
        self.a.mode = "401"
        self.assertEqual(self.served_by(self.gw.chat()), "b")
        k = self.gw.prov("a")["keys"][0]
        self.assertEqual(k["status"], "invalid")
        n = self.a.calls
        self.gw.chat()
        self.assertEqual(self.a.calls, n, "invalid key must not be retried")
        # operator replaces the key -> provider usable again
        self.a.mode = "ok"
        self.gw.admin("POST", "/admin/api/keys", {"provider": "a", "label": "t", "key": "sk-fake-a-0000"})
        self.assertEqual(self.served_by(self.gw.chat()), "a")

    def test_05_expired_key(self):
        self.a.mode = "expired"
        self.gw.chat()
        k = self.gw.prov("a")["keys"][0]
        self.assertEqual((k["status"], k["reason"]), ("invalid", "expired"))

    def test_06_provider_timeout(self):
        self.a.mode = "timeout"
        r = self.gw.chat()
        self.assertEqual(self.served_by(r), "b")
        self.assertEqual(self.gw.prov("a")["health"]["last_error_kind"], "timeout")

    def test_07_provider_outage_and_backoff(self):
        self.a.mode = "down"
        self.assertEqual(self.served_by(self.gw.chat()), "b")
        c1 = self.gw.prov("a")["health"]["cooldown_remaining_s"]
        self.gw.clock.advance(c1 + 1)                    # cooldown over -> half-open probe by next real request
        self.assertEqual(self.served_by(self.gw.chat()), "b")
        c2 = self.gw.prov("a")["health"]["cooldown_remaining_s"]
        self.assertGreater(c2, c1 * 1.5, "backoff must grow on repeated failure")

    def test_08_connection_refused_failover(self):
        self.a.stop()
        self.assertEqual(self.served_by(self.gw.chat()), "b")
        self.assertEqual(self.gw.prov("a")["health"]["last_error_kind"], "connection")
        self.a.__init__("a"); self.a.valid_key = "sk-fake-a-0000"   # (new port, irrelevant: only cleanup)

    def test_09_provider_recovery(self):
        self.a.mode = "down"
        self.gw.chat()
        self.assertFalse(self.gw.prov("a")["health"]["available"])
        self.a.mode = "ok"
        self.gw.clock.advance(10_000)
        self.assertTrue(self.gw.prov("a")["health"]["available"])
        r = self.gw.chat()
        self.assertEqual(self.served_by(r), "a")
        self.assertEqual(self.gw.prov("a")["health"]["state"], "healthy")

    def test_bad_request_is_not_retried_elsewhere(self):
        self.a.mode = "400"
        r = self.gw.chat()
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.b.calls, 0)

    def test_context_length_goes_to_next_without_penalty(self):
        self.a.mode = "ctx"
        self.assertEqual(self.served_by(self.gw.chat()), "b")
        self.assertTrue(self.gw.prov("a")["health"]["available"])

    def test_all_providers_down_gives_clear_503(self):
        self.a.mode = self.b.mode = "429"
        r = self.gw.chat()
        self.assertEqual(r.status_code, 503)
        self.assertIn("Retry-After", r.headers)
        self.assertEqual(r.json()["error"]["code"], "no_provider_available")

    def test_model_404_cooldown_without_provider_penalty(self):
        self.a.mode = "404"
        self.assertEqual(self.served_by(self.gw.chat()), "b")
        self.assertTrue(self.gw.prov("a")["health"]["available"])
        self.assertFalse(self.gw.prov("a")["models"][0]["model_available"])


class TestStrategies(Base):
    def test_priority_fails_back_immediately(self):
        self.a.mode = "down"; self.gw.chat()
        self.a.mode = "ok"; self.gw.clock.advance(10_000)
        self.assertEqual(self.served_by(self.gw.chat()), "a")

    def test_priority_change_from_dashboard(self):
        self.gw.admin("POST", "/admin/api/providers/b", {"priority": 1})
        self.assertEqual(self.served_by(self.gw.chat()), "b")

    def test_disable_provider(self):
        self.gw.admin("POST", "/admin/api/providers/a", {"enabled": False})
        self.assertEqual(self.served_by(self.gw.chat()), "b")
        self.assertEqual(self.a.calls, 0)

    def test_disable_key(self):
        kid = self.gw.prov("a")["keys"][0]["id"]
        self.gw.admin("POST", "/admin/api/keys/%d" % kid, {"enabled": False})
        self.assertEqual(self.served_by(self.gw.chat()), "b")

    def test_strategy_switch_endpoint(self):
        self.assertEqual(self.gw.admin("POST", "/admin/api/strategy", {"strategy": "bogus"}).status_code, 400)
        self.gw.admin("POST", "/admin/api/strategy", {"strategy": "balanced"})
        self.assertEqual(self.gw.status()["strategy"], "balanced")

    def test_cooldown_config_change(self):
        self.gw.admin("POST", "/admin/api/providers/a", {"cooldowns": {"rate_limit": 5}})
        self.a.mode = "429"
        # provider sends Retry-After: 30, which is honoured as the minimum
        self.gw.chat()
        self.assertGreaterEqual(self.gw.prov("a")["health"]["cooldown_remaining_s"], 29)
        self.assertEqual(self.gw.admin("POST", "/admin/api/providers/a", {"cooldowns": {"rate_limit": -1}}).status_code, 400)


class TestBalanced(Base):
    strategy = "balanced"
    extra = {"providers": {"a": {"models": [{"id": "m-a", "quality": 3, "speed": 5, "context": 32000, "claims": ["chat", "stream", "tools"]}]},
                           "b": {"models": [{"id": "m-b", "quality": 9, "speed": 5, "context": 32000, "claims": ["chat", "stream", "tools"]}]}}}

    def test_balanced_prefers_quality_then_adapts_to_failures(self):
        self.assertEqual(self.served_by(self.gw.chat()), "b")
        self.b.mode = "down"
        self.assertEqual(self.served_by(self.gw.chat()), "a")       # failover inside balanced


class TestFailoverStrategy(Base):
    strategy = "failover"

    def test_sticky_backup_then_failback(self):
        self.a.mode = "down"
        self.assertEqual(self.served_by(self.gw.chat()), "b")
        self.a.mode = "ok"; self.gw.clock.advance(300)              # a healthy again, but only 300s on backup
        self.assertEqual(self.served_by(self.gw.chat()), "b")       # no flapping
        self.gw.clock.advance(1000)                                  # > failback_seconds (900)
        self.assertEqual(self.served_by(self.gw.chat()), "a")


class TestStreaming(Base):
    def events(self, r):
        return [l for l in r.text.split("\n\n") if l.strip()]

    def test_10_streaming_response(self):
        r = self.gw.chat(stream=True)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["Content-Type"].startswith("text/event-stream"))
        ev = self.events(r)
        self.assertEqual(ev[-1], "data: [DONE]")
        text = "".join(json.loads(e[6:])["choices"][0]["delta"].get("content", "") for e in ev[:-1])
        self.assertEqual(text, "hello from a")

    def test_stream_failover_before_first_byte(self):
        self.a.mode = "stream_first_error"
        r = self.gw.chat(stream=True)
        self.assertEqual(self.served_by(r), "b")
        self.assertIn("hello", r.text)

    def test_stream_http_error_failover(self):
        self.a.mode = "429"
        self.assertEqual(self.served_by(self.gw.chat(stream=True)), "b")

    def test_midstream_failure_is_never_spliced(self):
        self.a.mode = "stream_drop"
        r = self.gw.chat(stream=True)
        self.assertEqual(self.served_by(r), "a")
        self.assertEqual(self.b.calls, 0, "must not silently switch model during a response")
        self.assertIn("stream_interrupted", r.text)
        self.assertTrue(r.text.rstrip().endswith("data: [DONE]"))
        self.assertNotEqual(self.gw.prov("a")["health"]["state"], "healthy")
        j = [x for x in self.gw.status()["recent_journal"] if x["status"] == "interrupted"]
        self.assertTrue(j)

    def test_usage_recorded_verified_vs_estimated(self):
        self.gw.chat(stream=True).text                                # consume the whole stream
        t = self.gw.prov("a")["today"]
        self.assertGreaterEqual(t["pt"], 9)
        self.assertEqual(t["te"], 0)                                 # provider reported usage -> not an estimate


class TestToolsAndCapabilities(Base):
    def test_11_tool_calling_roundtrip(self):
        r = self.gw.chat(tools=TOOLS)
        tc = r.json()["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(tc["function"]["name"], "get_weather")
        self.assertEqual(json.loads(tc["function"]["arguments"]), {"city": "Paris"})

    def test_invalid_tool_arguments_trigger_failover(self):
        self.a.mode = "bad_tool_json"
        r = self.gw.chat(tools=TOOLS)
        self.assertEqual(self.served_by(r), "b")

    def test_models_endpoint_lists_only_verified_capabilities(self):
        d = requests.get(self.gw.base + "/v1/models", headers={"Authorization": "Bearer " + GW_KEY}).json()
        self.assertEqual(d["data"][0]["id"], "coder")
        self.assertEqual(sorted(d["data"][0]["verified_capabilities"]), ["chat", "stream", "tools"])

    def test_unverified_tool_support_is_not_used(self):
        a2, b2 = Fake("c"), Fake("d")
        g = GW([a2, b2], probe=False)                                  # nothing probed yet
        try:
            r = g.chat(tools=TOOLS)
            self.assertEqual(r.status_code, 503)
            self.assertIn("probe", r.json()["error"]["message"])
            self.assertEqual(a2.calls + b2.calls, 0)
            d = requests.get(g.base + "/v1/models", headers={"Authorization": "Bearer " + GW_KEY}).json()
            self.assertEqual(d["data"][0]["verified_capabilities"], [])
            g.admin("POST", "/admin/api/probe/c")                      # now verify provider c only
            self.assertEqual(g.chat(tools=TOOLS).status_code, 200)
        finally:
            g.stop(); a2.stop(); b2.stop()

    def test_probe_does_not_verify_what_was_not_demonstrated(self):
        self.a.mode = "notools"                                        # answers text even when asked to call a tool
        self.gw.admin("POST", "/admin/api/probe/a")
        caps = self.gw.prov("a")["models"][0]["caps"]
        self.assertEqual(caps["chat"], "verified")
        self.assertEqual(caps["tools"], "failed")
        self.a.mode = "ok"
        self.assertEqual(self.served_by(self.gw.chat(tools=TOOLS)), "b")

    def test_connection_test_uses_models_endpoint(self):
        r = self.gw.admin("POST", "/admin/api/test/a").json()
        self.assertTrue(r["ok"])
        self.assertEqual(r["sample"], ["m-a"])


class TestQuotaInfo(Base):
    def test_verified_quota_from_headers_blocks_provider(self):
        self.a.headers_extra = {"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "45s"}
        self.gw.chat()
        q = self.gw.prov("a")["quota"]["verified_from_provider"]
        self.assertEqual(q["remaining_requests"], 0)
        self.assertIn("verified", q["source"])
        n = self.a.calls
        self.assertEqual(self.served_by(self.gw.chat()), "b")
        self.assertEqual(self.a.calls, n)
        self.gw.clock.advance(60)
        self.assertEqual(self.served_by(self.gw.chat()), "a")

    def test_estimated_rpm_limit_is_labelled_estimated(self):
        self.gw.cfg["providers"]["a"]["limits"] = {"rpm": 2}
        self.gw.restart()
        for _ in range(2):
            self.gw.chat()
        self.assertEqual(self.served_by(self.gw.chat()), "b")
        self.assertEqual(self.gw.prov("a")["health"]["reason"], "estimated_rpm_limit")
        self.assertIn("documented_limits_estimated", self.gw.prov("a")["quota"])


class TestContext(Base):
    HISTORY = [{"role": "system", "content": "You are a coding agent."},
               {"role": "user", "content": "Fix bug in src/app.py"},
               {"role": "assistant", "content": None, "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "read", "arguments": "{\"path\":\"src/app.py\"}"}}]},
               {"role": "tool", "tool_call_id": "t1", "content": "def f(): return 1/0"},
               {"role": "user", "content": "go on"}]

    def test_12_context_preserved_across_provider_switch(self):
        self.a.mode = "down"
        r = self.gw.chat(messages=self.HISTORY)
        self.assertEqual(self.served_by(r), "b")
        self.assertEqual(self.b.seen[-1]["body"]["messages"], self.HISTORY)   # byte-for-byte same context

    def test_13_session_continuation_across_switch(self):
        hist = [{"role": "user", "content": "turn 1"}]
        for turn in range(1, 5):
            if turn == 3:
                self.a.mode = "down"                                     # provider dies mid-session
            r = self.gw.chat(messages=list(hist))
            self.assertEqual(r.status_code, 200)
            who = self.a if self.served_by(r) == "a" else self.b
            self.assertEqual(who.seen[-1]["body"]["messages"], hist)       # full history every time
            hist += [{"role": "assistant", "content": r.json()["choices"][0]["message"]["content"]},
                     {"role": "user", "content": "turn %d" % (turn + 1)}]
        self.assertEqual(self.served_by(r), "b")

    def test_context_too_large_for_small_model_uses_larger_one(self):
        self.gw.cfg["providers"]["a"]["models"][0]["context"] = 50
        self.gw.restart()
        big = [{"role": "user", "content": "x" * 2000}]
        self.assertEqual(self.served_by(self.gw.chat(messages=big)), "b")

    def test_oversize_context_without_compaction_is_explicit_error(self):
        for n in "ab":
            self.gw.cfg["providers"][n]["models"][0]["context"] = 50
        self.gw.restart()
        r = self.gw.chat(messages=[{"role": "user", "content": "x" * 2000}])
        self.assertEqual(r.status_code, 503)                              # explicit; nothing silently truncated
        self.assertEqual(self.a.calls + self.b.calls, 0)


class TestCompaction(Base):
    extra = {"context": {"compaction": True, "keep_last_messages": 2},
             "providers": {"a": {"models": [{"id": "m-a", "context": 1500, "claims": ["chat", "stream", "tools"]}]}}}

    def test_compaction_is_explicit_and_keeps_recent_messages(self):
        a_only = [x for x in self.gw.cfg["aliases"]["coder"]["members"]]
        self.gw.admin("POST", "/admin/api/providers/b", {"enabled": False})
        msgs = [{"role": "system", "content": "sys"}] + [{"role": "user", "content": "old message %d " % i + "y" * 600} for i in range(8)] + \
               [{"role": "assistant", "content": "recent-1"}, {"role": "user", "content": "recent-2"}]
        r = self.gw.chat(messages=msgs)
        self.assertEqual(r.status_code, 200)
        self.assertIn("compacted", r.headers.get("X-Gateway-Context-Compacted", ""))   # never silent
        sent = self.a.seen[-1]["body"]["messages"]
        self.assertEqual(sent[0], msgs[0])
        self.assertIn("GATEWAY CONTEXT SUMMARY", sent[1]["content"])
        self.assertEqual(sent[-2:], msgs[-2:])
        events = self.gw.admin("GET", "/admin/api/events").json()
        self.assertTrue(any(e["kind"] == "context_compacted" for e in events))


class TestRestartAndRecovery(Base):
    def test_14_gateway_restart_preserves_state(self):
        self.a.mode = "quota"
        self.gw.chat()
        self.gw.app.gateway.store.x("INSERT INTO journal(id,ts,alias,provider,model,stream,status) VALUES('zz',1,'coder','a','m-a',1,'started')")
        self.gw.restart()
        h = self.gw.prov("a")["health"]
        self.assertEqual(h["state"], "quota_exhausted")                    # cooldown survived the restart
        n = self.a.calls
        self.assertEqual(self.served_by(self.gw.chat()), "b")
        self.assertEqual(self.a.calls, n)
        self.assertEqual(self.gw.prov("b")["keys"][0]["masked"], "****0000")  # encrypted keys survived and still decrypt
        j = {x["id"]: x["status"] for x in self.gw.status()["recent_journal"]}
        self.assertEqual(j["zz"], "interrupted")

    def test_15_database_recovery_from_backup(self):
        bdir = os.path.join(self.gw.tmp, "bk")
        self.gw.backup_dir = bdir
        self.gw.restart()
        self.assertEqual(self.gw.admin("POST", "/admin/api/backup").status_code, 200)
        self.gw.stop(); self.gw.app.gateway.store.conn.close()
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(self.gw.db + ext):
                os.remove(self.gw.db + ext)
        open(self.gw.db, "wb").write(b"this is not a sqlite database" * 100)     # corruption
        self.gw.start()
        self.assertEqual(requests.get(self.gw.base + "/health").json()["status"], "ok")
        self.assertEqual(self.served_by(self.gw.chat()), "a")                  # keys restored from backup
        self.assertTrue([f for f in os.listdir(self.gw.tmp) if "corrupt" in f])

    def test_15b_database_recovery_without_backup_starts_fresh(self):
        self.gw.stop(); self.gw.app.gateway.store.conn.close()
        for ext in ("-wal", "-shm"):
            if os.path.exists(self.gw.db + ext):
                os.remove(self.gw.db + ext)
        open(self.gw.db, "wb").write(b"garbage" * 500)
        self.gw.start()                                                         # start() re-adds keys via the admin API
        self.assertEqual(requests.get(self.gw.base + "/health").json()["status"], "ok")


class TestDynamicProviders(Base):
    def test_add_provider_at_runtime(self):
        c = Fake("c"); c.valid_key = "sk-fake-c-0000"
        try:
            body = {"name": "c", "base_url": "http://127.0.0.1:%d/v1" % c.port, "priority": 1, "alias": "coder",
                    "models": [{"id": "m-c", "claims": ["chat", "stream", "tools"], "context": 32000}]}
            self.assertEqual(self.gw.admin("POST", "/admin/api/providers", body).status_code, 201)
            self.gw.admin("POST", "/admin/api/keys", {"provider": "c", "key": "sk-fake-c-0000"})
            self.gw.admin("POST", "/admin/api/probe/c")
            self.assertEqual(self.served_by(self.gw.chat()), "c")
        finally:
            c.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
