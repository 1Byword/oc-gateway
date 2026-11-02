# Architecture and decisions

```
OpenCode (Kali WSL2)  --HTTPS, Bearer GATEWAY_KEY-->  Caddy (TLS)  -->  Gateway (Flask/gunicorn, 1 process)
                                                                         |-- auth + rate limit + validation
                                                                         |-- Router (priority | balanced | failover)
                                                                         |-- Health (cooldowns, key state, quota info)
                                                                         |-- SQLite (encrypted keys, state, usage, journal)
                                                                         `--> OpenRouter | Groq | NVIDIA | any OpenAI-compatible provider
```

## Why not LiteLLM?
The brief said to use LiteLLM unless something is technically superior. I could not install or run LiteLLM in the build
environment (no network), so I could not verify any claim about it. I built a small custom gateway instead because the
requirements that matter most here are easiest to guarantee in code that is fully tested: capability gating (never route tool calls
to an unverified model), a hard rule against switching models mid-stream, quota-vs-rate-limit distinction with per-kind cooldowns,
verified-vs-estimated usage, and the dashboard. LiteLLM is still a reasonable alternative if you prefer a large community project;
you would then have to re-create the capability probing and the journal yourself. Nothing here prevents putting LiteLLM behind/in place of the router later.

## Request lifecycle and safety rules
1. Authenticate (hash compare), throttle, validate JSON shape.
2. Resolve alias (`coder`, `fast`) -> candidate models; drop any that are disabled, in cooldown, over estimated/verified limits,
   lacking a **verified** capability needed by this request, or whose context window is too small.
3. Order by strategy. **priority**: configured order, fail back immediately. **failover**: sticky to the current backup until it fails
   or `failback_seconds` pass. **balanced**: weighted quality / observed latency / observed success rate.
4. Call the provider with the provider's own key (decrypted in memory only). On failure classify it:
   429 -> rate_limit (cooldown >= Retry-After) or quota (hints like "quota/daily", or Retry-After > 15 min); 401/403 -> key marked invalid/expired
   and never retried; 404 -> model cooldown; 5xx/timeout/connection -> exponential backoff; 400 -> returned to the client, **not** retried;
   context-length -> next model with a bigger window, no penalty.
5. Retry safety: tools execute in OpenCode, never at the provider, so re-sending a *failed* LLM request cannot duplicate a tool run.
   Streaming is committed at the first byte: after that the gateway never switches model; a break produces an explicit
   `stream_interrupted` error event and a journal entry.
6. When everything is unavailable the client gets 503 + `Retry-After` and the reasons (no secrets).

## Capability verification
Configured `claims` are only claims. `/admin/api/probe/<provider>` sends real tiny requests (chat, stream, tool call) and stores
verified/failed. Tool-bearing requests only go to models with a *verified* tool capability (`require_verified_tools`). `/v1/models`
lists only verified capabilities. Context length and max output are **declared** values (config or provider metadata), shown as such;
they are not verified by probing.

## Quota honesty
* **Verified**: provider response headers (`x-ratelimit-remaining-*`), when present.
* **Estimated**: the gateway's own request counts vs the documented limits in config (other clients using the same key are invisible to it).
Token counts are provider-reported when available, otherwise a chars/3.5 estimate flagged as estimated. Cost uses `price_in/price_out` per 1M tokens (default 0).
Multiple keys per provider are supported for *distinct legitimate purposes*; quota and rate-limit cooldowns are applied per provider, on purpose,
so extra keys cannot be used to hammer a provider that said stop. Do not create extra accounts to dodge limits: it breaks provider terms.

## Session and context preservation (what is and is not possible)
* OpenCode keeps the session and sends the **full message history on every request**; the gateway forwards it unchanged, so a provider switch
  does not create a new session and the new model sees the whole history (tested byte-for-byte). Models do not share hidden state.
* `PROJECT_STATE.md` + `AGENTS.md` give any model the project objective, tasks, decisions, errors, tests and next actions.
* `ocstate.py checkpoint` stores a git snapshot under `refs/gateway-checkpoints/` without touching your branch or index.
* Context limits: the gateway estimates prompt size and skips models whose window is too small. If nothing fits it returns an explicit error
  (OpenCode then compacts its own session). Optional gateway compaction (`context.compaction: true`) summarizes the oldest messages with
  a model, keeps the system message and recent messages (tool calls stay paired with results), flags the response with
  `X-Gateway-Context-Compacted`, and warns inside the summary if the summarizer input had to be truncated. It is a safety net; summaries are model-written
  and can be wrong, which is why it is off by default.
* **Recovery**: an interrupted generation cannot be resumed token-by-token (providers do not support it, and splicing two models is unsafe).
  Recovery is task-level: `ocstate.py resume` + the journal (`interrupted` entries in the dashboard).

## Anti-hallucination: what is enforced where
Enforced by code: capability gating, JSON validity of non-streamed tool calls (bad ones fail over), secrets never in logs/dashboard, no silent compaction.
Enforced by OpenCode: `permission: {"*": "ask"}` makes you approve edits and shell commands (this is your destructive-operation guard).
Enforced only by instructions (AGENTS.md): inspect before edit, verify paths, run tests, label findings confirmed/hypothesis, do not trust another model's
unverified claims. Models can still ignore instructions; the gateway cannot read your filesystem or verify their claims.
