from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BINDINGS = ("job_id", "jd_version", "rubric_version", "scorer_profile_version")


def uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def now() -> float:
    return time.time()


def iso(value: float | None = None) -> str:
    return datetime.fromtimestamp(now() if value is None else value, timezone.utc).isoformat().replace("+00:00", "Z")


def canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(obj) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class DomainError(Exception):
    def __init__(self, code: str, message: str, *, retryable=False, status=400, details=None):
        super().__init__(message)
        self.code, self.message, self.retryable, self.status, self.details = code, message, retryable, status, details

    def as_dict(self):
        result = {"code": self.code, "message": self.message, "retryable": self.retryable,
                  "resume_action": "查看工作台任务详情", "request_id": uid("request")}
        if self.details is not None:
            result["details"] = self.details
        return result
