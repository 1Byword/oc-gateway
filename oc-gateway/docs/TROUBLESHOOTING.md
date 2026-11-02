# Troubleshooting

| Symptom | Likely cause | Check / fix |
|---|---|---|
| 401 `invalid_api_key` from the gateway | wrong client key, or hash not in `GATEWAY_KEY_HASHES` | `echo -n KEY | sha256sum` must equal a hash in `.env`; restart compose after editing |
| 429 `auth_throttled` | too many bad attempts from your IP | wait 60 s |
| 503 `no_provider_available` + "not verified" | no model has verified tool-calling | dashboard -> Probe capabilities (costs up to 3 requests/model) |
| 503 with attempts listing `cooldown:*` | every provider is cooling down (429/quota/outage) | read `Retry-After`; dashboard shows reason and remaining seconds |
| Provider shows `auth_failed` / key "invalid" | wrong/expired key | dashboard -> Save / replace key (re-activates it) |
| Probe says tools "failed" | model did not return a valid tool call | use another model for `coder`; do not force-enable tool support |
| `stream_interrupted` in OpenCode | provider dropped the stream after output began | re-send, or `ocstate.py resume`; the gateway will not splice models |
| Startup error "GATEWAY_MASTER_KEY is not set" | missing `.env` value | run `tools/genkeys.py` |
| "cannot decrypt stored key" | master key changed | restore the old master key, or re-add keys |
| Dashboard 403 | Caddy blocks your IP | use the SSH tunnel or add your CIDR to `ADMIN_ALLOW_CIDRS` |
| Model 404 / "model_unavailable" | free model id was rotated out | `Test connection` to list ids, update `providers.yaml`, Probe again |
| Slow first response | provider cold start or sticky failover on a slower backup | change strategy or wait for `failback_seconds` |
Logs: `docker compose logs -f gateway` (secrets are redacted). Health: `curl https://YOUR-DOMAIN/health`.
