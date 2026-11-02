"""Merges static YAML config with providers/aliases added at runtime from the dashboard."""
import copy
from .config import DEFAULTS


class Registry:
    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store

    # -- providers ----------------------------------------------------
    def providers(self):
        out = copy.deepcopy(self.cfg["providers"])
        for name, p in (self.store.kv_get("dynamic_providers", {}) or {}).items():
            out[name] = p
        return out

    def provider(self, name):
        return self.providers().get(name)

    def effective(self, name):
        """Provider config with dashboard overrides (enabled / priority) applied."""
        p = self.provider(name)
        if p is None:
            return None
        p = copy.deepcopy(p)
        st = self.store.q1("SELECT enabled, priority FROM pstate WHERE provider=?", (name,))
        if st:
            if st["enabled"] is not None:
                p["enabled"] = bool(st["enabled"])
            if st["priority"] is not None:
                p["priority"] = st["priority"]
        return p

    def add_provider(self, name, spec):
        dyn = self.store.kv_get("dynamic_providers", {}) or {}
        spec = dict(spec)
        spec.setdefault("enabled", True)
        spec.setdefault("priority", 100)
        spec.setdefault("auth", "bearer")
        spec.setdefault("limits", {})
        spec.setdefault("cooldowns", {})
        spec.setdefault("extra_headers", {})
        spec.setdefault("quota_reset_utc", "00:00")
        for m in spec.get("models", []):
            m.setdefault("quality", 5); m.setdefault("speed", 5); m.setdefault("context", 0)
            m.setdefault("max_output", 0); m.setdefault("claims", ["chat", "stream"])
            m.setdefault("price_in", 0.0); m.setdefault("price_out", 0.0)
        spec.setdefault("models", [])
        dyn[name] = spec
        self.store.kv_set("dynamic_providers", dyn)

    # -- aliases ------------------------------------------------------
    def aliases(self):
        out = {k: copy.deepcopy(v) for k, v in self.cfg["aliases"].items()}
        for alias, members in (self.store.kv_get("dynamic_alias_members", {}) or {}).items():
            out.setdefault(alias, {"members": []})["members"].extend(members)
        return out

    def members(self, alias):
        """-> list of (provider_name, model_cfg, alias_position)"""
        a = self.aliases().get(alias)
        if not a:
            return None
        provs = self.providers()
        res = []
        for i, m in enumerate(a.get("members", [])):
            if isinstance(m, str):
                pn, _, mid = m.partition("::")
            else:
                pn, mid = m["provider"], m["model"]
            p = provs.get(pn)
            if not p:
                continue
            mc = next((x for x in p.get("models", []) if x["id"] == mid), None)
            if mc:
                res.append((pn, mc, i))
        return res

    def strategy(self):
        return self.store.kv_get("strategy") or self.cfg["routing"]["default_strategy"]
