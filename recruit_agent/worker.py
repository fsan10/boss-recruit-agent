from __future__ import annotations

import json
import threading
from pathlib import Path

from .common import DomainError, canonical, iso, now, uid
from .db import Database
from .harnesses import HarnessAdapter, terminate_tree
from .platforms import BossAgentCliAdapter, STOP_CODES


class Worker:
    def __init__(self, service):
        self.s = service
        self.owner = uid("worker")
        self.stop_event = threading.Event()
        self.process = None
        self.active_lease = None
        self.started = now()
        self.last_audit_heartbeat = 0
        self.threads = []

    def start(self):
        with self.s.db.transaction() as con:
            con.execute("UPDATE outbox SET state='OUTCOME_UNKNOWN',error=? WHERE state='SENDING'", (canonical({"code":"ACTION_OUTCOME_UNKNOWN","message":"服务恢复时发现未确认的写请求，禁止自动重发。"}),))
            Database.audit(con, self.owner, "service_started", {"started_at": iso(self.started), "version": "0.1.0"})
        for target, name in ((self.heartbeat_loop, "heartbeat"), (self.schedule_loop, "scheduler"), (self.score_loop, "scoring"), (self.collect_loop, "collector")):
            thread = threading.Thread(target=target, name="recruit-" + name, daemon=True)
            self.threads.append(thread)
            thread.start()

    def stop(self):
        self.stop_event.set()
        if self.process:
            terminate_tree(self.process)
        for thread in self.threads:
            thread.join(timeout=2)
        self.s.db.set_setting("worker_heartbeat", {"timestamp": 0, "started_at": iso(self.started), "owner": self.owner, "stopped_at": iso()})
        with self.s.db.transaction() as con:
            Database.audit(con, self.owner, "service_stopped", {"uptime_seconds": round(now() - self.started)})

    def heartbeat_loop(self):
        while not self.stop_event.is_set():
            self.s.db.set_setting("worker_heartbeat", {"timestamp": now(), "started_at": iso(self.started), "owner": self.owner, "uptime_seconds": round(now() - self.started), "active_batch": self.active_lease["batch"]["batch_id"] if self.active_lease else None})
            if now() - self.last_audit_heartbeat >= 60:
                with self.s.db.transaction() as con:
                    Database.audit(con, self.owner, "service_heartbeat", {"uptime_seconds": round(now() - self.started)})
                self.last_audit_heartbeat = now()
            lease = self.active_lease
            if lease:
                try:
                    self.s.queue.heartbeat(lease["task_id"], lease["lease_token"], lease["fencing_token"], self.s.config["queue"]["lease_seconds"])
                except DomainError:
                    if self.process:
                        terminate_tree(self.process)
            self.stop_event.wait(min(10, self.s.config["queue"]["heartbeat_seconds"]))

    def schedule_loop(self):
        while not self.stop_event.is_set():
            try:
                self.s.flush_batches()
                cfg = self.s.config
                with self.s.db.transaction() as con:
                    self.resume_daily_budgets(con)
                    self.s.queue.recover(con, cfg["queue"]["max_attempts"])
                    if cfg["platform"]["live_enabled"]:
                        for row in con.execute("SELECT cc.* FROM collection_cursors cc JOIN accounts a ON a.account_id=cc.account_id WHERE a.status NOT IN ('PAUSED','DEMO') AND (cc.next_poll IS NULL OR cc.next_poll<=?)", (now(),)).fetchall():
                            object_id = canonical({"account_id": row["account_id"], "job_id": row["job_id"], "channel": row["channel"], "page": row["page"], "offset": row["offset"]})
                            self.s.queue.enqueue(con, "collect", object_id, "collect:" + digest_cursor(row))
                            con.execute("UPDATE collection_cursors SET next_poll=? WHERE account_id=? AND job_id=? AND channel=?", (now() + cfg["platform"]["poll_seconds"], row["account_id"], row["job_id"], row["channel"]))
            except Exception:
                self.s.db.set_setting("scheduler_error", {"code": "SCHEDULER_ERROR", "timestamp": now()})
            if now() - self.s.db.setting("maintenance_last", now()) > 3600:
                self.maintenance()
            if self.s.db.setting("maintenance_last") is None:
                self.s.db.set_setting("maintenance_last", now())
            self.stop_event.wait(2)

    def resume_daily_budgets(self, con, clock=None):
        t = now() if clock is None else clock
        for row in con.execute("SELECT * FROM tasks WHERE state='BLOCKED' AND next_run_at<=?", (t,)).fetchall():
            error = json.loads(row["error"] or "{}")
            if error.get("code") != "BUDGET_EXHAUSTED":
                continue
            con.execute("UPDATE tasks SET state='RETRY',attempts=0,error=NULL,next_run_at=? WHERE task_id=?", (t, row["task_id"]))
            if row["kind"] == "score":
                con.execute("UPDATE evaluation_batches SET state='RETRY' WHERE batch_id=?", (row["object_id"],))
            Database.audit(con, row["task_id"], "daily_budget_resumed")
        pause = self.s.db.setting("harness_pause")
        if pause and pause.get("code") == "BUDGET_EXHAUSTED" and pause.get("details", {}).get("resumes_at", float("inf")) <= t:
            self.s.db.set_setting("harness_pause", None, con)

    def score_loop(self):
        while not self.stop_event.is_set():
            cfg = self.s.config
            h = cfg["harness"]
            if not h["enabled"] or h["primary"] == "mcp" or self.s.db.setting("harness_pause"):
                self.stop_event.wait(2)
                continue
            # Missing binaries block before claiming tasks, so unavailable dependencies do not burn attempts.
            adapter = HarnessAdapter(h)
            try:
                adapter.executable()
            except DomainError as error:
                self.s.db.set_setting("harness_pause", error.as_dict())
                continue
            lease = self.s.claim_batch(self.owner, force=False)
            if not lease.get("batch"):
                if self.run_rubric(adapter):
                    continue
                self.stop_event.wait(2)
                continue
            self.active_lease = lease
            task = {"task_id": lease["task_id"], "lease_token": lease["lease_token"], "fencing_token": lease["fencing_token"]}
            try:
                if lease["job"]["scorer_profile_version"] != self.s.profile_version():
                    raise DomainError("SCORER_VERSION_CHANGED", "harness 配置已变更，请在岗位页重新审核启用新评分版本。")
                self.s.reserve_budget("harness_runs", h["max_runs_per_day"])
                envelope, metadata = adapter.evaluate(lease["job"], lease["batch"], self.set_process)
                self.s.submit(lease["batch"]["batch_id"], lease["lease_token"], lease["fencing_token"], envelope, metadata, lease["task_id"])
            except DomainError as error:
                pause = error.code in {"HARNESS_AUTH_REQUIRED", "HARNESS_LIMIT", "CAPABILITY_UNAVAILABLE", "BUDGET_EXHAUSTED"}
                block = pause or error.code in {"DATA_PROCESSOR_NOT_ENABLED", "MODEL_NOT_CONFIGURED", "SCORER_VERSION_CHANGED"}
                if pause:
                    self.s.db.set_setting("harness_pause", error.as_dict())
                try:
                    self.s.queue.fail(task, error, cfg["queue"]["max_attempts"], pause=block)
                except DomainError:
                    pass
            except Exception:
                try:
                    self.s.queue.fail(task, {"code": "WORKER_ERROR", "message": "评分器发生内部异常；未覆盖已完成结果。"}, cfg["queue"]["max_attempts"])
                except DomainError:
                    pass
            finally:
                self.active_lease = None
                self.process = None

    def set_process(self, process):
        self.process = process

    def run_rubric(self, adapter):
        task = self.s.queue.claim("rubric", self.owner, max(90, self.s.config["harness"]["timeout_seconds"] + 30))
        if not task:
            return False
        try:
            from .jobs import generate_rubric
            job = self.s.get_job(task["object_id"])
            if job["active"]:
                raise DomainError("JOB_ALREADY_ACTIVE", "岗位已启用，AI 草案不覆盖人工标准。")
            self.s.reserve_budget("harness_runs", self.s.config["harness"]["max_runs_per_day"])
            profile, metadata = generate_rubric(self.s, job, self.set_process)
            with self.s.db.transaction() as con:
                self.s.queue.assert_lease(con, task["task_id"], task["lease_token"], task["fencing_token"])
                if con.execute("SELECT active FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0]:
                    raise DomainError("JOB_ALREADY_ACTIVE", "用户已启用标准，AI 草案不覆盖。")
                profile["rubric_version"] = uid("rubric")
                con.execute("UPDATE jobs SET profile=? WHERE job_id=?", (canonical(profile), job["job_id"]))
                self.s.save_rubric(con, profile)
                self.s.db.set_setting("job-note:" + job["job_id"], "AI 已生成岗位量表，等待人工审核启用。", con)
                con.execute("UPDATE tasks SET state='COMPLETED',lease_token=NULL,lease_until=NULL WHERE task_id=?", (task["task_id"],))
                Database.audit(con, job["job_id"], "rubric_generated", metadata)
        except DomainError as error:
            pause = error.code in {"HARNESS_AUTH_REQUIRED", "HARNESS_LIMIT", "CAPABILITY_UNAVAILABLE", "BUDGET_EXHAUSTED"}
            if pause:
                self.s.db.set_setting("harness_pause", error.as_dict())
            try:
                self.s.queue.fail(task, error, pause=pause)
            except DomainError:
                pass
        except Exception:
            self.s.queue.fail(task, {"code": "RUBRIC_ERROR", "message": "量表生成异常，草案未启用。"})
        finally:
            self.process = None
        return True

    def collect_loop(self):
        while not self.stop_event.is_set():
            cfg = self.s.config
            if not cfg["platform"]["live_enabled"]:
                self.stop_event.wait(2)
                continue
            task = self.s.queue.claim("collect", self.owner, 120)
            if not task:
                self.stop_event.wait(2)
                continue
            cursor = json.loads(task["object_id"])
            lost = threading.Event()
            def renew():
                while not lost.wait(15):
                    try:
                        self.s.queue.heartbeat(task["task_id"], task["lease_token"], task["fencing_token"], 120)
                    except DomainError:
                        break
            thread = threading.Thread(target=renew, daemon=True)
            thread.start()
            try:
                result = BossAgentCliAdapter(self.s).collect(**cursor)
                with self.s.db.transaction() as con:
                    self.s.queue.assert_lease(con, task["task_id"], task["lease_token"], task["fencing_token"])
                    con.execute("UPDATE tasks SET state='COMPLETED',lease_token=NULL,lease_until=NULL WHERE task_id=?", (task["task_id"],))
                    con.execute("UPDATE collection_cursors SET page=?,offset=?,last_poll=?,next_poll=? WHERE account_id=? AND job_id=? AND channel=?", (result["next_page"], result["next_offset"], now(), now() + (30 if result["has_more"] else cfg["platform"]["poll_seconds"]), cursor["account_id"], cursor["job_id"], cursor["channel"]))
            except DomainError as error:
                pause = error.code in STOP_CODES - {"BUDGET_EXHAUSTED"} or error.code in {"FIELD_MAPPING_REQUIRED", "CAPABILITY_UNAVAILABLE", "IDENTITY_UNVERIFIED"}
                if pause:
                    self.s.pause_account(cursor["account_id"], error.code)
                try:
                    self.s.queue.fail(task, error, pause=pause)
                except DomainError:
                    pass
            except Exception:
                try:
                    self.s.queue.fail(task, {"code": "COLLECTOR_ERROR", "message": "采集器发生异常；已导入内容保留。"})
                except DomainError:
                    pass
            finally:
                lost.set()
                thread.join(timeout=1)

    def maintenance(self):
        from .maintenance import backup, retain
        try:
            retain(self.s)
            if now() - self.s.db.setting("backup_last", 0) >= 86400:
                backup(self.s)
                self.s.db.set_setting("backup_last", now())
            self.s.db.set_setting("maintenance_last", now())
        except Exception:
            self.s.db.set_setting("maintenance_error", {"code": "MAINTENANCE_FAILED", "timestamp": now()})


def digest_cursor(row):
    # last_poll distinguishes periodic scans, while concurrent schedulers share the same key.
    from .common import digest
    return digest({k: row[k] for k in ("account_id", "job_id", "channel", "page", "offset", "last_poll")})
