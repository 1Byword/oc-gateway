"""Provider health, cooldowns, local rate estimates and verified quota information."""
import collections
import json
import threading
import time

from .util import parse_duration


def _day(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def next_reset(now, hhmm):
    """Seconds until the next daily reset at HH:MM UTC."""
    h, m = [int(x) for x in str(hhmm).split(":")]
    t = time.gmtime(now)
    today_reset = now - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec) + h * 3600 + m * 60
    if today_reset <= now:
        today_reset += 86400
    return today_reset - now


class Health:
    def __init__(self, store, registry, secrets, clock=time.time):
        self.store, self.registry, self.secrets, self.clock = store, registry, secrets, clock
        self._recent = collections.defaultdict(collections.deque)   # provider -> request timestamps (rpm estimate)
        self._lock = threading.RLock()

    # ---------------- state helpers ----------------
    def _ensure(self, provider):
        self.store.x("INSERT OR IGNORE INTO pstate(provider) VALUES(?)", (provider,))

    def state(self, provider):
        self._ensure(provider)
        return self.store.q1("SELECT * FROM pstate WHERE provider=?", (provider,))

    def cooldown_seconds(self, provider, kind):
        rc = self.registry.cfg["routing"]["cooldowns"]
        pc = (self.registry.provider(provider) or {}).get("cooldowns", {})
        st = self.state(provider)
        override = json.loads(st["cooldown_cfg"]) if st.get("cooldown_cfg") else {}
        for src in (override, pc, rc):
            if kind in src:
                return float(src[kind])
        return 60.0

    # ---------------- keys ----------------
    def active_keys(self, provider):
        return self.store.q("SELECT * FROM keys WHERE provider=? AND enabled=1 AND status='active' ORDER BY last_used ASC", (provider,))

    def pick_key(self, provider, skip_ids=()):
        p = self.registry.provider(provider) or {}
        if p.get("auth") == "none":
            return {"id": None, "key": ""}
        for k in self.active_keys(provider):
            if k["id"] in skip_ids:
                continue
            self.store.x("UPDATE keys SET last_used=? WHERE id=?", (self.clock(), k["id"]))
            return {"id": k["id"], "key": self.secrets.decrypt(k["ciphertext"])}
        return None

    # ---------------- availability ----------------
    def availability(self, provider):
        """-> (available: bool, reason: str). Never raises."""
        p = self.registry.effective(provider)
        if not p:
            return False, "unknown_provider"
        if not p.get("enabled", True):
            return False, "disabled"
        if p.get("auth") != "none" and not self.active_keys(provider):
            return False, "no_active_key"
        now = self.clock()
        st = self.state(provider)
        if st["cooldown_until"] and st["cooldown_until"] > now:
            return False, "cooldown:%s" % (st["last_error_kind"] or "unknown")
        q = json.loads(st["quota_json"]) if st.get("quota_json") else None
        if q and q.get("remaining_requests") == 0 and q.get("reset_at", 0) > now:
            return False, "verified_quota_exhausted"
        lim = p.get("limits", {})
        if lim.get("rpm"):
            dq = self._recent[provider]
            while dq and dq[0] < now - 60:
                dq.popleft()
            if len(dq) >= lim["rpm"]:
                return False, "estimated_rpm_limit"
        if lim.get("rpd"):
            row = self.store.q1("SELECT COALESCE(SUM(requests),0) n FROM usage WHERE provider=? AND day=?", (provider, _day(now)))
            if row["n"] >= lim["rpd"]:
                return False, "estimated_rpd_limit"
        return True, "ok"

    def model_available(self, provider, model):
        until = self.store.kv_get("model_cd:%s::%s" % (provider, model), 0)
        return until <= self.clock()

    def public_state(self, provider):
        st = self.state(provider)
        ok, reason = self.availability(provider)
        now = self.clock()
        return {
            "available": ok, "reason": reason, "state": st["state"] if not ok or st["state"] != "healthy" else "healthy",
            "cooldown_remaining_s": max(0, int(st["cooldown_until"] - now)) if st["cooldown_until"] else 0,
            "consecutive_failures": st["consec_failures"], "last_success": st["last_success"],
            "last_failure": st["last_failure"], "last_error_kind": st["last_error_kind"], "last_error": st["last_error"],
            "quota_verified": json.loads(st["quota_json"]) if st.get("quota_json") else None,
        }

    # ---------------- recording ----------------
    def note_attempt(self, provider, model):
        now = self.clock()
        self._recent[provider].append(now)
        self.store.x(
            "INSERT INTO usage(provider,model,day,requests) VALUES(?,?,?,1) "
            "ON CONFLICT(provider,model,day) DO UPDATE SET requests=requests+1", (provider, model, _day(now)))

    def record_success(self, provider, model, latency_ms, usage, headers, price):
        now = self.clock()
        self._ensure(provider)
        self.store.x("UPDATE pstate SET state='healthy', consec_failures=0, cooldown_until=0, last_success=? WHERE provider=?", (now, provider))
        pt, ct, est = usage
        cost = (pt * price[0] + ct * price[1]) / 1e6
        self.store.x(
            "UPDATE usage SET ok=ok+1, prompt_tokens=prompt_tokens+?, completion_tokens=completion_tokens+?, "
            "tokens_estimated=tokens_estimated+?, cost=cost+? WHERE provider=? AND model=? AND day=?",
            (pt, ct, (pt + ct) if est else 0, cost, provider, model, _day(now)))
        self.store.x("INSERT OR IGNORE INTO mstats(provider,model) VALUES(?,?)", (provider, model))
        self.store.x("UPDATE mstats SET ok=ok+1, ewma_ms=CASE WHEN ewma_ms IS NULL THEN ? ELSE ewma_ms*0.8+?*0.2 END "
                     "WHERE provider=? AND model=?", (latency_ms, latency_ms, provider, model))
        self._parse_quota_headers(provider, headers or {}, now)

    def _parse_quota_headers(self, provider, headers, now):
        h = {k.lower(): v for k, v in headers.items()}
        rem = h.get("x-ratelimit-remaining-requests")
        remt = h.get("x-ratelimit-remaining-tokens")
        if rem is None and remt is None:
            return
        info = {"source": "response_headers (verified by provider)", "observed_at": now}
        try:
            if rem is not None:
                info["remaining_requests"] = int(float(rem))
            if remt is not None:
                info["remaining_tokens"] = int(float(remt))
        except ValueError:
            return
        reset = parse_duration(h.get("x-ratelimit-reset-requests"))
        if reset is not None:
            info["reset_at"] = now + reset
        self._ensure(provider)
        self.store.x("UPDATE pstate SET quota_json=? WHERE provider=?", (json.dumps(info), provider))

    def record_failure(self, provider, model, kind, detail="", retry_after=None, key_id=None):
        """kind: rate_limit | quota | auth | server | timeout | connection | model_unavailable | bad_output"""
        now = self.clock()
        self._ensure(provider)
        st = self.state(provider)
        day = _day(now)
        self.store.x(
            "INSERT INTO usage(provider,model,day) VALUES(?,?,?) ON CONFLICT DO NOTHING", (provider, model, day))
        col = {"rate_limit": "rate_limited", "quota": "quota_events"}.get(kind, "errors")
        self.store.x("UPDATE usage SET %s=%s+1 WHERE provider=? AND model=? AND day=?" % (col, col), (provider, model, day))
        self.store.x("INSERT OR IGNORE INTO mstats(provider,model) VALUES(?,?)", (provider, model))

        if kind == "auth":
            if key_id is not None:
                reason = "expired" if "expired" in detail.lower() else "invalid_or_unauthorized"
                self.store.x("UPDATE keys SET status='invalid', reason=? WHERE id=?", (reason, key_id))
                self.store.event(now, provider, "key_" + reason, "key id %s disabled" % key_id)
            self.store.x("UPDATE mstats SET fail=fail+1 WHERE provider=? AND model=?", (provider, model))
            self.store.x("UPDATE pstate SET state='auth_failed', last_failure=?, last_error_kind=?, last_error=? WHERE provider=?",
                         (now, kind, detail[:300], provider))
            return 0
        if kind == "model_unavailable":
            cd = self.cooldown_seconds(provider, "model_unavailable")
            self.store.kv_set("model_cd:%s::%s" % (provider, model), now + cd)
            self.store.event(now, provider, "model_unavailable", "%s: %s" % (model, detail))
            return cd

        consec = st["consec_failures"] + 1
        base = self.cooldown_seconds(provider, kind)
        if kind in ("server", "timeout", "connection", "bad_output"):
            mult = min(2 ** (consec - 1), self.cooldown_seconds(provider, "max_backoff_multiplier") or 8)
            cd = base * mult
        elif kind == "quota":
            reset = next_reset(now, (self.registry.provider(provider) or {}).get("quota_reset_utc", "00:00"))
            cd = max(retry_after or 0, base, 0)
            # quota windows documented as daily reset: wait for the documented reset rather than polling
            if retry_after is None and (self.registry.provider(provider) or {}).get("quota_resets_daily", False):
                cd = max(cd, reset)
        else:  # rate_limit
            cd = max(retry_after or 0, base)
        state = {"rate_limit": "rate_limited", "quota": "quota_exhausted"}.get(kind, "unhealthy")
        self.store.x(
            "UPDATE pstate SET state=?, consec_failures=?, cooldown_until=?, last_failure=?, last_error_kind=?, last_error=? WHERE provider=?",
            (state, consec, now + cd, now, kind, detail[:300], provider))
        self.store.x("UPDATE mstats SET fail=fail+1 WHERE provider=? AND model=?", (provider, model))
        self.store.event(now, provider, kind, "%s (cooldown %ds)" % (detail[:200], cd))
        return cd

    # ---------------- stats for routing ----------------
    def model_stats(self, provider, model):
        r = self.store.q1("SELECT ok, fail, ewma_ms FROM mstats WHERE provider=? AND model=?", (provider, model))
        return r or {"ok": 0, "fail": 0, "ewma_ms": None}
