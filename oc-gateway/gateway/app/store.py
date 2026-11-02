import json
import logging
import os
import shutil
import sqlite3
import threading
import time

log = logging.getLogger("gateway.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS keys(
  id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, label TEXT, ciphertext TEXT, last4 TEXT,
  status TEXT DEFAULT 'active', enabled INTEGER DEFAULT 1, reason TEXT, last_used REAL DEFAULT 0,
  created REAL, UNIQUE(provider,label));
CREATE TABLE IF NOT EXISTS pstate(
  provider TEXT PRIMARY KEY, enabled INTEGER, priority INTEGER, state TEXT DEFAULT 'healthy',
  cooldown_until REAL DEFAULT 0, consec_failures INTEGER DEFAULT 0, last_success REAL, last_failure REAL,
  last_error_kind TEXT, last_error TEXT, quota_json TEXT, cooldown_cfg TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, provider TEXT, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS usage(
  provider TEXT, model TEXT, day TEXT, requests INTEGER DEFAULT 0, ok INTEGER DEFAULT 0,
  rate_limited INTEGER DEFAULT 0, quota_events INTEGER DEFAULT 0, errors INTEGER DEFAULT 0,
  prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0, tokens_estimated INTEGER DEFAULT 0,
  cost REAL DEFAULT 0, PRIMARY KEY(provider,model,day));
CREATE TABLE IF NOT EXISTS mstats(provider TEXT, model TEXT, ok INTEGER DEFAULT 0, fail INTEGER DEFAULT 0, ewma_ms REAL,
  PRIMARY KEY(provider,model));
CREATE TABLE IF NOT EXISTS caps(provider TEXT, model TEXT, cap TEXT, verified INTEGER, checked REAL, detail TEXT,
  PRIMARY KEY(provider,model,cap));
CREATE TABLE IF NOT EXISTS journal(id TEXT PRIMARY KEY, ts REAL, alias TEXT, provider TEXT, model TEXT, stream INTEGER,
  status TEXT, detail TEXT, finished REAL);
"""


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        res = self.conn.execute("PRAGMA integrity_check").fetchone()[0]
        if res != "ok":
            raise sqlite3.DatabaseError("integrity_check failed: %s" % res)
        self.conn.executescript(SCHEMA)

    # ---- recovery -------------------------------------------------
    @classmethod
    def open_with_recovery(cls, path, backup_dir=None):
        """Open the DB. If it is corrupt: quarantine it, restore the newest backup if any, else start fresh."""
        try:
            return cls(path)
        except sqlite3.DatabaseError as e:
            log.error("database unusable (%s); attempting recovery", e)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            if os.path.exists(path):
                shutil.move(path, "%s.corrupt-%s" % (path, stamp))
            for ext in ("-wal", "-shm"):
                if os.path.exists(path + ext):
                    os.remove(path + ext)
            if backup_dir and os.path.isdir(backup_dir):
                backups = sorted(f for f in os.listdir(backup_dir) if f.endswith(".sqlite"))
                if backups:
                    shutil.copy(os.path.join(backup_dir, backups[-1]), path)
                    log.warning("restored database from backup %s", backups[-1])
                    try:
                        return cls(path)
                    except sqlite3.DatabaseError:
                        os.remove(path)
            log.warning("starting with a fresh database")
            return cls(path)

    def backup(self, backup_dir, keep=14):
        os.makedirs(backup_dir, exist_ok=True)
        dest = os.path.join(backup_dir, "gateway-%s.sqlite" % time.strftime("%Y%m%d-%H%M%S"))
        with self.lock:
            dst = sqlite3.connect(dest)
            self.conn.backup(dst)
            dst.close()
        files = sorted(f for f in os.listdir(backup_dir) if f.endswith(".sqlite"))
        for old in files[:-keep]:
            os.remove(os.path.join(backup_dir, old))
        os.chmod(dest, 0o600)
        return dest

    # ---- helpers --------------------------------------------------
    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def q1(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args)

    def kv_get(self, k, default=None):
        r = self.q1("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r["v"]) if r else default

    def kv_set(self, k, v):
        self.x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

    def event(self, ts, provider, kind, detail=""):
        self.x("INSERT INTO events(ts,provider,kind,detail) VALUES(?,?,?,?)", (ts, provider, kind, str(detail)[:500]))
        self.x("DELETE FROM events WHERE id < (SELECT MAX(id) FROM events) - 5000")

    def mark_stale_journal(self, now):
        """After a restart, requests that never finished were interrupted."""
        self.x("UPDATE journal SET status='interrupted', detail='gateway restarted', finished=? WHERE status='started'", (now,))
