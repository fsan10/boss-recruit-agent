from __future__ import annotations

from .common import DomainError, canonical, now, uid
from .db import Database


class Queue:
    def __init__(self, db: Database):
        self.db = db

    def enqueue(self, con, kind, object_id, key, next_run=None):
        task_id = uid("task")
        con.execute("INSERT OR IGNORE INTO tasks(task_id,kind,object_id,idempotency_key,state,next_run_at,created_at) VALUES (?,?,?,?,?,?,?)",
                    (task_id, kind, object_id, key, "READY", now() if next_run is None else next_run, now()))
        return dict(con.execute("SELECT * FROM tasks WHERE idempotency_key=?", (key,)).fetchone())

    def recover(self, con, maximum=3, clock=None):
        t = now() if clock is None else clock
        rows = con.execute("SELECT * FROM tasks WHERE state='RUNNING' AND lease_until<=?", (t,)).fetchall()
        for row in rows:
            state = "FAILED" if row["attempts"] >= maximum else "RETRY"
            err = canonical({"code": "LEASE_LOST", "message": "运行器租约到期；已完成成员保留。"})
            con.execute("UPDATE tasks SET state=?,lease_owner=NULL,lease_token=NULL,lease_until=NULL,next_run_at=?,error=? WHERE task_id=?",
                        (state, t, err, row["task_id"]))
            if row["kind"] == "score":
                con.execute("UPDATE evaluation_batches SET state=? WHERE batch_id=?", (state, row["object_id"]))
                if state == "FAILED":
                    con.execute("UPDATE batch_members SET state='FAILED',error=? WHERE batch_id=? AND state!='COMPLETED'", (err, row["object_id"]))
            Database.audit(con, row["task_id"], "lease_recovered", {"state": state})

    def claim(self, kind, owner, lease_seconds=90, object_id=None, maximum=3, clock=None):
        t = now() if clock is None else clock
        with self.db.transaction() as con:
            self.recover(con, maximum, t)
            sql = "SELECT * FROM tasks WHERE kind=? AND state IN ('READY','RETRY') AND next_run_at<=?"
            args = [kind, t]
            if object_id:
                sql += " AND object_id=?"
                args.append(object_id)
            row = con.execute(sql + " ORDER BY created_at,task_id LIMIT 1", args).fetchone()
            if not row:
                return None
            token = uid("lease")
            con.execute("UPDATE tasks SET state='RUNNING',attempts=attempts+1,lease_owner=?,lease_token=?,lease_until=?,heartbeat=?,fencing_token=fencing_token+1,error=NULL WHERE task_id=?",
                        (owner, token, t + lease_seconds, t, row["task_id"]))
            return dict(con.execute("SELECT * FROM tasks WHERE task_id=?", (row["task_id"],)).fetchone())

    @staticmethod
    def assert_lease(con, task_id, token, fence, clock=None):
        row = con.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        t = now() if clock is None else clock
        if not row or row["state"] != "RUNNING" or row["lease_token"] != token or row["fencing_token"] != fence or row["lease_until"] <= t:
            raise DomainError("LEASE_LOST", "租约已失效，迟到结果不能覆盖当前任务。", status=409)
        return row

    def heartbeat(self, task_id, token, fence, seconds=90):
        with self.db.transaction() as con:
            self.assert_lease(con, task_id, token, fence)
            con.execute("UPDATE tasks SET lease_until=?,heartbeat=? WHERE task_id=?", (now() + seconds, now(), task_id))
        return {"lease_until": now() + seconds, "fencing_token": fence}

    def fail(self, task, error, maximum=3, delay=None, pause=False):
        with self.db.transaction() as con:
            row = self.assert_lease(con, task["task_id"], task["lease_token"], task["fencing_token"])
            state = "BLOCKED" if pause else ("FAILED" if row["attempts"] >= maximum else "RETRY")
            delay = delay if delay is not None else (30, 120, 300)[min(row["attempts"] - 1, 2)]
            data = error.as_dict() if isinstance(error, DomainError) else error
            con.execute("UPDATE tasks SET state=?,next_run_at=?,error=?,lease_token=NULL,lease_until=NULL WHERE task_id=?",
                        (state, now() + delay, canonical(data), row["task_id"]))
            if row["kind"] == "score":
                con.execute("UPDATE evaluation_batches SET state=? WHERE batch_id=?", (state, row["object_id"]))
                con.execute("UPDATE batch_members SET state=?,error=? WHERE batch_id=? AND state!='COMPLETED'",
                            ("FAILED" if state == "FAILED" else "PENDING", canonical(data), row["object_id"]))
            Database.audit(con, row["task_id"], "task_failed", {"code": data.get("code"), "state": state})
