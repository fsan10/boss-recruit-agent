from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .common import ROOT, BINDINGS, DomainError, canonical, digest, iso, now, read_json, uid
from .db import Database
from .resumes import make_snapshot

STOP_CODES = {"AUTH_REQUIRED", "AUTH_EXPIRED", "TOKEN_REFRESH_FAILED", "ACCOUNT_RISK", "ENVIRONMENT_RISK", "PLATFORM_LIMIT", "GREET_LIMIT", "BUDGET_EXHAUSTED"}
NETWORK_CODES = {"NETWORK_ERROR", "NETWORK_TIMEOUT"}


def at_path(value, path):
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def opaque(value):
    result = str(value) if value is not None else ""
    if not re.fullmatch(r"[A-Za-z0-9_+=.:/-]{1,2000}", result) or result.startswith("-"):
        raise DomainError("FIELD_MAPPING_REQUIRED", "缺少有效的稳定 ID 或平台定位参数。")
    return result


def resume_text(parsed):
    """Allowlist upstream structured fields. Demographics, names and school/company brands stay out."""
    parts = []
    for w in parsed.get("work_experience", []):
        parts.append("工作任务：" + "；".join(str(w[k]) for k in ("position", "start", "end", "responsibility", "performance") if w.get(k)))
    for p in parsed.get("project_experience", []):
        parts.append("项目实践：" + "；".join(str(p[k]) for k in ("name", "role", "start", "end", "description", "achievement") if p.get(k)))
    certs = parsed.get("certifications") or []
    if certs:
        parts.append("资质自述：" + "；".join(map(str, certs)))
    if not parts:
        raise DomainError("RESUME_PARTIAL", "结构化响应没有完整的工作或项目正文；未将卡片摘要冒充简历。")
    return "\n\n".join(parts)


class BossAgentCliAdapter:
    def __init__(self, service):
        self.s = service
        self.config = service.config["platform"]
        self.mapping = read_json(ROOT / "config" / "boss-field-map.json")

    def argv(self, args):
        exe = shutil.which(self.config["executable"])
        if not exe:
            raise DomainError("CAPABILITY_UNAVAILABLE", "Boss CLI 尚未安装；请按锁定提交安装并填写可执行路径。")
        argv = [exe, "--json"]
        if self.config["data_dir"]:
            argv += ["--data-dir", self.config["data_dir"]]
        return argv + args

    def call(self, args, account_id=None, *, offline=False, write=False):
        argv = self.argv(args)
        lock = uid("platform-lock")
        if not offline:
            if not self.config["live_enabled"]:
                raise DomainError("PLATFORM_DISABLED", "实时采集尚未启用。")
            if account_id:
                account = self.s.db.one("SELECT * FROM accounts WHERE account_id=?", (account_id,))
                if not account or account["status"] in {"PAUSED", "DEMO"}:
                    raise DomainError("ACCOUNT_PAUSED", "账号已暂停或未配置。")
            self.s.reserve_budget("platform_cli_operations", self.config["operation_budget_per_day"])
            with self.s.db.transaction() as con:
                existing = self.s.db.setting("platform_lock")
                if existing and existing["until"] > now():
                    raise DomainError("PLATFORM_BUSY", "同一账号正在执行另一项操作。", retryable=True)
                self.s.db.set_setting("platform_lock", {"token": lock, "until": now() + 75}, con)
                previous = self.s.db.setting("platform_last_op", 0)
            # Delay is bounded (at most configured three seconds in the default policy).
            wait = max(0, self.config["min_read_interval_seconds"] - (now() - previous))
            if wait:
                time.sleep(min(wait, 30))
        try:
            result = subprocess.run(argv, capture_output=True, timeout=60, check=False)
            if len(result.stdout) > 20 * 1024 * 1024:
                raise DomainError("OUTPUT_INVALID", "Boss CLI 返回过大响应。")
            try:
                envelope = json.loads(result.stdout)
            except ValueError:
                raise DomainError("OUTPUT_INVALID", "Boss CLI 没有返回 JSON 信封，请核验 --json 与版本。")
            if not envelope.get("ok"):
                error = envelope.get("error") or {}
                code = error.get("code", "PLATFORM_ERROR")
                raise DomainError(code, "Boss CLI 操作未完成；请查看诊断或在官方页面处理。", retryable=code in NETWORK_CODES,
                                  details={"operation": args[:2]})
            if result.returncode != 0:
                raise DomainError("ACTION_OUTCOME_UNKNOWN" if write else "PLATFORM_ERROR", "CLI 退出异常，操作结果尚未确认。", retryable=not write)
            return envelope
        except subprocess.TimeoutExpired:
            raise DomainError("ACTION_OUTCOME_UNKNOWN" if write else "NETWORK_TIMEOUT", "Boss CLI 操作超时。", retryable=not write)
        finally:
            if not offline:
                with self.s.db.transaction() as con:
                    current = self.s.db.setting("platform_lock")
                    if current and current["token"] == lock:
                        self.s.db.set_setting("platform_lock", None, con)
                        self.s.db.set_setting("platform_last_op", now(), con)

    def probe(self):
        exe = shutil.which(self.config["executable"])
        if not exe:
            return {"available": False, "error": "CAPABILITY_UNAVAILABLE", "live_test": "not_run", "pinned_commit": self.config["pinned_commit"]}
        result = {"available": True, "pinned_commit": self.config["pinned_commit"], "live_test": "not_run", "field_mapping": "requires_account_fixture"}
        try:
            version = subprocess.run([exe, "--version"], capture_output=True, timeout=10)
            result["version"] = version.stdout.decode(errors="replace").strip()[:160]
            schema = self.call(["schema"], offline=True)
            doctor = self.call(["doctor"], offline=True)
            (self.s.runtime / "capabilities").mkdir(exist_ok=True)
            (self.s.runtime / "capabilities" / "boss-schema.json").write_text(canonical(schema), encoding="utf-8")
            # Persist field names and truth flags, not credential locations or arbitrary diagnostic values.
            result["doctor_fields"] = list((doctor.get("data") or {}).keys())
            result["schema_saved"] = True
            result["account_identity"] = "status CLI exposes name, not stable account ID; writes require verified mapping"
        except DomainError as error:
            result["error"] = error.code
        return result

    def verify_account(self, account_id):
        expected = self.config["expected_account_id"]
        if not expected:
            raise DomainError("IDENTITY_UNVERIFIED", "请填写预期平台账号 ID，不能用姓名核验账号。")
        data = self.call(["me", "--section", "user"], account_id)["data"]
        current = at_path(data, self.mapping["current_account_path"])
        if current is None:
            raise DomainError("CAPABILITY_UNAVAILABLE", "当前 Boss CLI 的用户响应缺少稳定账号 ID。只读采集可继续；平台动作需新增已核验账号映射。")
        if str(current) != expected:
            raise DomainError("IDENTITY_UNVERIFIED", "当前登录账号与绑定账号不一致。")
        return expected

    def collect(self, account_id, job_id, channel="applications", page=1, offset=0):
        job = self.s.get_job(job_id)
        if job["is_synthetic"] or not job["active"] or not job["platform_job_id"]:
            raise DomainError("JOB_NOT_READY", "真实采集需要已审核启用的真实岗位与平台职位 ID。")
        if channel not in {"applications", "existing_chat", "recommendation"}:
            raise DomainError("NOT_SUPPORTED", "首版支持投递、已有沟通和显式启用的推荐采集。")
        if channel == "recommendation" and not self.config["active_sourcing_enabled"]:
            raise DomainError("SOURCING_DISABLED", "主动寻源未启用。")
        if self.s.status()["pending"] >= self.s.config["queue"]["pending_resume_backpressure"]:
            raise DomainError("BACKPRESSURE", "待评估积压达到上限，暂停读取新详情。", retryable=True)
        # Read-only collection binds an owned local CLI profile; remote writes have a stricter account-ID gate.
        status = self.call(["status", "--live"], account_id)["data"]
        if not status.get("logged_in"):
            raise DomainError("AUTH_REQUIRED", "请先在 Boss CLI 完成招聘账号登录。")
        command = {"applications": "applications", "existing_chat": "chat", "recommendation": "recommendations"}[channel]
        platform_job = opaque(job["platform_job_id"])
        envelope = self.call(["hr", command, "--job-id", platform_job, "--page", str(page)], account_id)
        data, mapping = envelope.get("data") or {}, self.mapping[channel]
        records = at_path(data, mapping["list_path"])
        if not isinstance(records, list):
            raise DomainError("FIELD_MAPPING_REQUIRED", "候选人列表字段与当前配置不同，请按脱敏响应调整字段映射。")
        imported, errors = [], []
        for record in records[offset:offset+10]:
            try:
                pid = opaque(at_path(record, mapping["platform_id"]))
                geek = opaque(at_path(record, mapping["geek_id"]))
                security = opaque(at_path(record, mapping["security_id"]))
                actual_job = at_path(record, mapping["job_id"])
                if actual_job is not None and str(actual_job) != platform_job:
                    raise DomainError("IDENTITY_UNVERIFIED", "候选人记录关联的职位与当前岗位不一致。")
                existing = self.s.db.one("SELECT candidate_id FROM candidates WHERE account_id=? AND platform_id=?", (account_id, pid))
                cid = existing["candidate_id"] if existing else uid("candidate")
                # Already-seen people are not charged as newly fetched resumes on every poll.
                if existing and self.s.db.one("SELECT 1 FROM candidate_jobs WHERE candidate_id=? AND job_id=?", (cid, job_id)):
                    continue
                self.s.reserve_budget("new_resume_details", self.config["new_resume_budget_per_day"])
                parsed = self.call(["hr", "resume", geek, "--job-id", platform_job, "--security-id", security], account_id)["data"]
                text = resume_text(parsed)
                locator = {"platform_id": pid, "geek_id": geek, "security_id": security, "platform_job_id": platform_job,
                           "friend_id": at_path(record, mapping["friend_id"]), "page": page, "channel": channel, "observed_at": iso()}
                candidate = {"candidate_id": cid, "candidate_label": str(at_path(record, mapping["label"]) or "平台候选人"),
                             "account_id": account_id, "source_channel": channel, "discovered_at": iso(), "snapshot": make_snapshot(text),
                             "actions": [{"action_id": uid("action"), "kind": k, "state": "available" if k == "resume_view" else "unverified" if k == "chat_open" or channel == "applications" else "unavailable", "handle_url": None, "observed_platform_url": None, "last_verified_at": None} for k in ("resume_view", "application_view", "chat_open")]}
                batch = {"schema_version": "1.0", "batch_id": uid("platform-import"), "parent_batch_id": None,
                         **{k: job[k] for k in BINDINGS}, "mode": "live", "created_at": iso(), "candidates": [candidate]}
                self.s.import_batch(batch, f"platform:{account_id}:{job_id}:{pid}", verified_identity={cid: pid}, locators={cid: locator})
                imported.append(cid)
            except DomainError as error:
                errors.append({"code": error.code, "message": error.message})
                if error.code in STOP_CODES:
                    raise
        # More than ten records need a continuation; do not silently skip the unprocessed tail.
        blocking = next((e for e in errors if e["code"] in {"FIELD_MAPPING_REQUIRED", "IDENTITY_UNVERIFIED"}), None)
        if not imported and blocking:
            raise DomainError(blocking["code"], blocking["message"])
        has_more = bool(data.get("hasMore") or (envelope.get("pagination") or {}).get("has_more"))
        tail = offset + 10 < len(records)
        with self.s.db.transaction() as con:
            con.execute("UPDATE accounts SET status='CONNECTED_READ_ONLY',last_success=?,pause_reason=NULL WHERE account_id=?", (now(), account_id))
            Database.audit(con, account_id, "platform_read", {"imported": len(imported), "errors": errors, "channel": channel, "page": page})
        return {"imported": imported, "errors": errors, "has_more": has_more or tail,
                "next_page": page if tail else page+1 if has_more else 1, "next_offset": offset+10 if tail else 0}

    def refresh_locator(self, action):
        self.verify_account(action["account_id"])
        locator = json.loads(action["locator"])
        if not locator.get("platform_id"):
            raise DomainError("IDENTITY_UNVERIFIED", "入口没有已核验的平台身份。")
        job = self.s.get_job(action["job_id"])
        channel = locator["channel"]
        command = {"applications": "applications", "existing_chat": "chat", "recommendation": "recommendations"}[channel]
        # Refresh the observed page and nearby pages by stable ID only, never by name/index.
        for page in sorted({1, max(1, locator.get("page", 1)-1), locator.get("page", 1), locator.get("page", 1)+1}):
            data = self.call(["hr", command, "--job-id", opaque(job["platform_job_id"]), "--page", str(page)], action["account_id"])["data"]
            mapping = self.mapping[channel]
            for row in at_path(data, mapping["list_path"]) or []:
                if str(at_path(row, mapping["platform_id"])) == locator["platform_id"]:
                    actual_job = at_path(row, mapping["job_id"])
                    if actual_job is not None and str(actual_job) != job["platform_job_id"]:
                        raise DomainError("IDENTITY_UNVERIFIED", "刷新入口时职位不一致。")
                    locator.update(geek_id=opaque(at_path(row, mapping["geek_id"])), security_id=opaque(at_path(row, mapping["security_id"])), friend_id=at_path(row, mapping["friend_id"]), page=page, observed_at=iso())
                    with self.s.db.transaction() as con:
                        con.execute("UPDATE action_targets SET locator=?,verified_at=?,expires_at=?,state='available' WHERE action_id=?", (canonical(locator), now(), now()+300, action["action_id"]))
                    return locator
        with self.s.db.transaction() as con:
            con.execute("UPDATE action_targets SET state='expired' WHERE action_id=?", (action["action_id"],))
        raise DomainError("ACTION_EXPIRED", "没有在刷新结果中找到同一候选人，请在官方页面核验。")

    def download_attachment(self, action, attachment_message_id):
        mid = opaque(attachment_message_id)
        import_key = "attachment:" + action["candidate_id"] + ":" + mid
        previous = self.s.db.one("SELECT result FROM imports WHERE import_key=?", (import_key,))
        if previous:
            return {**json.loads(previous["result"]), "replayed": True, "attachment_message_id": mid}
        locator = self.refresh_locator(action)
        friend = opaque(locator.get("friend_id"))
        history = self.call(["hr", "chatmsg", friend], action["account_id"])["data"]
        messages = history.get("messages", []) if isinstance(history, dict) else []
        message = next((m for m in messages if str(m.get("mid") or m.get("msgId")) == mid), None)
        body = (message or {}).get("body", {})
        link = body.get("hyperLink", {}) if isinstance(body, dict) else {}
        if link.get("hyperLinkType") not in {1, 9}:
            raise DomainError("ATTACHMENT_NOT_SHARED", "该消息不是已共享附件卡片；不能把简历请求消息当作附件。")
        filename = link.get("fileName") or body.get("fileName") or message.get("fileName") or ""
        extension = Path(str(filename)).suffix.lower()
        if extension not in {".pdf", ".docx", ".png", ".jpg", ".jpeg"}:
            raise DomainError("FIELD_MAPPING_REQUIRED", "附件卡片缺少可核验文件类型，需校准当前响应字段。")
        folder = self.s.runtime / "attachments"
        folder.mkdir(mode=0o700, exist_ok=True)
        output = folder / (action["candidate_id"] + "-" + uid("attachment") + extension)
        self.call(["hr", "download-resume", friend, "--message-id", mid, "--output", str(output)], action["account_id"])
        if not output.is_file() or output.is_symlink():
            raise DomainError("ATTACHMENT_DOWNLOAD_FAILED", "CLI 未生成预期的本地文件。")
        from .resumes import parse_file, MAX_FILE_SIZE
        with output.open("rb") as stream:
            content = stream.read(MAX_FILE_SIZE + 1)
        parsed = parse_file(content, output.name)
        candidate = self.s.batch_candidate(action["candidate_id"], action["job_id"])
        candidate["snapshot"] = make_snapshot(parsed["text"], parsed["completeness"], parsed["issues"], parsed["pages"])
        job = self.s.get_job(action["job_id"])
        batch = {"schema_version":"1.0","batch_id":uid("attachment-import"),"parent_batch_id":None,**{k:job[k] for k in BINDINGS},"mode":"live","created_at":iso(),"candidates":[candidate]}
        result = self.s.import_batch(batch,import_key,verified_identity={action["candidate_id"]:locator["platform_id"]},locators={action["candidate_id"]:locator})
        return {**result,"attachment_message_id":mid,"completeness":parsed["completeness"]}


class BrowserSessionProvider:
    """Extension boundary for a dedicated, user-owned browser profile. No generic scraping guesses."""
    def __init__(self, profile_dir: Path):
        self.profile_dir = Path(profile_dir).resolve()

    async def open(self):
        from playwright.async_api import async_playwright
        manager = await async_playwright().start()
        try:
            context = await manager.chromium.launch_persistent_context(str(self.profile_dir), headless=False)
            return manager, context
        except BaseException:
            await manager.stop()
            raise


class BrowserAdapter:
    """Read-only extraction from an already verified resume element, never an account/selector guess."""
    async def extract_visible_resume(self, page, selector, verified_candidate_id, visible_candidate_id, reached_bottom):
        if not verified_candidate_id or verified_candidate_id != visible_candidate_id:
            raise DomainError("IDENTITY_UNVERIFIED", "页面候选人 ID 与记录不一致。")
        element = page.locator(selector)
        if await element.count() != 1:
            raise DomainError("CAPABILITY_UNAVAILABLE", "简历容器选择器失效。")
        text = await element.inner_text()
        return make_snapshot(text, "full_text" if reached_bottom else "partial", [] if reached_bottom else ["PAGE_BOTTOM_NOT_VERIFIED"])
