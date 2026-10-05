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
        self.threads = []

    def start(self):
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

    def heartbeat_loop(self):
        while not self.stop_event.is_set():
            self.s.db.set_setting("worker_heartbeat", {"timestamp": now(), "started_at": iso(self.started), "owner": self.owner, "uptime_seconds": round(now() - self.started), "active_batch": self.active_lease["batch"]["batch_id"] if self.active_lease else None})
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
                    self.s.queue.recover(con, cfg["queue"]["max_attempts"])
                    if cfg["platform"]["live_enabled"]:
                        for row in con.execute("SELECT cc.* FROM collection_cursors cc JOIN accounts a ON a.account_id=cc.account_id WHERE a.status NOT IN ('PAUSED','DEMO') AND (cc.next_poll IS NULL OR cc.next_poll<=?)", (now(),)).fetchall():
                            object_id = canonical({"account_id": row["account_id"], "job_id": row["job_id"], "channel": row["channel"], "page": row["page"]})
                            self.s.queue.enqueue(con, "collect", object_id, "collect:" + digest_cursor(row))
                            con.execute("UPDATE collection_cursors SET next_poll=? WHERE account_id=? AND job_id=? AND channel=?", (now() + cfg["platform"]["poll_seconds"], row["account_id"], row["job_id"], row["channel"]))
            except Exception:
                self.s.db.set_setting("scheduler_error", {"code": "SCHEDULER_ERROR", "timestamp": now()})
            self.stop_event.wait(2)

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
                    con.execute("UPDATE collection_cursors SET page=?,last_poll=?,next_poll=? WHERE account_id=? AND job_id=? AND channel=?", (result["next_page"], now(), now() + (30 if result["has_more"] else cfg["platform"]["poll_seconds"]), cursor["account_id"], cursor["job_id"], cursor["channel"]))
            except DomainError as error:
                pause = error.code in STOP_CODES or error.code in {"FIELD_MAPPING_REQUIRED", "CAPABILITY_UNAVAILABLE", "IDENTITY_UNVERIFIED"}
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


def digest_cursor(row):
    # last_poll distinguishes periodic scans, while concurrent schedulers share the same key.
    from .common import digest
    return digest({k: row[k] for k in ("account_id", "job_id", "channel", "page", "last_poll")})
