"""Routing engine: candidate selection (priority / balanced / failover) and safe execution with failover.

Safety rules implemented here:
  * Tool execution happens in the client (OpenCode), never at the provider, so re-sending a *failed* LLM request
    cannot duplicate tool side effects.
  * A streamed response is NEVER switched to another model after its first byte reached the client. If it breaks
    mid-way the client gets an explicit error event and decides (task-level recovery), instead of a spliced answer.
  * Non-retryable errors (400-class bad requests) are returned to the client, not retried elsewhere.
"""
import json
import logging
import threading
import time
import uuid

import requests

from . import context as ctx
from . import guard
from .errors import (UpstreamError, classify_exception, classify_response, error_from_stream_event)
from .util import est_tokens, redact

log = logging.getLogger("gateway.router")


class NoCandidates(Exception):
    def __init__(self, message, attempts=None, retry_after=None, status=503):
        super().__init__(message)
        self.attempts, self.retry_after, self.status = attempts or [], retry_after, status


class Router:
    def __init__(self, registry, health, store, secrets, clock=time.time, session=None):
        self.registry, self.health, self.store, self.secrets, self.clock = registry, health, store, secrets, clock
        self.http = session or requests.Session()
        self._tl = threading.local()          # per-request scratch (the server is multi-threaded)

    # ------------------------------------------------------------------ capability gate
    def cap_status(self, provider, model_cfg, cap):
        row = self.store.q1("SELECT verified FROM caps WHERE provider=? AND model=? AND cap=?", (provider, model_cfg["id"], cap))
        if row is not None:
            return "verified" if row["verified"] else "failed"
        return "claimed" if cap in model_cfg.get("claims", []) else "unsupported"

    def _supports(self, provider, mc, need):
        r = self.registry.cfg["routing"]
        if need.get("tools"):
            s = self.cap_status(provider, mc, "tools")
            if s == "verified":
                pass
            elif s == "claimed" and not r["require_verified_tools"]:
                pass
            else:
                return False
        if need.get("stream"):
            s = self.cap_status(provider, mc, "stream")
            if s == "verified" or (s == "claimed" and not r["require_verified_streaming"]):
                pass
            else:
                return False
        if self.cap_status(provider, mc, "chat") in ("failed", "unsupported"):
            return False
        return True

    # ------------------------------------------------------------------ candidate ordering
    def candidates(self, alias, need, payload=None):
        members = self.registry.members(alias)
        if members is None:
            raise NoCandidates("unknown model alias '%s'" % alias, status=404)
        strategy = self.registry.strategy()
        margin = self.registry.cfg["context"]["safety_margin"]
        cands, skipped = [], []
        for pn, mc, pos in members:
            ok, reason = self.health.availability(pn)
            if not ok:
                skipped.append((pn, mc["id"], reason)); continue
            if not self.health.model_available(pn, mc["id"]):
                skipped.append((pn, mc["id"], "model_cooldown")); continue
            if not self._supports(pn, mc, need):
                skipped.append((pn, mc["id"], "capability_not_verified")); continue
            if payload is not None and not ctx.fits(payload, mc, margin):
                skipped.append((pn, mc["id"], "context_too_large")); continue
            cands.append((pn, mc, pos))
        prio = lambda c: (self.registry.effective(c[0])["priority"], c[2])
        if strategy == "balanced":
            w = self.registry.cfg["routing"]["balanced_weights"]
            cands.sort(key=lambda c: -self._score(c[0], c[1], w))
        elif strategy == "failover":
            cands.sort(key=prio)
            cands = self._apply_sticky(alias, cands)
        else:
            cands.sort(key=prio)
        self._tl.skipped = skipped
        return cands

    def _score(self, provider, mc, w):
        st = self.health.model_stats(provider, mc["id"])
        n = st["ok"] + st["fail"]
        reliability = (st["ok"] + 2) / (n + 2.0)                     # Laplace prior: unknown ~ 1.0
        if st["ewma_ms"]:
            speed = 1.0 / (1.0 + st["ewma_ms"] / 3000.0)             # 3 s -> 0.5
        else:
            speed = mc.get("speed", 5) / 10.0
        return w["quality"] * mc.get("quality", 5) / 10.0 + w["speed"] * speed + w["reliability"] * reliability

    def _apply_sticky(self, alias, cands):
        """Failover: stay on the current backup until it fails (no flapping); fail back after failback_seconds."""
        if not cands:
            return cands
        cur = self.store.kv_get("active:" + alias)
        now = self.clock()
        if cur:
            idx = next((i for i, c in enumerate(cands) if (c[0], c[1]["id"]) == tuple(cur["m"])), None)
            primary = cands[0]
            if idx is not None:
                is_primary = idx == 0
                if is_primary or now - cur["since"] < self.registry.cfg["routing"]["failback_seconds"]:
                    return [cands[idx]] + [c for i, c in enumerate(cands) if i != idx]
        return cands

    def _set_active(self, alias, provider, model):
        cur = self.store.kv_get("active:" + alias)
        if not cur or tuple(cur["m"]) != (provider, model):
            self.store.kv_set("active:" + alias, {"m": [provider, model], "since": self.clock()})
            if cur:
                self.store.event(self.clock(), provider, "failover", "%s: %s -> %s" % (alias, "/".join(cur["m"]), "%s/%s" % (provider, model)))

    # ------------------------------------------------------------------ upstream call plumbing
    def _build(self, provider, pcfg, mc, payload, key, stream):
        body = dict(payload)
        body["model"] = mc["id"]
        body["stream"] = bool(stream)
        body.pop("gateway", None)
        headers = {"Content-Type": "application/json"}
        if key["key"]:
            headers["Authorization"] = "Bearer " + key["key"]
        headers.update(pcfg.get("extra_headers", {}))
        url = pcfg["base_url"].rstrip("/") + "/chat/completions"
        return url, body, headers

    def _timeouts(self):
        r = self.registry.cfg["routing"]
        return (r["connect_timeout"], r["read_timeout"])

    def _usage_from(self, resp_json, payload, completion_chars):
        u = (resp_json or {}).get("usage") if isinstance(resp_json, dict) else None
        if u and u.get("prompt_tokens") is not None:
            return (int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0)), False)
        return (ctx.estimate_prompt_tokens(payload), est_tokens(completion_chars), True)

    def _journal(self, rid, alias, provider, model, stream, status, detail=""):
        now = self.clock()
        if status == "started":
            self.store.x("INSERT OR REPLACE INTO journal(id,ts,alias,provider,model,stream,status,detail) VALUES(?,?,?,?,?,?,?,?)",
                         (rid, now, alias, provider, model, int(stream), status, detail))
        else:
            self.store.x("UPDATE journal SET status=?, detail=?, finished=?, provider=?, model=? WHERE id=?",
                         (status, redact(detail)[:300], now, provider, model, rid))

    # ------------------------------------------------------------------ main entry
    def handle(self, payload, stream, rid=None):
        """Returns ("json", dict, meta) or ("stream", generator, meta). Raises NoCandidates / UpstreamError(bad_request)."""
        rid = rid or uuid.uuid4().hex[:12]
        alias = payload.get("model")
        need = {"tools": bool(payload.get("tools")), "stream": bool(stream)}
        cands = self.candidates(alias, need, payload)
        cfg_r = self.registry.cfg["routing"]
        attempts, notes = [], []
        if not cands:
            # context compaction safety net (opt-in), then give a precise explanation
            if self.registry.cfg["context"]["compaction"] and any(s[2] == "context_too_large" for s in self._tl.skipped):
                payload, note = self._compact(payload, alias)
                if note:
                    cands = self.candidates(alias, need, payload)
                    notes.append(note)
            if not cands:
                raise self._no_candidates(alias, need, self._tl.skipped)
        for pn, mc, pos in cands[: cfg_r["max_attempts"]]:
            pcfg = self.registry.effective(pn)
            tried_keys = []
            while True:
                key = self.health.pick_key(pn, tried_keys)
                if key is None:
                    attempts.append({"provider": pn, "model": mc["id"], "kind": "no_active_key"}); break
                self.health.note_attempt(pn, mc["id"])
                self._journal(rid, alias, pn, mc["id"], stream, "started")
                try:
                    if stream:
                        res = self._attempt_stream(rid, alias, pn, pcfg, mc, payload, key)
                    else:
                        res = self._attempt_json(rid, alias, pn, pcfg, mc, payload, key)
                    self._set_active(alias, pn, mc["id"])
                    res[2]["notes"] = notes
                    return res
                except UpstreamError as e:
                    attempts.append({"provider": pn, "model": mc["id"], "kind": e.kind, "status": e.status})
                    self._journal(rid, alias, pn, mc["id"], stream, "failed", "%s %s" % (e.kind, e.detail))
                    if e.kind == "bad_request":
                        raise
                    if e.kind == "context_length":
                        self.store.event(self.clock(), pn, "context_length", mc["id"])
                        break                                # not the provider's fault: just try a bigger-window model
                    self.health.record_failure(pn, mc["id"], e.kind, e.detail, e.retry_after, key["id"])
                    if e.kind == "auth":
                        tried_keys.append(key["id"])
                        continue                              # another active key for the same provider, if any
                    break
        raise self._no_candidates(alias, need, self._tl.skipped, attempts)

    def _no_candidates(self, alias, need, skipped, attempts=None):
        remaining = [self.health.state(p)["cooldown_until"] - self.clock() for p, _, r in skipped if str(r).startswith("cooldown")]
        remaining += [self.health.state(a["provider"])["cooldown_until"] - self.clock() for a in (attempts or []) if "provider" in a]
        remaining = [r for r in remaining if r > 0]
        msg = "no provider could serve alias '%s'" % alias
        reasons = {"%s/%s" % (p, m): r for p, m, r in skipped}
        if any(r == "capability_not_verified" for r in reasons.values()):
            msg += " (some models are excluded because the requested capability is not verified - run a capability probe)"
        e = NoCandidates(msg, attempts=[{"skipped": reasons}] + (attempts or []), retry_after=int(min(remaining)) + 1 if remaining else None)
        return e

    # ------------------------------------------------------------------ non-streaming
    def _attempt_json(self, rid, alias, pn, pcfg, mc, payload, key):
        url, body, headers = self._build(pn, pcfg, mc, payload, key, False)
        t0 = time.time()
        try:
            r = self.http.post(url, json=body, headers=headers, timeout=self._timeouts())
        except requests.exceptions.RequestException as e:
            raise classify_exception(e)
        if r.status_code != 200:
            raise classify_response(r.status_code, r.text[:2000], r.headers, self.clock())
        try:
            data = r.json()
        except ValueError:
            raise UpstreamError("server", 200, "invalid JSON from provider")
        problem = guard.validate_completion(data)
        if problem:
            raise UpstreamError("bad_output", 200, problem)
        chars = sum(len((c.get("message") or {}).get("content") or "") for c in data["choices"])
        self.health.record_success(pn, mc["id"], (time.time() - t0) * 1000, self._usage_from(data, payload, chars), r.headers,
                                   (mc["price_in"], mc["price_out"]))
        self._journal(rid, alias, pn, mc["id"], False, "completed")
        return "json", data, {"provider": pn, "model": mc["id"], "id": rid}

    # ------------------------------------------------------------------ streaming
    def _attempt_stream(self, rid, alias, pn, pcfg, mc, payload, key):
        url, body, headers = self._build(pn, pcfg, mc, payload, key, True)
        t0 = time.time()
        try:
            r = self.http.post(url, json=body, headers=headers, timeout=self._timeouts(), stream=True)
        except requests.exceptions.RequestException as e:
            raise classify_exception(e)
        if r.status_code != 200:
            txt = r.text[:2000]; r.close()
            raise classify_response(r.status_code, txt, r.headers, self.clock())
        it = r.iter_lines()
        first = None
        try:
            for line in it:
                if line and line.startswith(b"data:"):
                    first = line; break
        except requests.exceptions.RequestException as e:
            r.close(); raise classify_exception(e)
        if first is None:
            r.close(); raise UpstreamError("connection", 200, "stream closed before first event")
        payload_txt = first[5:].strip()
        if payload_txt != b"[DONE]":
            try:
                err = error_from_stream_event(json.loads(payload_txt))
            except ValueError:
                err = None
            if err:
                r.close(); raise err
        headers_snapshot = dict(r.headers)
        price = (mc["price_in"], mc["price_out"])
        health, clock, router = self.health, self.clock, self

        def gen():
            st = {"usage": None, "chars": 0, "done": False, "finish": False, "recorded": False}

            def finalize():
                if st["recorded"]:
                    return
                st["recorded"] = True
                u = ((st["usage"]["prompt_tokens"], st["usage"].get("completion_tokens", 0), False) if st["usage"]
                     else (ctx.estimate_prompt_tokens(payload), est_tokens(st["chars"]), True))
                health.record_success(pn, mc["id"], (time.time() - t0) * 1000, u, headers_snapshot, price)
                router._journal(rid, alias, pn, mc["id"], True, "completed")

            try:
                for line in _chain(first, it):
                    if not line:
                        continue
                    if line.startswith(b"data:"):
                        d = line[5:].strip()
                        if d == b"[DONE]":
                            st["done"] = True
                            finalize()                       # bookkeeping happens BEFORE the last byte reaches the client
                        else:
                            try:
                                obj = json.loads(d)
                                if obj.get("usage"):
                                    st["usage"] = obj["usage"]
                                for ch in obj.get("choices", []):
                                    st["chars"] += len((ch.get("delta") or {}).get("content") or "")
                                    if ch.get("finish_reason"):
                                        st["finish"] = True
                            except ValueError:
                                pass
                    yield line + b"\n\n"
                if not st["recorded"]:
                    if not st["finish"]:
                        raise requests.exceptions.ChunkedEncodingError("stream ended without completion marker")
                    finalize()
            except GeneratorExit:
                if not st["recorded"]:
                    router._journal(rid, alias, pn, mc["id"], True, "client_disconnected")
                raise
            except Exception as e:                                         # mid-stream failure: committed, cannot switch
                err = classify_exception(e) if isinstance(e, requests.exceptions.RequestException) else UpstreamError("server", None, type(e).__name__)
                health.record_failure(pn, mc["id"], err.kind, "mid-stream: " + err.detail, None, key["id"])
                router._journal(rid, alias, pn, mc["id"], True, "interrupted", "mid-stream: " + err.detail)
                msg = {"error": {"message": "upstream stream interrupted after output had started; the gateway does not splice "
                                           "answers from another model. Re-send the request or resume from PROJECT_STATE.md.",
                                 "type": "gateway_upstream_error", "code": "stream_interrupted", "provider": pn}}
                yield b"data: " + json.dumps(msg).encode() + b"\n\n"
                yield b"data: [DONE]\n\n"
            finally:
                r.close()

        return "stream", gen(), {"provider": pn, "model": mc["id"], "id": rid}

    # ------------------------------------------------------------------ opt-in compaction
    def _compact(self, payload, alias):
        c = self.registry.cfg["context"]
        msgs = payload.get("messages", [])
        system, middle, tail = ctx.split_for_compaction(msgs, c["keep_last_messages"])
        if not middle:
            return payload, None
        summ_alias = c.get("summarizer_alias") or alias
        usable = self.candidates(summ_alias, {"tools": False, "stream": False})       # only models that can really run now
        ctxs = [mc["context"] for _, mc, _ in usable if mc["context"]]
        biggest = max(ctxs) if ctxs else 0
        limit = max(2000, int(biggest * self.registry.cfg["context"]["safety_margin"] * 3.0) - 3000) if biggest else 60000
        text, truncated = ctx.render_for_summary(middle, limit)
        sp = {"model": summ_alias, "messages": [{"role": "system", "content": ctx.SUMMARY_PROMPT},
                                                {"role": "user", "content": text}],
              "max_tokens": min(2000, biggest // 4) if biggest else 2000}
        kind, data, _ = self.handle(sp, False)
        summary = data["choices"][0]["message"]["content"]
        warn = (" WARNING: the older history was too long for the summarizer window, so its middle part was NOT summarized "
                "and is missing - re-read files and PROJECT_STATE.md instead of trusting this summary.") if truncated else ""
        new = dict(payload)
        new["messages"] = system + [{"role": "system", "content": "[GATEWAY CONTEXT SUMMARY of %d earlier messages - generated by a model, "
                                                                "verify against files before relying on it.%s]\n%s" % (len(middle), warn, summary)}] + tail
        self.store.event(self.clock(), "-", "context_compacted", "%s: %d messages summarized%s" % (alias, len(middle), " (INPUT TRUNCATED)" if truncated else ""))
        return new, "compacted %d messages%s" % (len(middle), " (summarizer input truncated)" if truncated else "")


def _chain(first, it):
    yield first
    for x in it:
        yield x
