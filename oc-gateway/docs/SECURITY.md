# Security notes

| Area | What is implemented | Where |
|---|---|---|
| Transport | HTTPS via Caddy (automatic Let's Encrypt), HSTS | deploy/Caddyfile |
| Client auth | Bearer key, only its SHA-256 is stored on the server; constant-time compare | server.py |
| Admin auth | Separate admin token (hash), separate from client keys; admin/dashboard blocked publicly by Caddy except allow-listed IPs; recommended access = SSH tunnel | server.py, Caddyfile |
| Brute force | per-IP failed-auth throttle (429), per-client rate limit | server.py |
| Provider keys | Fernet-encrypted in SQLite with `GATEWAY_MASTER_KEY` (env only); never returned by any API (last 4 chars only); never sent to OpenCode; log filter redacts key-like strings | secrets_box.py, util.py |
| Input validation | JSON shape, field types, body size limit (12 MB app + Caddy) | server.py |
| Dashboard | static files, strict CSP (`default-src 'self'`), no innerHTML (tested), `no-store`, frame denial | static/, server.py |
| Containers | non-root user, read-only root fs, all capabilities dropped, no-new-privileges, memory limit, loopback-only published port | Dockerfile, docker-compose.yml |
| Repo hygiene | `.env`/db ignored; a test scans repo files for key-like strings | .gitignore, tests |
| LiteLLM UI | not used, so there is no management UI to expose |  |

Residual risks (honest list): the master key and the decrypted provider keys live in the server process memory and env, so anyone with root on the VM can obtain them;
the in-process rate limits reset on restart; the secret-redaction regex covers common key formats only; prompts and code you send are visible to the chosen free providers
(free tiers often allow training on your data: read each provider's terms before sending proprietary or sensitive code).
Dependencies: pinned by major version in `gateway/requirements.txt`; rebuild monthly (`docker compose build --pull`).
