import logging
import os
import threading
import time

from app.server import create_app

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
DATA = os.environ.get("GATEWAY_DATA", "/data")
os.makedirs(DATA, exist_ok=True)
app = create_app(os.environ.get("GATEWAY_CONFIG", "/app/config/providers.yaml"), os.path.join(DATA, "gateway.sqlite"),
                 backup_dir=os.path.join(DATA, "backups"))


def _backup_loop():
    while True:
        time.sleep(6 * 3600)
        try:
            app.gateway.store.backup(os.path.join(DATA, "backups"))
        except Exception as e:           # never let a backup failure kill the gateway
            logging.getLogger("gateway").error("backup failed: %s", type(e).__name__)


threading.Thread(target=_backup_loop, daemon=True).start()
