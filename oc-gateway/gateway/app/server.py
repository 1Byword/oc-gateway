import hmac
import json
import logging
import os
import time
import uuid
import collections

from flask import Flask, Response, jsonify, request, send_from_directory

from . import __version__
from .config import env_keys_for, load_config
from .errors import UpstreamError
from .health import Health
from .probe import discover_models, probe_model
from .registry import Registry
from .router import NoCandidates, Router
from .secrets_box import SecretBox
from .store import Store
from .util import RedactFilter, mask_key, redact, sha256_hex

log = logging.getLogger("gateway")
STATIC = os.path.join(os.path.dirname(__file__), "..", "static")


def _bad(status, message, code=None, extra=None, headers=None):
    body = {"error": {"message": message, "type": "gateway_error", "code": code or str(status)}}
    if extra:
        body["error"].update(extra)
    resp = jsonify(body)
    resp.status_code = status
    for k, v in (headers or {}).items():
        resp.headers[k] = v
    return resp


def create_app(config, db_path, environ=None, clock=time.time, backup_dir=None, session=None):
    environ = os.environ if environ is None else environ
    cfg = load_config(config)
    logging.getLogger().addFilter(RedactFilter())
    for h in logging.getLogger().handlers:
        h.addFilter(RedactFilter())

    store = Store.open_with_recovery(db_path, backup_dir)
    store.mark_stale_journal(clock())
    secrets = SecretBox(environ.get("GATEWAY_MASTER_KEY"))
    registry = Registry(cfg, store)
    health = Health(store, registry, secrets, clock)
    router = Router(registry, health, store, secrets, clock, session)

    # keys supplied through environment variables are imported (encrypted) so the dashboard can manage them
    for prov, pairs in env_keys_for(cfg, environ).items():
        for var, val in pairs:
            row = store.q1("SELECT id FROM keys WHERE provider=? AND label=?", (prov, "env:" + var))
            if row:
                # an env change means the operator rotated the key: reactivate it
                store.x("UPDATE keys SET ciphertext=?, last4=?, status='active', reason=NULL WHERE id=?",
                        (secrets.encrypt(val), val[-4:], row["id"]))
            else:
                store.x("INSERT INTO keys(provider,label,ciphertext,last4,created) VALUES(?,?,?,?,?)",
                        (prov, "env:" + var, secrets.encrypt(val), val[-4:], clock()))

    client_hashes = {h.strip() for h in environ.get("GATEWAY_KEY_HASHES", "").split(",") if h.strip()}
    admin_hashes = {h.strip() for h in environ.get("ADMIN_TOKEN_HASHES", "").split(",") if h.strip()}
    trusted_proxy = environ.get("TRUSTED_PROXY", "0") == "1"
    sec = cfg["security"]

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = sec["max_body_bytes"]
    app.gateway = type("G", (), {"store": store, "router": router, "health": health, "registry": registry, "cfg": cfg, "secrets": secrets})()

    buckets = collections.defaultdict(lambda: [0.0, 0.0])      # client -> [tokens, last]
    fails = collections.defaultdict(collections.deque)

    def client_ip():
        if trusted_proxy and request.headers.get("X-Forwarded-For"):
            return request.headers["X-Forwarded-For"].split(",")[-1].strip()
        return request.remote_addr or "?"

    def _authenticate(hashes):
        ip = client_ip()
        now = clock()
        dq = fails[ip]
        while dq and dq[0] < now - 60:
            dq.popleft()
        if len(dq) >= sec["auth_fail_per_min"]:
            return None, _bad(429, "too many failed authentication attempts", "auth_throttled", headers={"Retry-After": "60"})
        auth = request.headers.get("Authorization", "")
        tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        h = sha256_hex(tok) if tok else ""
        if tok and any(hmac.compare_digest(h, x) for x in hashes):
            return h[:8], None
        dq.append(now)
        return None, _bad(401, "invalid or missing gateway key", "invalid_api_key")

    def client_auth():
        cid, err = _authenticate(client_hashes)
        if err:
            return None, err
        b = buckets[cid]
        now = clock()
        rate = sec["client_rpm"] / 60.0
        b[0] = min(sec["client_rpm"], b[0] + (now - b[1]) * rate) if b[1] else float(sec["client_rpm"])
        b[1] = now
        if b[0] < 1:
            return None, _bad(429, "gateway rate limit exceeded", "rate_limit_exceeded", headers={"Retry-After": "5"})
        b[0] -= 1
        return cid, None

    @app.after_request
    def secure_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.path.startswith("/dashboard") or request.path.startswith("/admin"):
            resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------------ public
    @app.get("/health")
    def health_ep():
        try:
            store.q1("SELECT 1 AS x")
            return jsonify({"status": "ok", "version": __version__})
        except Exception:
            return jsonify({"status": "db_error"}), 500

    @app.get("/v1/models")
    def models():
        cid, err = client_auth()
        if err:
            return err
        data = []
        for alias in registry.aliases():
            members = registry.members(alias) or []
            caps = {"chat": False, "stream": False, "tools": False}
            for pn, mc, _ in members:
                for c in caps:
                    if router.cap_status(pn, mc, c) == "verified":
                        caps[c] = True
            ctxs = [mc["context"] for _, mc, _ in members if mc["context"]]
            data.append({"id": alias, "object": "model", "created": 0, "owned_by": "gateway",
                         "verified_capabilities": [c for c, v in caps.items() if v],
                         "min_context_declared": min(ctxs) if ctxs else None})
        return jsonify({"object": "list", "data": data})

    @app.post("/v1/chat/completions")
    def chat():
        cid, err = client_auth()
        if err:
            return err
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _bad(400, "body must be a JSON object", "invalid_request")
        msgs = payload.get("messages")
        if not isinstance(msgs, list) or not msgs or not all(isinstance(m, dict) and "role" in m for m in msgs):
            return _bad(400, "'messages' must be a non-empty list of {role, content}", "invalid_request")
        if not isinstance(payload.get("model"), str):
            return _bad(400, "'model' (gateway alias) is required", "invalid_request")
        for f in ("temperature", "top_p", "max_tokens"):
            if f in payload and payload[f] is not None and not isinstance(payload[f], (int, float)):
                return _bad(400, "'%s' must be a number" % f, "invalid_request")
        stream = bool(payload.get("stream"))
        rid = uuid.uuid4().hex[:12]
        try:
            kind, result, meta = router.handle(payload, stream, rid)
        except NoCandidates as e:
            hdr = {"Retry-After": str(e.retry_after)} if e.retry_after else {}
            return _bad(e.status, str(e), "no_provider_available", {"attempts": e.attempts}, hdr)
        except UpstreamError as e:                         # non-retryable client error, passed through (redacted)
            return _bad(e.status if e.status and 400 <= e.status < 500 else 400, "provider rejected the request: " + e.detail, "upstream_bad_request")
        hdrs = {"X-Gateway-Provider": meta["provider"], "X-Gateway-Model": meta["model"], "X-Gateway-Request-Id": meta["id"]}
        if meta.get("notes"):
            hdrs["X-Gateway-Context-Compacted"] = "; ".join(meta["notes"])
        if kind == "stream":
            hdrs.update({"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
            return Response(result, mimetype="text/event-stream", headers=hdrs)
        resp = jsonify(result)
        for k, v in hdrs.items():
            resp.headers[k] = v
        return resp

    # ------------------------------------------------------------------ dashboard (static, no secrets inside)
    @app.get("/dashboard")
    def dash():
        return send_from_directory(STATIC, "dashboard.html")

    @app.get("/dashboard/app.js")
    def dash_js():
        return send_from_directory(STATIC, "app.js")

    # ------------------------------------------------------------------ admin API
    def admin(fn):
        def wrapper(*a, **kw):
            cid, err = _authenticate(admin_hashes)
            if err:
                return err
            return fn(*a, **kw)
        wrapper.__name__ = fn.__name__
        return wrapper

    def key_rows(provider=None):
        rows = store.q("SELECT id,provider,label,last4,status,enabled,reason,last_used,created FROM keys" +
                       (" WHERE provider=?" if provider else ""), (provider,) if provider else ())
        return [{"id": r["id"], "provider": r["provider"], "label": r["label"], "masked": "****" + r["last4"], "status": r["status"],
                 "enabled": bool(r["enabled"]), "reason": r["reason"], "last_used": r["last_used"]} for r in rows]

    @app.get("/admin/api/status")
    @admin
    def status():
        now = clock()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        provs = []
        for name in registry.providers():
            p = registry.effective(name)
            usage = store.q1("SELECT COALESCE(SUM(requests),0) req, COALESCE(SUM(ok),0) ok, COALESCE(SUM(rate_limited),0) rl, "
                             "COALESCE(SUM(quota_events),0) qe, COALESCE(SUM(errors),0) er, COALESCE(SUM(prompt_tokens),0) pt, "
                             "COALESCE(SUM(completion_tokens),0) ct, COALESCE(SUM(tokens_estimated),0) te, COALESCE(SUM(cost),0) cost "
                             "FROM usage WHERE provider=? AND day=?", (name, day))
            tot = store.q1("SELECT COALESCE(SUM(requests),0) req, COALESCE(SUM(rate_limited),0) rl, COALESCE(SUM(quota_events),0) qe "
                           "FROM usage WHERE provider=?", (name,))
            lim = p.get("limits", {})
            hs = health.public_state(name)
            models_out = []
            for mc in p.get("models", []):
                models_out.append({"id": mc["id"], "context": mc["context"] or None,
                                   "caps": {c: router.cap_status(name, mc, c) for c in ("chat", "stream", "tools")},
                                   "model_available": health.model_available(name, mc["id"])})
            provs.append({"name": name, "enabled": p["enabled"], "priority": p["priority"], "base_url": p["base_url"], "health": hs,
                          "keys": key_rows(name), "models": models_out, "cooldowns": p.get("cooldowns", {}),
                          "today": usage, "totals": tot,
                          "quota": {"documented_limits_estimated": lim,
                                    "estimated_requests_today": usage["req"],
                                    "verified_from_provider": hs["quota_verified"]}})
        return jsonify({"strategy": registry.strategy(), "strategies": ["priority", "balanced", "failover"], "providers": provs,
                        "aliases": {a: [{"provider": p, "model": m["id"]} for p, m, _ in (registry.members(a) or [])] for a in registry.aliases()},
                        "recent_journal": store.q("SELECT id,ts,alias,provider,model,stream,status,detail FROM journal ORDER BY ts DESC LIMIT 25"),
                        "version": __version__})

    @app.get("/admin/api/events")
    @admin
    def events():
        return jsonify(store.q("SELECT ts,provider,kind,detail FROM events ORDER BY id DESC LIMIT 200"))

    @app.post("/admin/api/strategy")
    @admin
    def set_strategy():
        s = (request.get_json(silent=True) or {}).get("strategy")
        if s not in ("priority", "balanced", "failover"):
            return _bad(400, "strategy must be priority|balanced|failover")
        store.kv_set("strategy", s)
        store.event(clock(), "-", "strategy_changed", s)
        return jsonify({"strategy": s})

    @app.post("/admin/api/providers")
    @admin
    def add_provider():
        d = request.get_json(silent=True) or {}
        name, base = d.get("name", ""), d.get("base_url", "")
        if not name.replace("-", "").replace("_", "").isalnum() or not base.startswith("https://") and not base.startswith("http://"):
            return _bad(400, "need a simple 'name' and an http(s) 'base_url'")
        if name in registry.providers():
            return _bad(409, "provider already exists")
        registry.add_provider(name, {k: d[k] for k in ("base_url", "priority", "limits", "models", "cooldowns", "auth", "extra_headers") if k in d})
        if d.get("alias"):
            members = [{"provider": name, "model": m["id"]} for m in d.get("models", [])]
            dyn = store.kv_get("dynamic_alias_members", {}) or {}
            dyn.setdefault(d["alias"], []).extend(members)
            store.kv_set("dynamic_alias_members", dyn)
        return jsonify({"ok": True}), 201

    @app.post("/admin/api/providers/<name>")
    @admin
    def update_provider(name):
        if not registry.provider(name):
            return _bad(404, "unknown provider")
        d = request.get_json(silent=True) or {}
        store.x("INSERT OR IGNORE INTO pstate(provider) VALUES(?)", (name,))
        if "enabled" in d:
            store.x("UPDATE pstate SET enabled=? WHERE provider=?", (int(bool(d["enabled"])), name))
        if "priority" in d:
            if not isinstance(d["priority"], int):
                return _bad(400, "priority must be an integer")
            store.x("UPDATE pstate SET priority=? WHERE provider=?", (d["priority"], name))
        if "cooldowns" in d:
            cds = d["cooldowns"]
            if not isinstance(cds, dict) or not all(isinstance(v, (int, float)) and 0 <= v <= 7 * 86400 for v in cds.values()):
                return _bad(400, "cooldowns must map kind -> seconds (0..604800)")
            store.x("UPDATE pstate SET cooldown_cfg=? WHERE provider=?", (json.dumps(cds), name))
        store.event(clock(), name, "provider_updated", json.dumps({k: v for k, v in d.items()}))
        return jsonify({"ok": True})

    @app.post("/admin/api/keys")
    @admin
    def add_key():
        d = request.get_json(silent=True) or {}
        prov, key, label = d.get("provider"), (d.get("key") or "").strip(), (d.get("label") or "").strip() or "key"
        if not registry.provider(prov):
            return _bad(404, "unknown provider")
        if len(key) < 8 or len(key) > 512 or any(c.isspace() for c in key):
            return _bad(400, "key looks invalid")
        if store.q1("SELECT id FROM keys WHERE provider=? AND label=?", (prov, label)):
            store.x("UPDATE keys SET ciphertext=?, last4=?, status='active', reason=NULL WHERE provider=? AND label=?",
                    (secrets.encrypt(key), key[-4:], prov, label))
        else:
            store.x("INSERT INTO keys(provider,label,ciphertext,last4,created) VALUES(?,?,?,?,?)", (prov, label, secrets.encrypt(key), key[-4:], clock()))
        store.event(clock(), prov, "key_added", "label=%s %s" % (label, mask_key(key)))
        return jsonify({"ok": True, "masked": mask_key(key)}), 201

    @app.post("/admin/api/keys/<int:kid>")
    @admin
    def toggle_key(kid):
        d = request.get_json(silent=True) or {}
        if not store.q1("SELECT id FROM keys WHERE id=?", (kid,)):
            return _bad(404, "unknown key")
        if "enabled" in d:
            store.x("UPDATE keys SET enabled=? WHERE id=?", (int(bool(d["enabled"])), kid))
        return jsonify({"ok": True})

    @app.delete("/admin/api/keys/<int:kid>")
    @admin
    def del_key(kid):
        store.x("DELETE FROM keys WHERE id=?", (kid,))
        return jsonify({"ok": True})

    @app.post("/admin/api/test/<name>")
    @admin
    def test_conn(name):
        """Connectivity/auth test: GET /models. Does not touch chat quotas (a /models call is not a completion)."""
        if not registry.provider(name):
            return _bad(404, "unknown provider")
        try:
            found = discover_models(router, name)
            return jsonify({"ok": True, "models_reported": len(found), "sample": [m["id"] for m in found[:10]]})
        except Exception as e:
            return jsonify({"ok": False, "error": redact(str(e))[:300]}), 200

    @app.post("/admin/api/probe/<name>")
    @admin
    def probe(name):
        """Costs real requests (<=3 per model). Triggered manually only."""
        p = registry.effective(name)
        if not p:
            return _bad(404, "unknown provider")
        only = (request.get_json(silent=True) or {}).get("model")
        out = {}
        for mc in p["models"]:
            if only and mc["id"] != only:
                continue
            health.note_attempt(name, mc["id"])
            out[mc["id"]] = {c: {"verified": v, "detail": d} for c, (v, d) in probe_model(router, name, mc).items()}
        return jsonify(out)

    @app.post("/admin/api/backup")
    @admin
    def backup():
        return jsonify({"file": os.path.basename(store.backup(backup_dir or os.path.dirname(db_path) + "/backups"))})

    return app
