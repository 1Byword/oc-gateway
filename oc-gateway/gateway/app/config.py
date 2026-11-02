import copy
import os
import yaml

DEFAULTS = {
    "routing": {
        "default_strategy": "failover",          # priority | balanced | failover
        "balanced_weights": {"quality": 0.5, "speed": 0.2, "reliability": 0.3},
        "failback_seconds": 900,                 # failover strategy: return to primary after this long
        "max_attempts": 4,                       # providers tried per request
        "connect_timeout": 10,
        "read_timeout": 120,
        "require_verified_tools": True,          # route tool-using requests only to probe-verified models
        "require_verified_streaming": False,
        "cooldowns": {                           # seconds; overridable per provider
            "rate_limit": 60, "quota": 3600, "server": 120, "timeout": 60, "connection": 120,
            "model_unavailable": 3600, "max_backoff_multiplier": 8,
        },
    },
    "context": {
        "compaction": False,        # OpenCode compacts its own sessions; enable only if you want a gateway safety net
        "safety_margin": 0.90,      # use at most this fraction of a model's context window
        "keep_last_messages": 8,
        "summarizer_alias": None,
    },
    "security": {"client_rpm": 120, "max_body_bytes": 12_000_000, "auth_fail_per_min": 10},
    "providers": {},
    "aliases": {},
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(source):
    """source: path to YAML or an already-built dict."""
    if isinstance(source, dict):
        raw = source
    else:
        with open(source) as f:
            raw = yaml.safe_load(f) or {}
    cfg = _merge(DEFAULTS, raw)
    for name, p in cfg["providers"].items():
        p.setdefault("enabled", True)
        p.setdefault("priority", 100)
        p.setdefault("auth", "bearer")
        p.setdefault("limits", {})
        p.setdefault("models", [])
        p.setdefault("quota_reset_utc", "00:00")
        p.setdefault("cooldowns", {})
        p.setdefault("extra_headers", {})
        for m in p["models"]:
            m.setdefault("quality", 5)
            m.setdefault("speed", 5)
            m.setdefault("context", 0)       # 0 = unknown
            m.setdefault("max_output", 0)
            m.setdefault("claims", ["chat", "stream"])
            m.setdefault("price_in", 0.0)
            m.setdefault("price_out", 0.0)
    return cfg


def env_keys_for(cfg, environ=os.environ):
    """Provider keys supplied through environment variables (api_key_env / api_keys_env)."""
    out = {}
    for name, p in cfg["providers"].items():
        keys = []
        for var in ([p["api_key_env"]] if p.get("api_key_env") else []) + list(p.get("api_keys_env", [])):
            v = environ.get(var)
            if v:
                keys.append((var, v.strip()))
        if keys:
            out[name] = keys
    return out
