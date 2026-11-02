# Deployment

## Where to host (checked 2026-11-02 against recent sources; free tiers change - re-check before relying on any)
The gateway needs: always-on, ~256-512 MB RAM, persistent disk (SQLite), outbound HTTPS, long-lived streaming connections.

| Option | Free-tier facts reported | Fit |
|---|---|---|
| Oracle Cloud Always Free | Reports say the Ampere A1 allowance was cut from 4 OCPU/24 GB to 2 OCPU/12 GB (effective 15 Jun 2026; some older sources still say 4/24). Always-on VM, 200 GB block storage. Risks: "out of capacity" when creating the VM, idle-instance reclamation, account sign-up rejections. | **Best truly free option; technically ideal.** Not guaranteed. |
| Render free web service | 512 MB, <1 CPU, 750 h/month; **sleeps after 15 idle minutes (~1 min wake)**; ephemeral filesystem (state and encrypted keys lost on restart); docs say not for production. | Poor: sleeping + no persistent disk breaks failover state and keys. |
| Railway | No free plan; $5 trial credit for 30 days then ~$1/month usage credit. | Not viable free. |
| Fly.io | No free allowance for new orgs; short trial then card. A shared-cpu-1x/256 MB machine is reported at about $2/month. | Cheapest PaaS option; check 256 MB is enough (use 512 MB if not). |
| Small VPS (any reputable host) | A few USD per month. | **Recommended fallback if Oracle is unavailable.** |

Recommendation: try Oracle Always Free (Ampere A1, 1 OCPU/6 GB is plenty; keep total within 2 OCPU/12 GB). If capacity is unavailable, a few-dollar VPS or Fly.io is the cheapest reliable choice. A "completely free and reliable" deployment cannot be promised.
Free-tier provider limits (also change): OpenRouter free models: 20 req/min and 50 req/day (1000/day once you have bought >= $10 credits at any time) per sources dated Oct 2026; model lists rotate without notice.

## Steps (Ubuntu VM with Docker)
1. Point a DNS A record for your domain at the VM; open ports 80/443 in the cloud firewall (and 22 for SSH).
2. `git clone` this folder to `/opt/oc-gateway` (do **not** commit `.env`).
3. `python3 tools/genkeys.py` (on your own machine is fine) -> fill `.env` (see `.env.example`). Keep `GATEWAY_MASTER_KEY` also in your password manager: without it the stored provider keys cannot be decrypted.
4. Edit `config/providers.yaml` (real model ids; see the note at the top of the file).
5. `docker compose up -d --build` ; `docker compose ps` should show healthy.
6. Dashboard through a tunnel: `ssh -L 8080:127.0.0.1:8080 user@server` -> http://127.0.0.1:8080/dashboard . Public `/admin` and `/dashboard` are blocked by Caddy except `ADMIN_ALLOW_CIDRS`.
7. In the dashboard: add keys, **Test connection**, **Probe capabilities**, choose strategy.
8. Client: `export GATEWAY_KEY=<client key>`, copy `opencode.json` (edit domain), run `opencode`.
9. Schedule `deploy/backup.sh` (cron) and copy backups off the server.

## Maintenance
* Updates: `git pull && docker compose build --pull && docker compose up -d` monthly; `pip list --outdated` inside the image; Caddy image updates itself on pull.
* Rotate gateway/admin keys: run `genkeys.py`, replace hashes in `.env`, `docker compose up -d`.
* Rotate a provider key: dashboard "Save / replace key" (same label replaces and re-activates it).
* Restore: stop gateway, copy a file from `/data/backups/*.sqlite` to `/data/gateway.sqlite`, start. A corrupt DB is auto-quarantined and the newest backup restored at startup.

> Not verified in the build sandbox: image build, compose, Caddy certificate issuance. Treat the first deployment as a test.
