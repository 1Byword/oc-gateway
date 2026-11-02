"""Fake upstream providers + helpers. Failures are simulated locally: no real provider quota is consumed."""
import json
import os
import sys
import tempfile
import threading
import time

import requests
from flask import Flask, Response, jsonify, request
from werkzeug.serving import make_server

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gateway"))
from app.secrets_box import SecretBox          # noqa: E402
from app.server import create_app              # noqa: E402
from app.util import sha256_hex                # noqa: E402

GW_KEY, ADMIN_KEY = "gw-test-key-123456", "admin-test-token-123456"


class Fake:
    """A fake OpenAI-compatible provider whose behaviour is switched with .mode"""

    def __init__(self, name):
        self.name, self.mode, self.calls, self.seen = name, "ok", 0, []
        self.headers_extra = {}
        a = Flask(name)

        @a.get("/v1/models")
        def models():
            if request.headers.get("Authorization") != "Bearer " + self.valid_key:
                return jsonify({"error": "bad key"}), 401
            return jsonify({"data": [{"id": "m-" + name, "context_length": 32000}]})

        @a.post("/v1/chat/completions")
        def chat():
            self.calls += 1
            body = request.get_json()
            self.seen.append({"body": body, "auth": request.headers.get("Authorization")})
            m = self.mode
            if m == "429":
                return jsonify({"error": {"message": "Rate limit reached, slow down"}}), 429, {"Retry-After": "30"}
            if m == "quota":
                return jsonify({"error": {"message": "You exceeded your current quota (daily limit)"}}), 429
            if m == "401":
                return jsonify({"error": {"message": "Invalid API key"}}), 401
            if m == "expired":
                return jsonify({"error": {"message": "API key expired"}}), 401
            if m == "500":
                return jsonify({"error": "boom"}), 500
            if m == "down":
                return jsonify({"error": "unavailable"}), 503
            if m == "timeout":
                time.sleep(2.0)
                return jsonify({"error": "late"}), 200
            if m == "400":
                return jsonify({"error": {"message": "unsupported parameter foo"}}), 400
            if m == "ctx":
                return jsonify({"error": {"message": "maximum context length exceeded"}}), 400
            if m == "404":
                return jsonify({"error": {"message": "model not found"}}), 404
            if body.get("stream"):
                return self._stream(body)
            if m == "bad_tool_json":
                return jsonify(self._tool_resp("{not json"))
            if body.get("tools") and m in ("ok", "tools"):
                return jsonify(self._tool_resp(json.dumps({"city": "Paris"})))
            if m == "notools":
                pass
            return jsonify({"id": "x", "object": "chat.completion", "model": body["model"],
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello from " + name}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}}), 200, self.headers_extra

        self.app = a
        self.valid_key = None
        self.server = make_server("127.0.0.1", 0, a, threaded=True)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _tool_resp(self, args):
        return {"id": "x", "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "get_weather", "arguments": args}}]}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 8}}

    def _stream(self, body):
        m = self.mode

        def gen():
            if m == "stream_first_error":
                yield 'data: {"error": {"code": 429, "message": "rate limit"}}\n\n'
                return
            for i, w in enumerate(["hello ", "from ", self.name]):
                yield "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": w}}]}) + "\n\n"
                if m == "stream_drop" and i == 1:
                    raise RuntimeError("connection dropped")       # aborts the chunked response mid-way
            yield "data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                                          "usage": {"prompt_tokens": 9, "completion_tokens": 3}}) + "\n\n"
            yield "data: [DONE]\n\n"
        return Response(gen(), mimetype="text/event-stream")

    def stop(self):
        self.server.shutdown()


def provider_cfg(fake, priority, key_env=None, **kw):
    cfg = {"base_url": "http://127.0.0.1:%d/v1" % fake.port, "priority": priority,
           "models": [{"id": "m-" + fake.name, "quality": kw.pop("quality", 5), "speed": kw.pop("speed", 5), "context": kw.pop("context", 32000),
                       "claims": ["chat", "stream", "tools"]}]}
    cfg.update(kw)
    return cfg


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class GW:
    """A running gateway instance wired to fake providers."""

    def __init__(self, fakes, strategy="priority", db=None, clock=None, extra_cfg=None, probe=True, backup_dir=None, tmpdir=None):
        self.fakes = fakes
        self.tmp = tmpdir or tempfile.mkdtemp()
        self.db = db or os.path.join(self.tmp, "gw.sqlite")
        self.clock = clock or Clock()
        self.master = SecretBox.generate()
        providers = {}
        for i, f in enumerate(fakes):
            f.valid_key = "sk-fake-%s-0000" % f.name
            providers[f.name] = provider_cfg(f, 10 * (i + 1))
        cfg = {"routing": {"default_strategy": strategy, "read_timeout": 0.6, "connect_timeout": 1, "max_attempts": 5,
                           "cooldowns": {"rate_limit": 60, "quota": 3600, "server": 120, "timeout": 60, "connection": 120}},
               "providers": providers,
               "aliases": {"coder": {"members": [{"provider": f.name, "model": "m-" + f.name} for f in fakes]}}}
        if extra_cfg:
            from app.config import _merge
            cfg = _merge(cfg, extra_cfg)
        self.cfg = cfg
        self.backup_dir = backup_dir
        self.start()
        if probe:
            for f in fakes:
                self.admin("POST", "/admin/api/probe/" + f.name)

    def env(self):
        return {"GATEWAY_MASTER_KEY": self.master, "GATEWAY_KEY_HASHES": sha256_hex(GW_KEY),
                "ADMIN_TOKEN_HASHES": sha256_hex(ADMIN_KEY)}

    def start(self):
        self.app = create_app(self.cfg, self.db, self.env(), clock=self.clock, backup_dir=self.backup_dir)
        self.server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.base = "http://127.0.0.1:%d" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        # keys are added through the admin API exactly like a real operator would
        existing = {k["provider"] for k in self.app.gateway.store.q("SELECT provider FROM keys")}
        for f in self.fakes:
            if f.name not in existing:
                self.admin("POST", "/admin/api/keys", {"provider": f.name, "label": "t", "key": f.valid_key})

    def stop(self):
        self.server.shutdown()

    def restart(self):
        self.stop()
        self.app.gateway.store.conn.close()
        self.start()

    def admin(self, method, path, body=None):
        return requests.request(method, self.base + path, json=body, headers={"Authorization": "Bearer " + ADMIN_KEY}, timeout=10)

    def chat(self, messages=None, stream=False, tools=None, key=GW_KEY, model="coder", **kw):
        body = {"model": model, "messages": messages or [{"role": "user", "content": "hi"}], "stream": stream}
        if tools:
            body["tools"] = tools
        body.update(kw)
        return requests.post(self.base + "/v1/chat/completions", json=body, headers={"Authorization": "Bearer " + key}, timeout=10, stream=stream)

    def status(self):
        return self.admin("GET", "/admin/api/status").json()

    def prov(self, name):
        return next(p for p in self.status()["providers"] if p["name"] == name)
