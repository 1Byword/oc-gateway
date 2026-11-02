# OC-Gateway

[OpenCode](https://opencode.ai/) cloud gateway with an OpenAI-compatible API, health-aware routing, provider failover, and project-state utilities.

OC-Gateway sits between OpenCode and one or more OpenAI-compatible model providers. It centralizes provider credentials and routing on a server, while the OpenCode client runs on your workstation. The gateway forwards prompts to cloud providers; **it does not run models locally**.

> **Deployment status:** This repository provides a single-instance gateway and deployment configuration. It is not a managed service, and its current architecture is not a multi-instance high-availability deployment. Review the security, testing, and operational limitations below before using it with sensitive data or relying on it for production workloads.

## Contents

- [Project overview](#project-overview)
- [Capabilities](#capabilities)
- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Requirements](#requirements)
- [Deploy the gateway](#deploy-the-gateway)
- [Configure providers and routing](#configure-providers-and-routing)
- [Connect OpenCode](#connect-opencode)
- [Operate and maintain](#operate-and-maintain)
- [Project-state utilities](#project-state-utilities)
- [Security and privacy](#security-and-privacy)
- [Testing and limitations](#testing-and-limitations)
- [Troubleshooting](#troubleshooting)
- [Documentation](#documentation)

## Project overview

The gateway is a Flask application served by Gunicorn, with SQLite for persistent state. Caddy provides public HTTPS termination and reverse-proxies requests to the gateway. Provider APIs remain responsible for model inference.

Use OC-Gateway when you want to:

- Give OpenCode one OpenAI-compatible endpoint instead of configuring each provider directly.
- Define model aliases that can resolve to several providers.
- Route according to priority, failover, or a balanced strategy.
- Track provider health and avoid models that are unavailable, cooling down, over configured quotas, or not verified for required capabilities.
- Manage provider keys and inspect gateway state through an admin dashboard.
- Keep project notes and Git checkpoints that help recover work after an interrupted model request.

OC-Gateway does not guarantee that a free provider is always available, that provider limits are exact, or that a third-party model will return correct answers. Free-tier plans and model catalogs change frequently.

## Capabilities

### Routing and provider health

- Configure providers that implement the OpenAI-compatible API shape used by the gateway. The checked-in configuration includes example entries for OpenRouter, Groq, and NVIDIA NIM.
- Choose among `priority`, `failover`, and `balanced` routing strategies. The default is configured in `config/providers.yaml`; the dashboard can change the active strategy.
- Apply cooldowns after rate limits, quota exhaustion, provider errors, timeouts, and connection failures.
- Distinguish provider-reported quota headers from estimates based on configured limits and requests observed by this gateway. Requests made outside the gateway are not included in its estimates.
- Return an unavailable response with retry guidance when no candidate model can serve a request.

### Capability checks and streaming safety

- Test provider connectivity and retrieve current model IDs from the dashboard.
- Probe model chat, streaming, and tool-calling behavior. Configuration claims are not treated as proof; a capability probe makes a real provider request and may consume provider quota.
- Require verified tool-calling capability for tool-bearing requests by default.
- Do not switch providers after a streamed response has started. If the upstream stream breaks after output begins, the gateway reports an interruption rather than splicing output from another model.

### Credentials, dashboard, and persistence

- Keep client, admin, and provider credentials separate.
- Store client/admin key hashes rather than their raw values. Provider credentials added through the dashboard are encrypted in SQLite using `GATEWAY_MASTER_KEY`.
- Persist gateway data in a Docker volume backed by SQLite. The deployment uses one gateway process because routing state such as sticky failover is held in process memory.
- Restrict dashboard/admin access at the reverse proxy; the recommended administrative access method is an SSH tunnel.

### OpenCode project continuity

The `tools/ocstate.py` utility initializes `PROJECT_STATE.md` and `AGENTS.md`, runs a heuristic secret scan on project state, creates Git checkpoints without changing the current branch or index, compares/restores checkpoints, and prints task-level recovery guidance. It cannot resume generated tokens or prove that an interrupted action completed.

## Architecture

```text
OpenCode workstation
  | HTTPS + client Bearer key
  v
Caddy (TLS, request limits, dashboard/admin access controls)
  |
  v
Flask / Gunicorn gateway (authentication, validation, routing, cooldowns)
  |                         |
  |                         +--> SQLite (state, usage/journal, encrypted provider keys)
  v
Configured OpenAI-compatible providers
```

The Compose deployment exposes the gateway's port on `127.0.0.1:8080` only. Caddy serves the public HTTPS endpoint on ports 80 and 443 and reaches the gateway over the Docker network. The dashboard should be reached through an SSH tunnel unless you have deliberately configured a trusted admin IP allowlist.

The current deployment uses one Gunicorn worker with multiple threads and one SQLite database. Do not scale it to multiple gateway replicas without first redesigning and testing its in-process routing state, rate estimates, and persistence strategy.

## Repository layout

The application and its deployment files live in the `oc-gateway/` directory of this repository. Run the commands below from that directory unless a command explicitly says otherwise.

| Path | Purpose |
| --- | --- |
| `oc-gateway/gateway/` | Flask application and gateway implementation. |
| `oc-gateway/config/providers.yaml` | Provider, model, alias, routing, cooldown, and context configuration. |
| `oc-gateway/deploy/` | Caddy configuration and database backup helper. |
| `oc-gateway/docker-compose.yml` | Gateway and Caddy services, volumes, ports, and container restrictions. |
| `oc-gateway/Dockerfile` | Gateway container image. |
| `oc-gateway/opencode.json` | Example OpenCode provider configuration. |
| `oc-gateway/tools/` | Key-generation and project-state utilities. |
| `oc-gateway/tests/` | Automated tests and test-report generator. |
| `oc-gateway/templates/` | Templates for project-state files. |
| `oc-gateway/docs/` | Architecture, deployment, security, and troubleshooting documentation. |
| `oc-gateway/TEST_REPORT.md` | Latest checked-in test report; regenerate it after running tests. |

## Requirements

### Server

- Linux VM or server with Docker Engine and Docker Compose v2.
- A DNS name pointing to the server.
- Inbound TCP ports 80 and 443 allowed for Caddy and certificate issuance; SSH access for administration.
- Persistent storage for the Docker volumes, especially the SQLite gateway data volume.
- Outbound HTTPS access to configured model providers.

The gateway container is configured with a 512 MB memory limit. Choose a host with sufficient memory for both containers and the operating system. For provider and hosting options, consult [`oc-gateway/docs/DEPLOYMENT.md`](oc-gateway/docs/DEPLOYMENT.md); hosting prices, free tiers, and quotas are time-sensitive and must be rechecked.

### Workstation

- Python 3 for key generation and project-state utilities.
- OpenCode installed and configured to use the gateway.
- API credentials for at least one supported provider. Provider accounts and model access are managed directly with those providers.

## Deploy the gateway

The examples below assume you have cloned this repository and are working from its application directory:

```bash
cd oc-gateway
```

If you are deploying from a fresh clone, first clone the repository and then enter its `oc-gateway/` subdirectory:

```bash
git clone https://github.com/1Byword/oc-gateway.git
cd oc-gateway/oc-gateway
```

### 1. Prepare DNS and firewall

Create a DNS A record for your gateway domain pointing to the server. Allow inbound ports 80 and 443 in both the host firewall and cloud firewall, and allow SSH from your administrative network. Do not expose port 8080 publicly; Compose binds it to loopback.

### 2. Generate gateway secrets

On a trusted workstation with Python 3, from the application directory, run:

```bash
python3 tools/genkeys.py
```

The command prints the values needed for the server environment and displays a client key and admin token once. Store the client key, admin token, and `GATEWAY_MASTER_KEY` in a secure password manager or secret store. Do not commit generated secrets or paste them into issues, logs, or chat.

The generated values serve different purposes:

- `GATEWAY_MASTER_KEY` encrypts provider credentials stored in SQLite. Preserve it securely and separately from database backups; losing it prevents decryption of the stored provider keys.
- `GATEWAY_KEY_HASHES` is the server-side hash for the client key used by OpenCode.
- `ADMIN_TOKEN_HASHES` is the server-side hash for dashboard/admin authentication.
- The raw client key is supplied to OpenCode as `GATEWAY_KEY`.
- The raw admin token is used only to sign in to the dashboard.

### 3. Create the server environment file

Create a private `.env` file in the application directory using the repository's `.env.example` as the template, if present. Populate the generated values and set the deployment values required by the Compose and Caddy configuration, including `GATEWAY_DOMAIN` and `ADMIN_ALLOW_CIDRS`. Review the template and [`oc-gateway/docs/DEPLOYMENT.md`](oc-gateway/docs/DEPLOYMENT.md) for all environment settings required by the checked-out version.

For SSH-tunnel-only dashboard access, configure the admin allowlist so the proxy does not permit unintended public access; verify the Caddy access rule for your setup before deployment. Never commit `.env` or copy it into a public artifact. Restrict its file permissions, for example:

```bash
chmod 600 .env
```

### 4. Configure provider models

Review `config/providers.yaml`. The checked-in model IDs are examples, not availability guarantees. In particular:

1. Obtain the current model IDs from the provider or dashboard **Test connection** action.
2. Replace stale IDs in the provider model lists and aliases.
3. Set appropriate priorities, routing strategy, and any documented quota estimates.
4. Check provider terms, quotas, and data-use policies before sending requests.

Do not invent or assume context limits and quotas. Values in configuration may be declarations or estimates rather than provider-verified facts.

### 5. Start and verify the services

From the application directory:

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 gateway
docker compose logs --tail=100 caddy
```

Wait for the gateway health check to pass. Verify the public health endpoint, replacing the hostname with your configured domain:

```bash
curl --fail https://YOUR-DOMAIN/health
```

Caddy obtains and renews HTTPS certificates automatically when DNS and network access are correctly configured. Certificate issuance and production deployment behavior should be verified in your own environment.

### 6. Access the dashboard securely

From your workstation, open an SSH tunnel to the server:

```bash
ssh -L 8080:127.0.0.1:8080 user@server
```

Keep the tunnel open and visit <http://127.0.0.1:8080/dashboard>. Authenticate using the generated admin token. Then:

1. Add one or more provider API keys using the dashboard.
2. Use **Test connection** to check connectivity and discover provider model IDs.
3. Configure the models and aliases in `config/providers.yaml` when needed, then apply/restart the service as appropriate.
4. Use **Probe capabilities** to verify chat, streaming, and tool-calling behavior. Probes send real requests and may consume quota.
5. Confirm the selected routing strategy and model health before connecting clients.

If you change environment values, recreate/restart the affected services so they receive the updated environment.

## Configure providers and routing

All provider and alias definitions are in `config/providers.yaml`. No code changes are required for ordinary provider/model configuration.

### Providers and aliases

A provider entry identifies an API base URL, key source, priority, optional limits, and one or more model IDs. Aliases such as `coder` and `fast` are the model names OpenCode uses; each alias can list multiple provider/model candidates. Keep alias members synchronized with the provider model IDs.

Provider API keys may be supplied through configured environment variables or added through the dashboard, where they are stored encrypted in the gateway database. Avoid storing raw provider keys in YAML committed to version control.

### Routing strategies

- **`priority`**: try configured candidates in priority order and fail back according to the routing behavior.
- **`failover`**: prefer the active candidate and move to another candidate on failure; failback behavior is controlled by `failback_seconds`.
- **`balanced`**: rank candidates using configured quality weights and observed latency/success behavior.

The router filters candidates based on their enabled/health state, cooldowns, quota state, request capability requirements, and context-window fit. Error handling differs by response class; for example, invalid provider credentials are not blindly retried, and client request errors are returned rather than treated as provider outages. See [`oc-gateway/docs/ARCHITECTURE.md`](oc-gateway/docs/ARCHITECTURE.md) for exact behavior.

### Quotas and model verification

Configured free-tier limits are estimates based on requests visible to this gateway; they cannot account for requests using the same provider account elsewhere. Provider rate-limit headers are treated as verified information when available. Model catalogs and free-tier plans can change without notice.

A model's configured `claims` are not capability verification. Run the dashboard probe after changing a model, provider, or relevant configuration. Tool-calling requests are gated on verified tool capability by default. Context size and maximum output are declared values, not verified by the capability probe.

## Connect OpenCode

From the OpenCode project directory, initialize the project continuity files using the utility in this checkout:

```bash
python3 /path/to/oc-gateway/tools/ocstate.py init
```

Copy `oc-gateway/opencode.json` into the OpenCode project directory. Update `YOUR-DOMAIN` in the configuration so its base URL points to `https://YOUR-DOMAIN/v1`. Keep the OpenCode configuration under version control only if it contains no credentials; the provided configuration reads the client key from the environment.

Set the client key in the shell that will run OpenCode:

```bash
export GATEWAY_KEY='your-client-key'
opencode
```

For persistent shell use, configure the secret through your operating system's secret manager rather than committing it to a shell profile or project file. The sample config selects `gateway/coder` and `gateway/fast`, and requests confirmation before OpenCode performs tool actions. Review OpenCode's permission behavior before changing it.

OpenCode sends conversation history with each request. When failover occurs, the selected model receives that request's history; models do not share hidden session state. The gateway cannot resume a response token-by-token after a stream interruption.

## Operate and maintain

Run Docker Compose commands from the application directory (the directory containing `docker-compose.yml`).

### Service status, logs, and health

```bash
docker compose ps
docker compose logs -f gateway
```

Follow reverse-proxy logs with:

```bash
docker compose logs -f caddy
```

Check the gateway health endpoint:

```bash
curl --fail https://YOUR-DOMAIN/health
```

Gateway logs redact common key-like strings, but redaction is not a guarantee. Avoid logging request bodies or sharing unreviewed logs.

### Configuration changes

Back up relevant configuration before editing. After changing provider configuration, verify the YAML and restart/recreate services when required so the active process reads the intended configuration. Re-test provider connectivity and probe capabilities after model changes. Keep a record of changes to routing strategy, provider IDs, quotas, and secrets rotation without recording secret values.

### Upgrade procedure

Schedule updates during a maintenance window appropriate to your workload. Review release notes and configuration changes, take and verify an off-server database backup, then update and rebuild:

```bash
git pull
docker compose build --pull
docker compose up -d
docker compose ps
```

Check gateway and Caddy logs, verify `/health`, and confirm a provider test and OpenCode request after the upgrade. If the release changes database or configuration formats, follow its migration instructions before continuing. Pin image/dependency versions according to your organization's change-management policy; do not assume that pulling the latest images is a substitute for testing.

### Backups and recovery

The gateway database contains persistent state and encrypted provider credentials. Back up the database regularly and copy backups off the server. Protect backups as sensitive data; encryption of provider credentials inside SQLite does not make the entire database harmless to disclose. Store `GATEWAY_MASTER_KEY` separately in a secure secret store.

The helper `deploy/backup.sh` copies the gateway's internal backup files to a destination and removes destination SQLite backups older than 30 days. **Review and adjust its configured Compose file path before scheduling it**; the script currently references `/opt/oc-gateway/docker-compose.yml` by default. Its optional first argument specifies the backup destination. An example cron schedule is included in the script.

A restore should be rehearsed before you need it. Follow the current restore instructions in [`oc-gateway/docs/DEPLOYMENT.md`](oc-gateway/docs/DEPLOYMENT.md), stop or quiesce the service as required, restore a known-good database, preserve the matching master key, restart, and verify provider credentials and service health. Keep recovery copies off-host and access-controlled.

### Credential rotation

- **Provider key:** replace it using the dashboard, then test the connection and probe the affected models.
- **Client/admin keys:** generate replacement hashes and raw keys, update the server environment securely, restart the service, update clients/admin users, and revoke old credentials. Plan the sequence to avoid locking out administrators or clients.
- **Master key:** do not casually replace it. Existing encrypted provider keys depend on the original key. Follow the documented migration/backup procedure and verify decryption before discarding the old key.

## Project-state utilities

Run these from the OpenCode project's Git working tree. Replace `/path/to/oc-gateway` with the location of the application checkout.

```bash
python3 /path/to/oc-gateway/tools/ocstate.py init
python3 /path/to/oc-gateway/tools/ocstate.py checkpoint "before refactor"
python3 /path/to/oc-gateway/tools/ocstate.py checkpoints
python3 /path/to/oc-gateway/tools/ocstate.py diff
python3 /path/to/oc-gateway/tools/ocstate.py resume
python3 /path/to/oc-gateway/tools/ocstate.py lint
```

To restore a checkpoint, first inspect the reference and the current working tree. Restore overwrites tracked/worktree file contents from that checkpoint; it creates a safety checkpoint first when invoked with `--yes`, and it does not delete files created after the checkpoint:

```bash
python3 /path/to/oc-gateway/tools/ocstate.py restore REF --yes
```

`init` creates `PROJECT_STATE.md` and `AGENTS.md` only when they do not already exist. Keep these files current with confirmed project goals, decisions, changed files, test results, and next actions. The `lint` command is heuristic and cannot guarantee that all secrets are detected.

## Security and privacy

Security controls currently documented by the project include HTTPS through Caddy, separate client and admin authentication, hash-based storage for gateway client/admin credentials, encrypted provider keys in SQLite, request validation and size limits, authentication throttling, a restrictive dashboard content policy, and container hardening. The gateway container runs as a non-root user, uses a read-only root filesystem, drops Linux capabilities, and has a memory limit. Review [`oc-gateway/docs/SECURITY.md`](oc-gateway/docs/SECURITY.md) and the actual deployment configuration before exposure.

Operational responsibilities and residual risks:

- **Protect the host and environment.** A server administrator with sufficient privileges can access process memory and environment values, including the master key and provider credentials at runtime.
- **Protect data sent to providers.** Prompts, conversation history, code, and tool outputs sent to the gateway may be forwarded to third-party model providers. Review their retention, training, and privacy terms. Do not send sensitive material unless approved for those providers.
- **Restrict administration.** Prefer SSH-tunnel dashboard access. Keep the admin allowlist narrow, use unique credentials, secure SSH, and do not expose the internal gateway port publicly.
- **Secure backups.** Database backups contain encrypted provider credentials and operational state. Store them off-host with access control; retain the master key separately.
- **Understand rate limits.** In-process rate-limit state resets on restart. Provider quota estimates are incomplete when keys are used outside this gateway.
- **Use tool approval.** OpenCode permission prompts are an important safeguard for local edits and shell commands. Review each requested action.
- **Do not bypass provider terms.** Respect provider rate limits and account policies; failover is for availability, not quota evasion.

These controls do not constitute a security certification or guarantee that the service is suitable for a regulated environment. Conduct your own threat model, access review, and compliance assessment.

## Testing and limitations

From the application directory, run the automated tests and regenerate the report:

```bash
python3 tests/report.py
```

The report is generated from actual test results. The test suite uses local provider fakes and does not spend real provider quota. Review `TEST_REPORT.md` for current results and its explicit coverage limitations.

The checked-in test report documents areas that may not be exercised in the test environment, including real provider APIs, Docker/Compose execution, Caddy certificate issuance, a real OpenCode session, and full browser rendering of the dashboard. Passing tests should not be interpreted as validation of those deployment paths.

Current architectural constraints include one gateway instance, one Gunicorn worker, in-process routing/limiting state, and SQLite persistence. The service does not provide clustered state, cross-region failover, an SLA, or a promise of provider availability. If your requirements need those properties, design and validate them before production use.

## Troubleshooting

| Symptom | Likely cause | First action |
| --- | --- | --- |
| Gateway returns `401 invalid_api_key` | Client key is wrong or its hash is not configured. | Verify `GATEWAY_KEY` and `GATEWAY_KEY_HASHES`; restart after environment changes. |
| Gateway returns `429 auth_throttled` | Too many failed authentication attempts from the source IP. | Wait for the throttle window to expire; check which client is using incorrect credentials. |
| `503 no_provider_available` or tool capability is not verified | No eligible model is available for this request. | Test provider connection, verify model IDs, then run capability probes. |
| Providers are in cooldown | Rate limit, exhausted quota, or upstream outage. | Check dashboard health/retry details and the `Retry-After` response header. |
| Provider key is marked invalid | Key may be incorrect, revoked, or expired. | Replace it in the dashboard and test the connection. |
| A model returns 404 or is unavailable | Provider model ID may have changed or been removed. | Query the provider's current models, update configuration, and probe again. |
| `stream_interrupted` | Provider stream ended after output began. | Review the request and resume at task level with `ocstate.py resume`; the gateway will not splice model outputs. |
| Dashboard returns 403 | Request source is not allowed by the Caddy admin rule. | Use the SSH tunnel or update `ADMIN_ALLOW_CIDRS` carefully. |
| Startup says `GATEWAY_MASTER_KEY` is missing | Required encryption key is not available in the server environment. | Restore the correct secret to `.env` and restart. |
| Stored keys cannot be decrypted | The configured master key does not match the key used to encrypt them. | Restore the original master key from secure storage or re-add provider credentials. |
| Slow first response | Provider cold start or failover to a slower model. | Check provider health, strategy, cooldowns, and failback configuration. |

For additional diagnostic steps, see [`oc-gateway/docs/TROUBLESHOOTING.md`](oc-gateway/docs/TROUBLESHOOTING.md). Gateway logs can be inspected with `docker compose logs -f gateway`.

## Documentation

- [Architecture and design decisions](oc-gateway/docs/ARCHITECTURE.md)
- [Deployment, maintenance, and recovery](oc-gateway/docs/DEPLOYMENT.md)
- [Security notes and residual risks](oc-gateway/docs/SECURITY.md)
- [Troubleshooting guide](oc-gateway/docs/TROUBLESHOOTING.md)
- [Latest test report](oc-gateway/TEST_REPORT.md)
- [Provider and routing configuration](oc-gateway/config/providers.yaml)
- [OpenCode example configuration](oc-gateway/opencode.json)

## Contributing and change management

Changes to routing, authentication, provider integrations, persistence, container configuration, or secret handling can affect availability and security. Before proposing or deploying a change:

1. Keep changes scoped and document configuration or operational impacts.
2. Run `python3 tests/report.py` from `oc-gateway/` and review failures and coverage gaps.
3. Update the relevant documentation and test report when behavior changes.
4. Never include real credentials, production data, or sensitive provider responses in commits or test fixtures.
5. Validate Docker, TLS, provider behavior, and OpenCode integration in a controlled environment before production rollout.

## License

See the repository for license information. If no license file is present in the checked-out version, contact the repository owner before redistributing or using the project under an assumed license.
