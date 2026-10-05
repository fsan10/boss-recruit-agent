from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from .common import DomainError, canonical, iso, now, uid


def backup(service):
    folder = service.runtime / "backups"
    folder.mkdir(mode=0o700, exist_ok=True)
    destination = folder / (uid("backup") + ".zip")
    with tempfile.TemporaryDirectory(prefix="recruit-backup-") as temporary:
        database = Path(temporary) / "recruit.sqlite3"
        source = service.db.connect()
        target = sqlite3.connect(database)
        try:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise DomainError("BACKUP_FAILED", "备份完整性检查未通过。")
        finally:
            source.close()
            target.close()
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(database, "recruit.sqlite3")
            for subdir in ("attachments",):
                for file in (service.runtime / subdir).glob("**/*"):
                    if file.is_file() and not file.is_symlink():
                        archive.write(file, str(file.relative_to(service.runtime)))
            archive.writestr("manifest.json", canonical({"version": "0.1.0", "created_at": iso(), "credentials_included": False, "note": "包含候选人数据，请受控保管。"}))
    if os.name != "nt":
        destination.chmod(0o600)
    with service.db.transaction() as con:
        service.db.audit(con, destination.name, "backup_created")
    return destination


def restore(archive_path, destination):
    destination = Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise DomainError("RESTORE_TARGET_NOT_EMPTY", "恢复到新的空目录，不能覆盖正在使用的数据库。")
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > 10000 or sum(e.file_size for e in entries) > 512 * 1024**2:
            raise DomainError("BACKUP_INVALID", "备份超过解压预算。")
        for entry in entries:
            name = entry.filename
            if not (name in {"recruit.sqlite3", "manifest.json"} or name.startswith("attachments/")) or ".." in Path(name).parts or Path(name).is_absolute() or "\\" in name:
                raise DomainError("BACKUP_INVALID", "备份包含不允许的路径。")
        if "recruit.sqlite3" not in archive.namelist():
            raise DomainError("BACKUP_INVALID", "缺少数据库。")
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        archive.extractall(destination)
    with sqlite3.connect(destination / "recruit.sqlite3") as con:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise DomainError("BACKUP_INVALID", "恢复后数据库完整性检查未通过。")
        con.execute("UPDATE tasks SET state='RETRY',lease_token=NULL,lease_until=NULL,next_run_at=? WHERE state='RUNNING'", (now(),))
        con.execute("UPDATE outbox SET state='OUTCOME_UNKNOWN' WHERE state='SENDING'")
        config = json.loads(con.execute("SELECT value FROM settings WHERE key='config'").fetchone()[0])
        config["platform"]["live_enabled"] = False
        config["harness"]["enabled"] = False
        config["actions"]["send_enabled"] = False
        con.execute("UPDATE settings SET value=? WHERE key='config'", (canonical(config),))
        con.execute("DELETE FROM settings WHERE key IN ('platform_lock','worker_heartbeat')")
    return destination


def retain(service):
    config = service.config["privacy"]
    with service.db.transaction() as con:
        con.execute("UPDATE resume_snapshots SET raw_text=NULL WHERE created_at<?", (now() - config["raw_resume_retention_days"] * 86400,))
    # Delete complete candidate records only after their last local snapshot reaches the configured horizon.
    old = service.db.all("SELECT candidate_id FROM resume_snapshots GROUP BY candidate_id HAVING MAX(created_at)<?", (now() - config["evaluation_retention_days"] * 86400,))
    for row in old:
        try:
            service.delete_candidate(row["candidate_id"])
        except DomainError:
            pass
    for path in (service.runtime / "backups").glob("*.zip"):
        if path.stat().st_mtime < now() - config["raw_resume_retention_days"] * 86400:
            path.unlink()
