# OC-Gateway

An OpenAI-compatible cloud gateway for [OpenCode](https://opencode.ai/). OC-Gateway forwards requests to configured cloud model providers and can route around rate limits, exhausted quotas, provider outages, and unavailable models.

The gateway is built with Flask and SQLite. It runs on your server; your local machine runs OpenCode and connects to the gateway. **Model inference is not performed locally.** Supported providers and model availability depend on their current APIs and account limits.

## What it does

- Routes requests across configured OpenAI-compatible providers, such as OpenRouter, Groq, and NVIDIA NIM.
- Offers `priority`, `failover`, and `balanced` routing strategies.
- Tracks provider health and applies cooldowns after rate limits, quota errors, timeouts, and outages.
- Lets you test provider connections and probe model capabilities. Tool-calling requests are only sent to models whose tool capability has been verified.
- Avoids switching providers after a streaming response has started. If a stream is interrupted, it reports the interruption instead of combining partial output from different models.
- Stores provider credentials encrypted in SQLite and exposes a dashboard for managing providers and routing.
- Includes utilities to preserve project state, create Git-based checkpoints, and prepare task-level recovery instructions.

## Requirements

- A Linux server or VM with Docker Engine and the Docker Compose plugin.
- A domain name pointing to the server, with inbound ports 80 and 443 allowed, for Caddy-managed HTTPS.
- Python 3 on the machine where you generate keys and use the project utilities.
- API keys for one or more compatible providers. Free-tier access, model names, quotas, and terms can change; check each provider before use.

## Quick start

### 1. Generate gateway credentials

From the repository, run this on a trusted machine:

```bash
python3 tools/genkeys.py
```

The script prints values for the server `.env`, a client key for OpenCode, and an admin token for the dashboard. **Save the client key and admin token in a password manager immediately; they are displayed only once.** Keep the generated `GATEWAY_MASTER_KEY` safe as well: without it, encrypted provider credentials in the database cannot be decrypted.

### 2. Configure the server

On the server, create a private `.env` file using `.env.example` as the template. Add the generated values and set the domain and any other required deployment settings described in `docs/DEPLOYMENT.md`. Never commit `.env` or share it publicly.

Edit `config/providers.yaml` to configure the provider endpoints, model IDs, aliases, and routing. The model IDs shipped in the file are examples and may no longer be available. Check the provider's current model list and replace them with valid IDs before relying on them.

### 3. Start the services

From the directory containing `docker-compose.yml`, run:

```bash
docker compose up -d --build
docker compose ps
```

The gateway's port is bound to the server's loopback interface. The public HTTPS endpoint is served through Caddy; the dashboard is intended to be accessed through an SSH tunnel or an explicitly allowed admin IP.

### 4. Open the dashboard and verify providers

From your local machine, open an SSH tunnel (replace `user` and `server` with your SSH account and hostname):

```bash
ssh -L 8080:127.0.0.1:8080 user@server
```

While the tunnel is running, visit <http://127.0.0.1:8080/dashboard>. Sign in with the generated admin token, add provider API keys, and run **Test connection** followed by **Probe capabilities**. A successful connection alone does not verify tool-calling support; probe models before using them for tool-enabled OpenCode requests.

### 5. Connect OpenCode

In the OpenCode project directory, initialize the project-state files:

```bash
python3 /path/to/oc-gateway/tools/ocstate.py init
```

Copy the repository's `opencode.json` into your project, replace `YOUR-DOMAIN` with your gateway domain, and make the generated client key available to OpenCode:

```bash
export GATEWAY_KEY='your-client-key'
opencode
```

The provided configuration selects the `gateway/coder` and `gateway/fast` aliases and asks for approval before tool actions. Keep the key private; do not put it in a committed project file.

## Everyday project-state commands

Run these from the OpenCode project's Git working tree. Use the script path appropriate to where you checked out this repository.

| Command | Purpose |
| --- | --- |
| `python3 /path/to/oc-gateway/tools/ocstate.py init` | Create `PROJECT_STATE.md` and `AGENTS.md` from templates; existing files are left untouched. |
| `python3 /path/to/oc-gateway/tools/ocstate.py checkpoint "before refactor"` | Save a snapshot under `refs/gateway-checkpoints/` without changing your branch or Git index. |
| `python3 /path/to/oc-gateway/tools/ocstate.py checkpoints` | List saved checkpoints. |
| `python3 /path/to/oc-gateway/tools/ocstate.py diff` | Show changes since the latest checkpoint; pass a checkpoint reference to compare against another one. |
| `python3 /path/to/oc-gateway/tools/ocstate.py restore REF --yes` | Restore files from a checkpoint. A safety checkpoint is created first; files added after the checkpoint are not deleted. |
| `python3 /path/to/oc-gateway/tools/ocstate.py resume` | Print task-level recovery guidance with Git status and project state after an interruption. |
| `python3 /path/to/oc-gateway/tools/ocstate.py lint` | Check `PROJECT_STATE.md` for strings that look like secrets. This is a heuristic, not a guarantee. |

## Operations and testing

View service status and logs:

```bash
docker compose ps
docker compose logs -f gateway
```

Check the public health endpoint, replacing the hostname:

```bash
curl https://YOUR-DOMAIN/health
```

Run the test suite and generate `TEST_REPORT.md`:

```bash
python3 tests/report.py
```

The tests use local provider fakes; they do not consume real provider quota. Review `TEST_REPORT.md` for coverage limitations. In particular, passing tests do not by themselves verify real provider APIs, Docker deployment, Caddy certificate issuance, or a live OpenCode session.

For deployment, backups, updates, and recovery procedures, see [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). For security assumptions and residual risks, see [`docs/SECURITY.md`](docs/SECURITY.md).

## Important security and reliability notes

- Prompts, conversation history, and code sent through the gateway are sent to the selected third-party provider. Review that provider's data-use and retention terms before sending sensitive or proprietary information.
- The gateway's master key and decrypted provider credentials are available to the server process at runtime. Secure the server and `.env`, and keep off-server database backups protected.
- Free-tier quotas and model catalogs change frequently. Gateway quota estimates only account for requests made through this gateway; provider-reported quota headers are used when available.
- Once streamed output has begun, the gateway does not switch models mid-response. An interrupted generation must be resumed as a task, not continued token by token.
- Do not use additional accounts or keys to evade provider limits or terms of service.

## Documentation

- [Architecture and routing behavior](docs/ARCHITECTURE.md)
- [Deployment and maintenance](docs/DEPLOYMENT.md)
- [Security notes](docs/SECURITY.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Test report](TEST_REPORT.md)
