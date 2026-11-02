#!/bin/sh
# Off-box backup of the gateway DB (keys are encrypted inside; keep GATEWAY_MASTER_KEY somewhere else, e.g. a password manager).
# cron example (daily 03:10):  10 3 * * * /opt/oc-gateway/deploy/backup.sh
set -eu
DEST="${1:-/var/backups/oc-gateway}"; mkdir -p "$DEST"
docker compose -f /opt/oc-gateway/docker-compose.yml cp gateway:/data/backups/. "$DEST/" 
find "$DEST" -name '*.sqlite' -mtime +30 -delete
