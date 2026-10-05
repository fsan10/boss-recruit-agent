from __future__ import annotations

import json

from .common import DomainError, canonical, digest, now
from .db import Database
from .platforms import BossAgentCliAdapter, opaque


class ActionBroker:
    def __init__(self, service, adapter=None):
        self.s = service
        self.adapter = adapter or BossAgentCliAdapter(service)

    def resolve(self, action_id, candidate_id, job_id):
        action = self.s.db.one("SELECT * FROM action_targets WHERE action_id=?", (action_id,))
        if not action or action["candidate_id"] != candidate_id or action["job_id"] != job_id:
            raise DomainError("IDENTITY_UNVERIFIED", "动作与当前候选人或岗位不匹配。", status=403)
        if action["kind"] == "resume_view":
            return {"kind": "local_resume", "url": f"/#candidate/{candidate_id}?job={job_id}", "candidate_id": candidate_id, "job_id": job_id}
        if self.s.is_synthetic(candidate_id):
            raise DomainError("DEMO_ACTION_UNAVAILABLE", "合成演示数据没有真实投递或沟通入口。")
        if action["state"] == "unavailable":
            raise DomainError("ACTION_UNAVAILABLE", "尚无投递记录。" if action["kind"] == "application_view" else "尚无可核验会话。")
        locator = self.adapter.refresh_locator(action)
        if action["kind"] == "chat_open":
            friend = opaque(locator.get("friend_id"))
            data = self.adapter.call(["hr", "chatmsg", friend], action["account_id"])["data"]
            return {"kind": "local_chat", "candidate_id": candidate_id, "job_id": job_id, "data": data}
        return {"kind": "verified_application_record", "candidate_id": candidate_id, "job_id": job_id,
                "message": "已核验投递记录；当前平台无已验证永久深链接，可在官方招聘端查看。"}

    def execute(self, item_id):
        if not self.s.config["actions"]["send_enabled"]:
            raise DomainError("SEND_DISABLED", "外部发送尚未启用。")
        item = self.s.db.one("SELECT * FROM outbox WHERE item_id=?", (item_id,))
        if not item or item["state"] != "APPROVED" or not item["approved_until"] or item["approved_until"] <= now():
            raise DomainError("APPROVAL_REQUIRED", "需要审核具体候选人、文本及有效期。", status=409)
        if digest({"text": item["text"], "candidate_id": item["candidate_id"], "job_id": item["job_id"]}) != item["text_hash"]:
            raise DomainError("APPROVAL_SCOPE_CHANGED", "批准后内容发生变化。", status=409)
        action = self.s.db.one("SELECT * FROM action_targets WHERE action_id=?", (item["action_id"],))
        locator = self.adapter.refresh_locator(action)
        friend = opaque(locator.get("friend_id"))
        with self.s.db.transaction() as con:
            changed = con.execute("UPDATE outbox SET state='SENDING',sent_at=? WHERE item_id=? AND state='APPROVED' AND approved_until>?", (now(), item_id, now())).rowcount
            if not changed:
                raise DomainError("APPROVAL_REQUIRED", "此消息已经执行或批准已过期。", status=409)
            Database.audit(con, item_id, "message_send_started", {"text_hash": item["text_hash"]})
        try:
            self.adapter.call(["hr", "reply", "--", friend, item["text"]], action["account_id"], write=True)
        except Exception as error:
            with self.s.db.transaction() as con:
                con.execute("UPDATE outbox SET state='OUTCOME_UNKNOWN',error=? WHERE item_id=?", (canonical({"code": "ACTION_OUTCOME_UNKNOWN", "message": "写请求已开始，结果未确认；禁止自动重发。"}), item_id))
            raise DomainError("ACTION_OUTCOME_UNKNOWN", "发送结果未确认，请核验正确会话，禁止自动重发。") from error
        with self.s.db.transaction() as con:
            con.execute("UPDATE outbox SET state='ACKNOWLEDGED' WHERE item_id=?", (item_id,))
        # A CLI acknowledgement is not verification. Keep the visible state until exact message evidence exists.
        return {"state": "ACKNOWLEDGED", "message": "平台返回接受；还需在正确会话确认内容与平台消息 ID。"}

    def verify_message(self, item_id, platform_message_id):
        item = self.s.db.one("SELECT * FROM outbox WHERE item_id=?", (item_id,))
        if not item or item["state"] not in {"ACKNOWLEDGED", "OUTCOME_UNKNOWN"}:
            raise DomainError("NOT_VERIFIABLE", "此消息不处于待核验状态。")
        action = self.s.db.one("SELECT * FROM action_targets WHERE action_id=?", (item["action_id"],))
        locator = self.adapter.refresh_locator(action)
        data = self.adapter.call(["hr", "chatmsg", opaque(locator.get("friend_id"))], action["account_id"])["data"]
        messages = data.get("messages", []) if isinstance(data, dict) else []
        match = next((m for m in messages if str(m.get("msgId")) == str(platform_message_id) and m.get("text") == item["text"]
                      and str(m.get("fromUid")) == self.s.config["platform"]["expected_account_id"]
                      and str(m.get("toUid")) == str(locator["platform_id"])
                      and isinstance(m.get("timestamp"), (int, float)) and abs(m["timestamp"] - item["sent_at"]) < 300), None)
        if not match:
            raise DomainError("MESSAGE_NOT_VERIFIED", "未找到目标、内容、发送方和时间均吻合的消息；状态保持待核验。")
        with self.s.db.transaction() as con:
            con.execute("UPDATE outbox SET state='VERIFIED',platform_message_id=? WHERE item_id=?", (str(platform_message_id), item_id))
            Database.audit(con, item_id, "message_verified", {"platform_message_id": str(platform_message_id)})
        return {"state": "VERIFIED"}
