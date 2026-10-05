from __future__ import annotations

import copy
import csv
import io
import json
import re
from datetime import datetime, time as day_time, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .common import ROOT, BINDINGS, DomainError, canonical, digest, iso, now, read_json, uid
from .db import Database
from .queue import Queue
from .resumes import clean_import_snapshot, make_snapshot
from .scoring import assess_envelope, calculate_score, validate_job, validate_schema, validate_member


def evaluation_key(job, candidate):
    return digest({**{k: job[k] for k in BINDINGS}, "candidate_id": candidate["candidate_id"],
                   "snapshot_sha256": candidate["snapshot"]["snapshot_sha256"]})


class Service:
    def __init__(self, runtime: Path):
        self.runtime = Path(runtime).resolve()
        self.db = Database(self.runtime / "recruit.sqlite3")
        self.queue = Queue(self.db)
        if self.db.setting("config") is None:
            self.db.set_setting("config", read_json(ROOT / "config" / "default.json"))

    @property
    def config(self):
        return self.db.setting("config")

    def profile_version(self, config=None):
        h = (config or self.config)["harness"]
        relevant = {k: v for k, v in h.items() if k not in {"enabled", "max_runs_per_day", "allowed_for_real_resumes", "timeout_seconds"}}
        return "scorer-" + digest({"harness": relevant, "prompt": (ROOT / "prompts" / "evaluate_batch.md").read_text(encoding="utf-8"),
                                  "schema": read_json(ROOT / "contracts" / "evaluation.schema.json"), "score": "decimal-v1"})[:18]

    def update_config(self, config):
        # Whitelist the shape rather than storing arbitrary executable argument injection.
        default = read_json(ROOT / "config" / "default.json")
        if not isinstance(config, dict) or set(config) != set(default) or any(
            not isinstance(config[k], dict) or set(config[k]) != set(default[k])
            for k in default if isinstance(default[k], dict)
        ):
            raise DomainError("INPUT_INVALID", "配置字段不完整或含未知字段。")
        def typed(actual, expected):
            if isinstance(expected, dict):
                return isinstance(actual, dict) and set(actual) == set(expected) and all(typed(actual[k], v) for k, v in expected.items())
            if isinstance(expected, bool):
                return type(actual) is bool
            if isinstance(expected, int):
                return type(actual) is int
            if isinstance(expected, float):
                return type(actual) in {int, float}
            return isinstance(actual, type(expected))
        if not typed(config, default):
            raise DomainError("INPUT_INVALID", "配置中的类型不合法。")
        h, p, q = config["harness"], config["platform"], config["queue"]
        if h["primary"] not in {"dsh", "codex", "opencode", "mcp"}:
            raise DomainError("INPUT_INVALID", "请选择 DSH、Codex、OpenCode 或 MCP。")
        if not 1 <= q["batch_size"] <= 10 or not 30 <= q["lease_seconds"] <= 3600 or not 1 <= q["max_attempts"] <= 3:
            raise DomainError("INPUT_INVALID", "批次、租约或重试设置越界。")
        if q["heartbeat_seconds"] >= q["lease_seconds"] / 2 or q["heartbeat_seconds"] < 1:
            raise DomainError("INPUT_INVALID", "续租间隔应短于租约的一半。")
        if not 1 <= h["max_runs_per_day"] <= 1000 or not 15 <= h["timeout_seconds"] <= 3600 or h["context_tokens"] < 8000:
            raise DomainError("INPUT_INVALID", "评分预算、上下文或超时配置不合法。")
        if not 0 < h["context_budget_ratio"] <= .7 or not 3 <= p["min_read_interval_seconds"] <= 30 or not 30 <= p["poll_seconds"] <= 86400:
            raise DomainError("INPUT_INVALID", "上下文比例或采集间隔不合法。")
        if not 1 <= p["operation_budget_per_day"] <= 300 or not 1 <= p["new_resume_budget_per_day"] <= 60:
            raise DomainError("INPUT_INVALID", "平台初版日预算上限为 300 次操作、60 份新详情。")
        try:
            ZoneInfo(config["timezone"])
        except Exception:
            raise DomainError("INPUT_INVALID", "时区配置无效。")
        with self.db.transaction() as con:
            self.db.set_setting("config", config, con)
            self.db.set_setting("harness_pause", None, con)
            con.execute("UPDATE tasks SET state='RETRY',next_run_at=? WHERE kind IN ('score','rubric') AND state='BLOCKED' AND error NOT LIKE '%CONTEXT_LIMIT%'", (now(),))
            Database.audit(con, "config", "config_updated", {"profile_version": self.profile_version(config)}, "workbench")
        return {"config": config, "profile_version": self.profile_version(config)}

    def get_job(self, job_id):
        row = self.db.one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if not row:
            raise DomainError("NOT_FOUND", "岗位不存在。", status=404)
        return {**json.loads(row["profile"]), "active": bool(row["active"]), "platform_job_id": row["platform_job_id"]}

    def create_job(self, profile, platform_job_id=None):
        profile = copy.deepcopy(profile)
        for field in ("active", "platform_job_id", "clarifications"):
            profile.pop(field, None)
        validate_job(profile)
        with self.db.transaction() as con:
            if con.execute("SELECT 1 FROM jobs WHERE job_id=?", (profile["job_id"],)).fetchone():
                raise DomainError("ID_CONFLICT", "岗位 ID 已存在。", status=409)
            con.execute("INSERT INTO jobs(job_id,profile,platform_job_id,created_at) VALUES (?,?,?,?)",
                        (profile["job_id"], canonical(profile), platform_job_id, now()))
            self.save_rubric(con, profile)
            Database.audit(con, profile["job_id"], "job_created", {"synthetic": profile["is_synthetic"]}, "workbench")
        return self.get_job(profile["job_id"])

    @staticmethod
    def save_rubric(con, profile):
        con.execute("INSERT OR IGNORE INTO rubrics(job_id,jd_version,rubric_version,scorer_profile_version,profile,created_at) VALUES (?,?,?,?,?,?)",
                    (*[profile[k] for k in BINDINGS], canonical(profile), now()))

    def activate_job(self, job_id, profile=None, actor="workbench", platform_job_id=None):
        current = self.get_job(job_id)
        clean = {k: v for k, v in (profile or current).items() if k not in {"active", "platform_job_id", "clarifications"}}
        if clean["job_id"] != job_id:
            raise DomainError("ID_CONFLICT", "岗位 ID 不可修改。")
        changed = any(clean[k] != current[k] for k in ("criteria", "hard_gates", "review_policy", "jd_text", "title"))
        if changed:
            clean["rubric_version"] = uid("rubric")
            if clean["jd_text"] != current["jd_text"]:
                clean["jd_version"] = uid("jd")
        clean["scorer_profile_version"] = self.profile_version()
        clean["rubric_status"], clean["approved_by"] = "approved", actor
        validate_job(clean)
        with self.db.transaction() as con:
            con.execute("UPDATE jobs SET profile=?,active=1,platform_job_id=? WHERE job_id=?",
                        (canonical(clean), platform_job_id or current["platform_job_id"], job_id))
            self.save_rubric(con, clean)
            superseded = con.execute("SELECT batch_id FROM evaluation_batches WHERE job_id=? AND state IN ('READY','RETRY','BLOCKED','PARTIAL') AND (json_extract(profile,'$.jd_version')<>? OR json_extract(profile,'$.rubric_version')<>? OR json_extract(profile,'$.scorer_profile_version')<>?)", (job_id, clean["jd_version"], clean["rubric_version"], clean["scorer_profile_version"])).fetchall()
            for old_batch in superseded:
                con.execute("UPDATE tasks SET state='CANCELLED' WHERE kind='score' AND object_id=? AND state!='RUNNING'", (old_batch[0],))
                con.execute("UPDATE evaluation_batches SET state='CANCELLED' WHERE batch_id=?", (old_batch[0],))
                con.execute("UPDATE batch_members SET state='SUPERSEDED' WHERE batch_id=? AND state!='COMPLETED'", (old_batch[0],))
            con.execute("DELETE FROM pending_evaluations WHERE job_id=? AND batch_id IS NULL AND (json_extract(profile,'$.jd_version')<>? OR json_extract(profile,'$.rubric_version')<>? OR json_extract(profile,'$.scorer_profile_version')<>?)", (job_id, clean["jd_version"], clean["rubric_version"], clean["scorer_profile_version"]))
            Database.audit(con, job_id, "rubric_approved", {k: clean[k] for k in BINDINGS}, actor)
        # Re-evaluate under a new version without deleting original judgments.
        for row in self.db.all("SELECT c.candidate_id FROM candidates c JOIN candidate_jobs cj ON cj.candidate_id=c.candidate_id WHERE cj.job_id=?", (job_id,)):
            candidate = self.batch_candidate(row["candidate_id"], job_id)
            self.queue_candidate(clean, candidate, "synthetic" if self.is_synthetic(candidate["candidate_id"]) else "manual")
        return self.get_job(job_id)

    def is_synthetic(self, cid):
        row = self.db.one("SELECT synthetic FROM candidates WHERE candidate_id=?", (cid,))
        return bool(row and row["synthetic"])

    def jobs(self):
        return [self.get_job(r["job_id"]) for r in self.db.all("SELECT job_id FROM jobs ORDER BY created_at DESC")]

    def ensure_account(self, con, aid, synthetic=False):
        con.execute("INSERT OR IGNORE INTO accounts(account_id,status,created_at) VALUES (?,?,?)",
                    (aid, "DEMO" if synthetic else "UNCONNECTED", now()))

    def import_batch(self, batch, import_key=None, *, verified_identity=None, locators=None, request_hash=None):
        validate_schema("batch", batch)
        job = self.get_job(batch["job_id"])
        if any(batch[k] != job[k] for k in BINDINGS):
            raise DomainError("BATCH_MISMATCH", "导入数据的岗位或规则版本不是当前版本。")
        ids = [c["candidate_id"] for c in batch["candidates"]]
        if len(set(ids)) != len(ids):
            raise DomainError("DUPLICATE_CANDIDATE", "导入批次内候选人 ID 重复。")
        if len({a["action_id"] for c in batch["candidates"] for a in c["actions"]}) != sum(len(c["actions"]) for c in batch["candidates"]):
            raise DomainError("ACTION_ID_INVALID", "不同候选人不能共用动作 ID。")
        key = import_key or batch["batch_id"]
        payload_hash = request_hash or digest(batch)
        synthetic = batch["mode"] == "synthetic"
        if synthetic != job["is_synthetic"]:
            raise DomainError("DEMO_MISMATCH", "真实与合成数据应使用各自的岗位。")
        candidates = copy.deepcopy(batch["candidates"])
        cleaned = {c["candidate_id"]: clean_import_snapshot(c["snapshot"]) for c in candidates}
        with self.db.transaction() as con:
            previous = con.execute("SELECT * FROM imports WHERE import_key=?", (key,)).fetchone()
            if previous:
                if previous["payload_hash"] != payload_hash:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "同一导入键对应不同内容。", status=409)
                return {**json.loads(previous["result"]), "replayed": True}
            for c in candidates:
                cid, aid = c["candidate_id"], c["account_id"]
                self.ensure_account(con, aid, synthetic)
                identity = (verified_identity or {}).get(cid)
                old = con.execute("SELECT * FROM candidates WHERE candidate_id=?", (cid,)).fetchone()
                if old and (old["account_id"] != aid or bool(old["synthetic"]) != synthetic):
                    raise DomainError("IDENTITY_UNVERIFIED", "已有候选人的账号或数据类型不匹配。")
                if old and identity and old["platform_id"] not in {None, identity}:
                    raise DomainError("IDENTITY_UNVERIFIED", "已有候选人的平台身份不匹配。")
                con.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET label=excluded.label",
                            (cid, aid, identity, int(bool(identity) or synthetic or batch["mode"] == "manual"), c["candidate_label"], int(synthetic), now()))
                locator = (locators or {}).get(cid, {})
                con.execute("INSERT INTO candidate_jobs(candidate_id,job_id,source_channel,locator,discovered_at) VALUES (?,?,?,?,?) ON CONFLICT(candidate_id,job_id) DO UPDATE SET locator=excluded.locator",
                            (cid, job["job_id"], c["source_channel"], canonical(locator), c["discovered_at"]))
                snapshot, raw = cleaned[cid]
                existing = con.execute("SELECT snapshot_id FROM resume_snapshots WHERE candidate_id=? AND snapshot_sha256=?", (cid, snapshot["snapshot_sha256"])).fetchone()
                if existing:
                    snapshot = json.loads(con.execute("SELECT snapshot FROM resume_snapshots WHERE snapshot_id=?", (existing["snapshot_id"],)).fetchone()[0])
                else:
                    conflicting = con.execute("SELECT candidate_id FROM resume_snapshots WHERE snapshot_id=?", (snapshot["snapshot_id"],)).fetchone()
                    if conflicting:
                        raise DomainError("ID_CONFLICT", "快照 ID 已被其他内容使用。", status=409)
                    con.execute("INSERT INTO resume_snapshots VALUES (?,?,?,?,?,?)", (snapshot["snapshot_id"], cid, snapshot["snapshot_sha256"], canonical(snapshot), raw, now()))
                c["snapshot"] = snapshot
                for action in c["actions"]:
                    kind = action["kind"]
                    old_action = con.execute("SELECT candidate_id,job_id FROM action_targets WHERE action_id=?", (action["action_id"],)).fetchone()
                    if old_action and (old_action["candidate_id"] != cid or old_action["job_id"] != job["job_id"]):
                        raise DomainError("ACTION_ID_INVALID", "动作 ID 已属于其他候选人或岗位。")
                    # Resume evidence is locally available; platform handles require live verification.
                    state = "available" if kind == "resume_view" else ("unavailable" if action["state"] == "unavailable" or synthetic else "unverified")
                    url = action.get("observed_platform_url")
                    if url:
                        parsed = urlparse(url)
                        if parsed.scheme != "https" or parsed.hostname not in {"www.zhipin.com", "zhipin.com"} or parsed.username or parsed.password:
                            url = None
                    con.execute("INSERT INTO action_targets(action_id,candidate_id,job_id,account_id,kind,state,locator,platform_url) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(candidate_id,job_id,kind) DO UPDATE SET state=excluded.state,locator=excluded.locator,platform_url=excluded.platform_url",
                                (action["action_id"], cid, job["job_id"], aid, kind, state, canonical(locator), url))
                if job["active"]:
                    self.queue_candidate(job, c, batch["mode"], con)
            result = {"import_key": key, "candidate_ids": ids, "count": len(ids), "mode": batch["mode"], "replayed": False}
            con.execute("INSERT INTO imports VALUES (?,?,?,?)", (key, payload_hash, canonical(result), now()))
            Database.audit(con, key, "resumes_imported", {"count": len(ids), "mode": batch["mode"]}, "import")
        self.flush_batches()
        return result

    def import_texts(self, job_id, rows, import_key):
        job = self.get_job(job_id)
        prior = self.db.one("SELECT result,payload_hash FROM imports WHERE import_key=?", (import_key,))
        request_hash = digest({"job_id": job_id, "rows": rows})
        if prior:
            if prior["payload_hash"] != request_hash:
                raise DomainError("IDEMPOTENCY_CONFLICT", "同一导入键内容发生变化。", status=409)
            return {**json.loads(prior["result"]), "replayed": True}
        if not 1 <= len(rows) <= 10:
            raise DomainError("INPUT_INVALID", "一次导入 1 至 10 人。")
        candidates = []
        for row in rows:
            cid = row.get("candidate_id") or uid("candidate")
            candidates.append({"candidate_id": cid, "candidate_label": row.get("label") or "待审核候选人", "account_id": "manual-account",
                               "source_channel": "manual_import", "discovered_at": iso(),
                               "snapshot": make_snapshot(row["text"], row.get("completeness", "full_text"), row.get("issues", []), row.get("pages")),
                               "actions": [{"action_id": uid("action"), "kind": k, "state": "available" if k == "resume_view" else "unverified" if row.get(k) else "unavailable",
                                            "handle_url": None, "observed_platform_url": row.get(k), "last_verified_at": None} for k in ("resume_view", "application_view", "chat_open")]})
        batch = {"schema_version": "1.0", "batch_id": uid("import"), "parent_batch_id": None,
                 **{k: job[k] for k in BINDINGS}, "mode": "synthetic" if job["is_synthetic"] else "manual", "created_at": iso(), "candidates": candidates}
        result = self.import_batch(batch, import_key, request_hash=request_hash)
        return result

    def queue_candidate(self, job, candidate, mode, con=None):
        if con is None:
            with self.db.transaction() as tx:
                return self.queue_candidate(job, candidate, mode, tx)
        key = evaluation_key(job, candidate)
        if con.execute("SELECT 1 FROM current_scores WHERE eval_key=?", (key,)).fetchone():
            return
        clean_job = {k: v for k, v in job.items() if k not in {"active", "platform_job_id", "clarifications"}}
        con.execute("INSERT OR IGNORE INTO pending_evaluations VALUES (?,?,?,?,?,?,?,NULL)",
                    (key, candidate["candidate_id"], job["job_id"], candidate["snapshot"]["snapshot_id"], canonical(clean_job), mode, now()))

    def batch_candidate(self, cid, job_id, snapshot_id=None):
        row = self.db.one("SELECT c.*,cj.source_channel,cj.discovered_at FROM candidates c JOIN candidate_jobs cj ON c.candidate_id=cj.candidate_id WHERE c.candidate_id=? AND cj.job_id=?", (cid, job_id))
        if not row:
            raise DomainError("NOT_FOUND", "该岗位没有此候选人。", status=404)
        snap = self.db.one("SELECT snapshot FROM resume_snapshots WHERE candidate_id=?" + (" AND snapshot_id=?" if snapshot_id else "") + " ORDER BY created_at DESC LIMIT 1", (cid, snapshot_id) if snapshot_id else (cid,))
        if not snap:
            raise DomainError("RESUME_EXPIRED", "评估正文已按保留期限清除。", status=410)
        return {"candidate_id": cid, "candidate_label": row["label"], "account_id": row["account_id"], "source_channel": row["source_channel"],
                "discovered_at": row["discovered_at"], "snapshot": json.loads(snap["snapshot"]), "actions": self.actions(cid, job_id)}

    def actions(self, cid, job_id):
        return [{"action_id": a["action_id"], "kind": a["kind"], "state": a["state"],
                 "handle_url": f"/#candidate/{cid}?job={job_id}", "observed_platform_url": None,
                 "last_verified_at": iso(a["verified_at"]) if a["verified_at"] else None} for a in self.db.all("SELECT * FROM action_targets WHERE candidate_id=? AND job_id=? ORDER BY kind", (cid, job_id))]

    def flush_batches(self, force=False, job_id=None):
        created = []
        with self.db.transaction() as con:
            sql = "SELECT * FROM pending_evaluations WHERE batch_id IS NULL"
            args = []
            if job_id:
                sql += " AND job_id=?"
                args.append(job_id)
            pending = [dict(r) for r in con.execute(sql + " ORDER BY queued_at,eval_key", args)]
            groups = {}
            for row in pending:
                groups.setdefault((row["profile"], row["mode"]), []).append(row)
            cfg = self.config
            for (profile_text, mode), rows in groups.items():
                profile = json.loads(profile_text)
                cap = cfg["queue"]["batch_size"]
                while rows and (force or len(rows) >= cap or now() - rows[0]["queued_at"] >= cfg["queue"]["batch_max_wait_seconds"]):
                    bid = uid("batch")
                    portion = rows[:cap]
                    del rows[:cap]
                    candidates = [self.batch_candidate(r["candidate_id"], r["job_id"], r["snapshot_id"]) for r in portion]
                    # Split without discarding any evidence when the configured context cannot hold the batch.
                    limit = int(cfg["harness"]["context_tokens"] * cfg["harness"]["context_budget_ratio"])
                    base = len(canonical(profile).encode("utf-8")) + 2200
                    buckets, current, cost = [], [], base
                    for candidate in candidates:
                        estimate = len(canonical(candidate).encode("utf-8")) + 3600
                        if current and cost + estimate > limit:
                            buckets.append(current)
                            current, cost = [], base
                        current.append(candidate)
                        cost += estimate
                    if current:
                        buckets.append(current)
                    for bucket in buckets:
                        child = uid("batch") if len(buckets) > 1 else bid
                        payload = {"schema_version": "1.0", "batch_id": child, "parent_batch_id": bid if len(buckets) > 1 else None,
                                   **{k: profile[k] for k in BINDINGS}, "mode": mode, "created_at": iso(), "candidates": bucket}
                        over = base + sum(len(canonical(c).encode("utf-8")) + 3600 for c in bucket) > limit
                        con.execute("INSERT INTO evaluation_batches VALUES (?,?,?,?,?,?,?)", (child, payload["parent_batch_id"], profile["job_id"], profile_text, canonical(payload), "BLOCKED" if over else "READY", now()))
                        for c in bucket:
                            con.execute("INSERT INTO batch_members(batch_id,candidate_id,state,error) VALUES (?,?,?,?)", (child, c["candidate_id"], "FAILED" if over else "PENDING", canonical({"code": "CONTEXT_LIMIT", "message": "单份简历超过上下文预算，请提高已核验上限或分段处理。"}) if over else None))
                            con.execute("UPDATE pending_evaluations SET batch_id=? WHERE eval_key=?", (child, evaluation_key(profile, c)))
                        task = self.queue.enqueue(con, "score", child, "score:" + child)
                        if over:
                            con.execute("UPDATE tasks SET state='BLOCKED',error=? WHERE task_id=?", (canonical({"code": "CONTEXT_LIMIT", "message": "完整输入超过配置预算。"}), task["task_id"]))
                        created.append(child)
        return created

    def claim_batch(self, owner="mcp", job_id=None, batch_id=None, force=True):
        if force:
            self.flush_batches(True, job_id)
        if job_id and not batch_id:
            selected = self.db.one("SELECT t.object_id FROM tasks t JOIN evaluation_batches b ON b.batch_id=t.object_id WHERE t.kind='score' AND b.job_id=? AND (t.state IN ('READY','RETRY') OR (t.state='RUNNING' AND t.lease_until<=?)) AND t.next_run_at<=? ORDER BY t.created_at LIMIT 1", (job_id, now(), now()))
            if not selected:
                return {"batch": None}
            batch_id = selected["object_id"]
        task = self.queue.claim("score", owner, self.config["queue"]["lease_seconds"], batch_id, self.config["queue"]["max_attempts"])
        if not task:
            return {"batch": None, "message": "当前没有可领取批次；正在处理的批次需要原调用者续租。"}
        row = self.db.one("SELECT * FROM evaluation_batches WHERE batch_id=?", (task["object_id"],))
        if job_id and row["job_id"] != job_id:
            self.queue.fail(task, {"code": "JOB_MISMATCH", "message": "此批次属于其他岗位。"}, delay=0)
            return {"batch": None}
        batch = json.loads(row["payload"])
        done = {r["candidate_id"] for r in self.db.all("SELECT candidate_id FROM batch_members WHERE batch_id=? AND state='COMPLETED'", (row["batch_id"],))}
        batch["candidates"] = [c for c in batch["candidates"] if c["candidate_id"] not in done]
        with self.db.transaction() as con:
            con.execute("UPDATE evaluation_batches SET state='RUNNING' WHERE batch_id=?", (row["batch_id"],))
        # Identity labels and external locators are intentionally excluded from the model prompt.
        return {"task_id": task["task_id"], "lease_token": task["lease_token"], "fencing_token": task["fencing_token"],
                "lease_until": task["lease_until"], "attempt": task["attempts"], "batch": batch, "job": json.loads(row["profile"]),
                "evaluation_schema": read_json(ROOT / "contracts" / "evaluation.schema.json"),
                "instructions": (ROOT / "prompts" / "evaluate_batch.md").read_text(encoding="utf-8")
                .replace("{{JOB_JSON}}", "使用本次响应的 job 对象。")
                .replace("{{BATCH_JSON}}", "使用本次响应的 batch 对象。")}

    def submit(self, batch_id, token, fence, envelope, metadata=None, task_id=None):
        row = self.db.one("SELECT * FROM evaluation_batches WHERE batch_id=?", (batch_id,))
        if not row:
            raise DomainError("NOT_FOUND", "批次不存在。", status=404)
        task_row = self.db.one("SELECT * FROM tasks WHERE kind='score' AND object_id=?", (batch_id,))
        if task_id and task_row["task_id"] != task_id:
            raise DomainError("LEASE_LOST", "批次任务不匹配。", status=409)
        with self.db.transaction() as con:
            self.queue.assert_lease(con, task_row["task_id"], token, fence)
        batch, job = json.loads(row["payload"]), json.loads(row["profile"])
        done = {r["candidate_id"] for r in self.db.all("SELECT candidate_id FROM batch_members WHERE batch_id=? AND state='COMPLETED'", (batch_id,))}
        batch["candidates"] = [c for c in batch["candidates"] if c["candidate_id"] not in done]
        successes, errors = assess_envelope(job, batch, envelope)
        candidates = {c["candidate_id"]: c for c in batch["candidates"]}
        meta = metadata or {"harness": "mcp", "verification": "client_submitted_not_server_invoked"}
        with self.db.transaction() as con:
            task = self.queue.assert_lease(con, task_row["task_id"], token, fence)
            for cid, evaluation in successes.items():
                c = candidates[cid]
                identity = con.execute("SELECT identity_verified FROM candidates WHERE candidate_id=?", (cid,)).fetchone()[0]
                score = calculate_score(job, c, evaluation, bool(identity))
                key = evaluation_key(job, c)
                existing = con.execute("SELECT run_id FROM current_scores WHERE eval_key=?", (key,)).fetchone()
                run_id = existing[0] if existing else uid("run")
                if not existing:
                    con.execute("INSERT INTO evaluation_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, key, batch_id, cid, c["snapshot"]["snapshot_id"], meta.get("harness", "unknown"), canonical(meta), canonical(evaluation), canonical(score), None, None, now()))
                    con.execute("INSERT INTO current_scores VALUES (?,?)", (key, run_id))
                    if evaluation["message_draft"]:
                        self._draft(con, cid, job["job_id"], evaluation["message_draft"])
                con.execute("UPDATE batch_members SET state='COMPLETED',error=NULL,run_id=? WHERE batch_id=? AND candidate_id=?", (run_id, batch_id, cid))
                Database.audit(con, run_id, "evaluation_published", {"route": score["route"], "harness": meta.get("harness")})
            if successes and meta.get("verification") == "real_cli_execution":
                self.db.set_setting("harness_last_success", {"timestamp": now(), "batch_id": batch_id, "harness": meta.get("harness"), "profile_version": job["scorer_profile_version"]}, con)
            failed = task["attempts"] >= self.config["queue"]["max_attempts"]
            for cid, error in errors.items():
                con.execute("UPDATE batch_members SET state=?,error=? WHERE batch_id=? AND candidate_id=?", ("FAILED" if failed else "PENDING", canonical(error), batch_id, cid))
            state = ("FAILED" if failed else "RETRY") if errors else "COMPLETED"
            con.execute("UPDATE tasks SET state=?,error=?,next_run_at=?,lease_token=NULL,lease_until=NULL WHERE task_id=?", (state, canonical(errors) if errors else None, now() + (30, 120, 300)[min(task["attempts"]-1, 2)], task["task_id"]))
            con.execute("UPDATE evaluation_batches SET state=? WHERE batch_id=?", ("PARTIAL" if errors and successes else state, batch_id))
        return {"accepted": list(successes), "errors": errors, "report": self.batch_report(batch_id)}

    def batch_report(self, batch_id):
        row = self.db.one("SELECT * FROM evaluation_batches WHERE batch_id=?", (batch_id,))
        if not row:
            raise DomainError("NOT_FOUND", "批次不存在。", status=404)
        batch = json.loads(row["payload"])
        report = []
        for member in self.db.all("SELECT * FROM batch_members WHERE batch_id=? ORDER BY candidate_id", (batch_id,)):
            c = next(c for c in batch["candidates"] if c["candidate_id"] == member["candidate_id"])
            run = self.db.one("SELECT * FROM evaluation_runs WHERE run_id=?", (member["run_id"],)) if member["run_id"] else None
            if run:
                current = self.db.one("SELECT er.* FROM current_scores cs JOIN evaluation_runs er ON er.run_id=cs.run_id WHERE cs.eval_key=?", (run["eval_key"],))
                run = current or run
            report.append({"candidate_id": c["candidate_id"], "candidate_label": c["candidate_label"], "state": member["state"],
                           "error": json.loads(member["error"]) if member["error"] else None, "actions": self.actions(c["candidate_id"], batch["job_id"]),
                           "evaluation": json.loads(run["evaluation"]) if run else None, "score": json.loads(run["score"]) if run else None,
                           "harness_metadata": json.loads(run["metadata"]) if run else None,
                           "run_id": run["run_id"] if run else None})
        return {"batch_id": batch_id, "parent_batch_id": row["parent_batch_id"], **{k: batch[k] for k in BINDINGS},
                "mode": batch["mode"], "state": row["state"], "total": len(report), "completed": sum(r["state"] == "COMPLETED" for r in report), "members": report}

    def list_candidates(self, job_id=None, route=None):
        sql = "SELECT c.*,cj.job_id,cj.source_channel,cj.human_status,cj.discovered_at FROM candidates c JOIN candidate_jobs cj ON cj.candidate_id=c.candidate_id"
        rows = self.db.all(sql + (" WHERE cj.job_id=?" if job_id else "") + " ORDER BY c.created_at DESC", (job_id,) if job_id else ())
        result = []
        for c in rows:
            job = self.get_job(c["job_id"])
            candidate = self.batch_candidate(c["candidate_id"], c["job_id"])
            run = self.db.one("SELECT er.* FROM current_scores cs JOIN evaluation_runs er ON er.run_id=cs.run_id WHERE cs.eval_key=?", (evaluation_key(job, candidate),))
            score = json.loads(run["score"]) if run else None
            if route and (not score or score["route"] != route):
                continue
            pending = self.db.one("SELECT bm.state,bm.error,bm.batch_id FROM batch_members bm JOIN evaluation_batches b ON b.batch_id=bm.batch_id WHERE bm.candidate_id=? AND b.job_id=? ORDER BY b.created_at DESC LIMIT 1", (c["candidate_id"], c["job_id"]))
            result.append({"candidate_id": c["candidate_id"], "label": c["label"], "job_id": c["job_id"], "job_title": job["title"],
                           "source_channel": c["source_channel"], "human_status": c["human_status"], "synthetic": bool(c["synthetic"]),
                           "identity_verified": bool(c["identity_verified"]), "discovered_at": c["discovered_at"],
                           "completeness": candidate["snapshot"]["completeness"], "score": score, "run_id": run["run_id"] if run else None,
                           "scorer_profile_version": job["scorer_profile_version"], "evaluation_state": "COMPLETED" if run else (pending["state"] if pending else "WAITING_BATCH"),
                           "harness": run["harness"] if run else None,
                           "error": json.loads(pending["error"]) if pending and pending["error"] else None, "actions": candidate["actions"]})
        # Keep distinct rubric/harness/quality cohorts; this is a review order, never a hiring decision.
        result.sort(key=lambda r: (r["job_id"], r["scorer_profile_version"], r["score"]["route"] if r["score"] else "WAITING",
                                   -(r["score"]["lower"] if r["score"] else -1), -(r["score"]["coverage"] if r["score"] else 0), r["discovered_at"], r["candidate_id"]))
        return result

    def candidate_detail(self, cid, job_id):
        candidate = self.batch_candidate(cid, job_id)
        job = self.get_job(job_id)
        runs = self.db.all("SELECT * FROM evaluation_runs WHERE candidate_id=? ORDER BY created_at DESC", (cid,))
        runs = [r for r in runs if json.loads(self.db.one("SELECT profile FROM evaluation_batches WHERE batch_id=?", (r["batch_id"],))["profile"])["job_id"] == job_id]
        for r in runs:
            for field in ("evaluation", "score", "metadata"):
                r[field] = json.loads(r[field])
        current = self.db.one("SELECT run_id FROM current_scores WHERE eval_key=?", (evaluation_key(job, candidate),))
        for run in runs:
            run["is_current"] = bool(current and run["run_id"] == current["run_id"])
        runs.sort(key=lambda r: (r["is_current"], r["created_at"]), reverse=True)
        row = self.db.one("SELECT raw_text FROM resume_snapshots WHERE snapshot_id=?", (candidate["snapshot"]["snapshot_id"],))
        candidate.update({"job": job, "runs": runs, "raw_text": row["raw_text"], "synthetic": self.is_synthetic(cid),
                          "audit": self.db.all("SELECT actor,event,data,created_at FROM audit_events WHERE object_id IN (?,?) ORDER BY id DESC LIMIT 30", (cid, runs[0]["run_id"] if runs else cid))})
        return candidate

    def override(self, run_id, evaluation, reason, actor="workbench"):
        if not reason.strip():
            raise DomainError("REASON_REQUIRED", "人工修正需要填写理由。")
        old = self.db.one("SELECT * FROM evaluation_runs WHERE run_id=?", (run_id,))
        if not old:
            raise DomainError("NOT_FOUND", "评估不存在。", status=404)
        batch_row = self.db.one("SELECT * FROM evaluation_batches WHERE batch_id=?", (old["batch_id"],))
        job, batch = json.loads(batch_row["profile"]), json.loads(batch_row["payload"])
        c = next(c for c in batch["candidates"] if c["candidate_id"] == old["candidate_id"])
        validate_member(job, c, evaluation)
        identity = self.db.one("SELECT identity_verified FROM candidates WHERE candidate_id=?", (old["candidate_id"],))
        score = calculate_score(job, c, evaluation, bool(identity["identity_verified"]))
        new = uid("run")
        with self.db.transaction() as con:
            current = con.execute("SELECT run_id FROM current_scores WHERE eval_key=?", (old["eval_key"],)).fetchone()
            if not current or current[0] != run_id:
                raise DomainError("VERSION_CONFLICT", "已有更新的评估，请刷新后修改。", status=409)
            con.execute("INSERT INTO evaluation_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (new, old["eval_key"], old["batch_id"], old["candidate_id"], old["snapshot_id"], "human_review", canonical({"reason": reason}), canonical(evaluation), canonical(score), actor, run_id, now()))
            con.execute("UPDATE current_scores SET run_id=? WHERE eval_key=?", (new, old["eval_key"]))
            Database.audit(con, old["candidate_id"], "human_override", {"reason": reason, "previous_run_id": run_id, "run_id": new}, actor)
        return {"run_id": new, "score": score}

    def _draft(self, con, cid, job_id, text):
        action = con.execute("SELECT action_id FROM action_targets WHERE candidate_id=? AND job_id=? AND kind='chat_open'", (cid, job_id)).fetchone()
        if not action:
            return None
        h = digest({"text": text, "candidate_id": cid, "job_id": job_id})
        old = con.execute("SELECT item_id FROM outbox WHERE candidate_id=? AND job_id=? AND text_hash=?", (cid, job_id, h)).fetchone()
        if old:
            return old[0]
        item = uid("draft")
        con.execute("INSERT INTO outbox(item_id,candidate_id,job_id,action_id,text,text_hash,state,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (item, cid, job_id, action[0], text, h, "DRAFT", now()))
        return item

    def create_drafts(self, job_id, drafts):
        ids = []
        with self.db.transaction() as con:
            for d in drafts:
                if not d["text"].strip() or len(d["text"]) > 5000:
                    raise DomainError("INPUT_INVALID", "草稿内容无效。")
                self.batch_candidate(d["candidate_id"], job_id)
                ids.append(self._draft(con, d["candidate_id"], job_id, d["text"]))
        return {"item_ids": ids}

    def approve_drafts(self, approvals, actor="workbench"):
        with self.db.transaction() as con:
            for approval in approvals:
                row = con.execute("SELECT * FROM outbox WHERE item_id=?", (approval["item_id"],)).fetchone()
                if not row or row["state"] != "DRAFT" or row["text_hash"] != approval["text_hash"] or row["candidate_id"] != approval["candidate_id"]:
                    raise DomainError("APPROVAL_SCOPE_CHANGED", "草稿目标、内容或状态发生变化，请重新审核。", status=409)
                candidate = con.execute("SELECT * FROM candidates WHERE candidate_id=?", (row["candidate_id"],)).fetchone()
                if candidate["synthetic"] or not candidate["platform_id"] or not candidate["identity_verified"]:
                    raise DomainError("IDENTITY_UNVERIFIED", "此记录尚未绑定已核验的真实平台身份。")
                con.execute("UPDATE outbox SET state='APPROVED',approved_by=?,approved_until=? WHERE item_id=?", (actor, now() + self.config["actions"]["approval_expiry_hours"] * 3600, row["item_id"]))
                Database.audit(con, row["item_id"], "message_approved", {"text_hash": row["text_hash"]}, actor)
        return {"approved": len(approvals)}

    def pause_account(self, account_id, reason="用户暂停"):
        with self.db.transaction() as con:
            if not con.execute("SELECT 1 FROM accounts WHERE account_id=?", (account_id,)).fetchone():
                raise DomainError("NOT_FOUND", "账号不存在。", status=404)
            con.execute("UPDATE accounts SET status='PAUSED',pause_reason=? WHERE account_id=?", (reason, account_id))
            Database.audit(con, account_id, "account_paused", {"reason": reason}, "workbench")
        return {"status": "PAUSED"}

    def resume_account(self, account_id):
        with self.db.transaction() as con:
            con.execute("UPDATE accounts SET status='UNCONNECTED',pause_reason=NULL WHERE account_id=?", (account_id,))
            con.execute("UPDATE tasks SET state='RETRY',next_run_at=? WHERE kind='collect' AND state='BLOCKED'", (now(),))
        return {"status": "UNCONNECTED", "message": "下一次采集先核验账号身份。"}

    def reserve_budget(self, scope, maximum):
        zone = ZoneInfo(self.config["timezone"])
        today = datetime.fromtimestamp(now(), zone).date()
        day = today.isoformat()
        with self.db.transaction() as con:
            row = con.execute("SELECT count FROM daily_budgets WHERE day=? AND scope=?", (day, scope)).fetchone()
            if row and row[0] >= maximum:
                reset = datetime.combine(today + timedelta(days=1), day_time.min, zone).timestamp()
                raise DomainError("BUDGET_EXHAUSTED", "今日预算已用完，将在配置时区的次日零点恢复。",
                                  retryable=True, details={"scope": scope, "resumes_at": reset})
            con.execute("INSERT INTO daily_budgets VALUES (?,?,1) ON CONFLICT(day,scope) DO UPDATE SET count=count+1", (day, scope))

    def status(self):
        heartbeat = self.db.setting("worker_heartbeat")
        return {"server": {"version": "0.1.0", "time": iso()},
                "worker": {"heartbeat": heartbeat, "alive": bool(heartbeat and now() - heartbeat["timestamp"] < 30)},
                "harness": {"primary": self.config["harness"]["primary"], "enabled": self.config["harness"]["enabled"], "pause": self.db.setting("harness_pause"), "profile_version": self.profile_version(), "last_success": self.db.setting("harness_last_success")},
                "accounts": self.db.all("SELECT * FROM accounts"), "jobs": len(self.jobs()),
                "candidates": self.db.one("SELECT COUNT(*) n FROM candidates")["n"],
                "batch_counts": self.db.all("SELECT state,COUNT(*) n FROM evaluation_batches GROUP BY state"),
                "pending": self.db.one("SELECT COUNT(*) n FROM pending_evaluations p LEFT JOIN current_scores s ON p.eval_key=s.eval_key WHERE s.eval_key IS NULL")["n"],
                "budgets": self.db.all("SELECT * FROM daily_budgets ORDER BY day DESC LIMIT 30"),
                "collections": self.db.all("SELECT * FROM collection_cursors"),
                "live_enabled": self.config["platform"]["live_enabled"]}

    def retry_task(self, task_id):
        with self.db.transaction() as con:
            task = con.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not task or task["state"] not in {"FAILED", "BLOCKED"}:
                raise DomainError("TASK_NOT_RETRYABLE", "只可重置已失败或暂停的任务。")
            con.execute("UPDATE tasks SET state='READY',attempts=0,next_run_at=?,error=NULL WHERE task_id=?", (now(), task_id))
            if task["kind"] == "score":
                con.execute("UPDATE batch_members SET state='PENDING',error=NULL WHERE batch_id=? AND state!='COMPLETED'", (task["object_id"],))
                con.execute("UPDATE evaluation_batches SET state='READY' WHERE batch_id=?", (task["object_id"],))
            Database.audit(con, task_id, "manual_retry", actor="workbench")
        return {"state": "READY"}

    def export(self, job_id, format="csv"):
        rows = self.list_candidates(job_id)
        if format == "csv":
            stream = io.StringIO()
            writer = csv.writer(stream)
            writer.writerow(["候选人ID", "标识", "岗位", "证据下界", "可能上界", "信息覆盖率", "复核队列", "评分状态", "简历入口", "投递入口", "沟通入口", "评分版本", "证据摘要", "信息缺口", "核实问题", "三类入口状态"])
            safe = lambda value: "'" + str(value) if str(value).lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else str(value)
            for row in rows:
                score = row["score"] or {}
                actions = {a["kind"]: a for a in row["actions"]}
                e = json.loads(self.db.one("SELECT evaluation FROM evaluation_runs WHERE run_id=?", (row["run_id"],))["evaluation"]) if row["run_id"] else None
                evidence = "；".join(ref["quote"] for c in e["criteria"] for ref in c["evidence_refs"]) if e else ""
                missing = "；".join(t for c in e["criteria"] for t in c["missing_information"]) if e else ""
                questions = "；".join(e["verification_questions"]) if e else ""
                writer.writerow([safe(v) for v in [row["candidate_id"], row["label"], row["job_title"], score.get("lower", ""), score.get("upper", ""), score.get("coverage", ""), score.get("route", ""), row["evaluation_state"], *[actions.get(k, {}).get("handle_url", "") for k in ("resume_view", "application_view", "chat_open")], row["scorer_profile_version"], evidence, missing, questions, canonical({k:a["state"] for k,a in actions.items()})]])
            return "\ufeff" + stream.getvalue()
        text = ["# 招聘复核报告", "", "分数用于岗位证据复核；不是录用或淘汰决定。", ""]
        for row in rows:
            text += [f"## {row['label'].replace('#','')} · {row['candidate_id']}", f"岗位：{row['job_title']} · 规则：{row['scorer_profile_version']}", f"结果：{canonical(row['score']) if row['score'] else '尚未完成评分'}", ""]
            if row["run_id"]:
                run = self.db.one("SELECT evaluation FROM evaluation_runs WHERE run_id=?", (row["run_id"],))
                e = json.loads(run["evaluation"])
                text += [e["summary"], ""]
                for criterion in e["criteria"]:
                    text += [f"- {criterion['criterion_id']}：{criterion['rating']} · {criterion['reason']}"]
                    text += [f"  原文证据：{ref['quote']} ({ref['block_id']})" for ref in criterion["evidence_refs"]]
                text += ["待核实：" + "；".join(e["verification_questions"]), ""]
            text += [f"- {a['kind']}：{a['state']} · {a['handle_url']}" for a in row["actions"]]
            text.append("")
        return "\n".join(text)

    def delete_candidate(self, cid):
        with self.db.transaction() as con:
            if not con.execute("SELECT 1 FROM candidates WHERE candidate_id=?", (cid,)).fetchone():
                raise DomainError("NOT_FOUND", "候选人不存在。", status=404)
            if con.execute("SELECT 1 FROM batch_members bm JOIN evaluation_batches b ON b.batch_id=bm.batch_id WHERE bm.candidate_id=? AND b.state='RUNNING'", (cid,)).fetchone():
                raise DomainError("TASK_RUNNING", "请先停止评分器或等待任务结束后删除。", status=409)
            batches = list(con.execute("SELECT batch_id FROM batch_members WHERE candidate_id=?", (cid,)))
            for row in batches:
                stored = con.execute("SELECT payload FROM evaluation_batches WHERE batch_id=?", (row[0],)).fetchone()
                payload = json.loads(stored[0])
                payload["candidates"] = [c for c in payload["candidates"] if c["candidate_id"] != cid]
                con.execute("UPDATE evaluation_batches SET payload=? WHERE batch_id=?", (canonical(payload), row[0]))
            con.execute("DELETE FROM candidates WHERE candidate_id=?", (cid,))
            Database.audit(con, cid, "candidate_deleted", {"supplier_logs": "不属于本地数据库删除范围", "backup": "备份按保留期限清除"}, "workbench")
        folder = self.runtime / "attachments"
        if folder.exists():
            for attachment in folder.iterdir():
                if attachment.name.startswith(cid + "-attachment-") and attachment.is_file() and not attachment.is_symlink():
                    attachment.unlink()
        return {"deleted": True}
