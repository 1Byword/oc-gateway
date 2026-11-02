"""Capability probes. A capability is recorded as verified ONLY after a real request demonstrated it."""
import json
import time

import requests

from .guard import validate_completion

TOOL = {"type": "function", "function": {"name": "get_weather", "description": "Get weather for a city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}


def _post(router, pcfg, mc, key, body, stream=False):
    url = pcfg["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if key["key"]:
        headers["Authorization"] = "Bearer " + key["key"]
    headers.update(pcfg.get("extra_headers", {}))
    body = dict(body, model=mc["id"], stream=stream)
    return router.http.post(url, json=body, headers=headers, timeout=router._timeouts(), stream=stream)


def probe_model(router, provider, mc, caps=("chat", "stream", "tools")):
    """Run real (tiny) requests. Returns {cap: (verified: bool, detail)} and stores results."""
    pcfg = router.registry.effective(provider)
    key = router.health.pick_key(provider)
    results = {}
    if key is None:
        return {c: (False, "no active key") for c in caps}
    base = {"max_tokens": 64, "messages": [{"role": "user", "content": "Reply with the single word: OK"}]}

    def chat():
        r = _post(router, pcfg, mc, key, base)
        if r.status_code != 200:
            return False, "HTTP %s" % r.status_code
        p = validate_completion(r.json())
        return (p is None), (p or "ok")

    def stream():
        r = _post(router, pcfg, mc, key, base, stream=True)
        if r.status_code != 200:
            r.close(); return False, "HTTP %s" % r.status_code
        got, done = False, False
        for line in r.iter_lines():
            if line.startswith(b"data:"):
                d = line[5:].strip()
                if d == b"[DONE]":
                    done = True
                else:
                    try:
                        got = got or bool(json.loads(d).get("choices"))
                    except ValueError:
                        pass
        r.close()
        return (got and done), ("ok" if got and done else "no valid SSE chunks / no [DONE]")

    def tools():
        body = {"max_tokens": 128, "tools": [TOOL],
                "messages": [{"role": "user", "content": "What is the weather in Paris? You must call the get_weather function."}]}
        r = _post(router, pcfg, mc, key, body)
        if r.status_code != 200:
            return False, "HTTP %s" % r.status_code
        data = r.json()
        for ch in data.get("choices", []):
            for tc in (ch.get("message") or {}).get("tool_calls") or []:
                try:
                    if tc["function"]["name"] == "get_weather" and "city" in json.loads(tc["function"]["arguments"]):
                        return True, "valid tool call returned"
                except (ValueError, KeyError, TypeError):
                    return False, "tool call malformed"
        return False, "model did not return a tool call (not demonstrated)"

    fns = {"chat": chat, "stream": stream, "tools": tools}
    for cap in caps:
        try:
            ok, detail = fns[cap]()
        except requests.exceptions.RequestException as e:
            ok, detail = False, "network error: %s" % type(e).__name__
        except Exception as e:                       # malformed provider output must not crash the probe
            ok, detail = False, "error: %s" % type(e).__name__
        results[cap] = (bool(ok), detail)
        router.store.x("INSERT OR REPLACE INTO caps(provider,model,cap,verified,checked,detail) VALUES(?,?,?,?,?,?)",
                       (provider, mc["id"], cap, int(bool(ok)), router.clock(), detail))
        if cap == "chat" and not ok:
            for rest in caps:
                if rest != "chat" and rest not in results:
                    results[rest] = (False, "skipped: chat failed")
            break
    return results


def discover_models(router, provider):
    """GET {base_url}/models. Returns [{id, context_length?}] reported by the provider (metadata, not verification)."""
    pcfg = router.registry.effective(provider)
    key = router.health.pick_key(provider)
    if key is None:
        raise RuntimeError("no active key")
    headers = {"Authorization": "Bearer " + key["key"]} if key["key"] else {}
    r = router.http.get(pcfg["base_url"].rstrip("/") + "/models", headers=headers, timeout=router._timeouts())
    r.raise_for_status()
    out = []
    for m in r.json().get("data", []):
        out.append({"id": m.get("id"), "context_length": m.get("context_length") or m.get("context_window") or m.get("max_model_len")})
    return out
