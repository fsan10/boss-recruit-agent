from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from contextlib import contextmanager
from pathlib import Path

from .common import ROOT, canonical, now


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.path.parent.chmod(0o700)
        with closing(self.connect()) as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY)")
            for migration in sorted((ROOT / "migrations").glob("*.sql")):
                con.execute("BEGIN IMMEDIATE")
                try:
                    if not con.execute("SELECT 1 FROM schema_migrations WHERE version=?", (migration.name,)).fetchone():
                        statement = ""
                        for line in migration.read_text(encoding="utf-8").splitlines(True):
                            statement += line
                            if sqlite3.complete_statement(statement):
                                con.execute(statement)
                                statement = ""
                        con.execute("INSERT INTO schema_migrations VALUES (?)", (migration.name,))
                    con.commit()
                except BaseException:
                    con.rollback()
                    raise
        if os.name != "nt":
            self.path.chmod(0o600)

    def connect(self):
        con = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    @contextmanager
    def transaction(self):
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def all(self, sql, args=()):
        with closing(self.connect()) as con:
            return [dict(r) for r in con.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        with closing(self.connect()) as con:
            row = con.execute(sql, args).fetchone()
            return dict(row) if row else None

    def setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key, value, con=None):
        if con is None:
            with self.transaction() as tx:
                return self.set_setting(key, value, tx)
        con.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, canonical(value)))

    @staticmethod
    def audit(con, object_id, event, data=None, actor="system"):
        con.execute("INSERT INTO audit_events(object_id,actor,event,data,created_at) VALUES (?,?,?,?,?)",
                    (object_id, actor, event, canonical(data or {}), now()))
