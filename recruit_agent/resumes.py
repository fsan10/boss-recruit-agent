from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .common import ROOT, DomainError, digest, uid

MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_TEXT_SIZE = 600_000
PRIVATE_LINE = re.compile(r"^\s*(?:姓名|性别|年龄|出生(?:日期|年月)|婚姻|婚育|民族|宗教|健康|籍贯|家庭住址|户籍|手机|电话|微信|邮箱|照片|name|gender|sex|age|date of birth|nationality|religion|marital status|health|address|phone|email)\s*[:：]", re.I)


def redact(text: str) -> str:
    lines = [line for line in text.splitlines() if not PRIVATE_LINE.match(line)]
    result = "\n".join(lines)
    result = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[联系方式已移除]", result)
    result = re.sub(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)", "[联系方式已移除]", result)
    result = re.sub(r"https?://[^\s<>]+", "[外部链接已移除]", result)
    result = re.sub(r"(?<!\d)\d{1,3}\s*(?:岁|years old)", "[年龄已移除]", result)
    return result.strip()


def snapshot_hash(snapshot: dict) -> str:
    return digest({k: snapshot[k] for k in ("scoring_text", "evidence_blocks", "completeness", "ocr_issues")})


def make_snapshot(text: str, completeness="full_text", issues=None, pages=None, snapshot_id=None):
    if len(text) > MAX_TEXT_SIZE:
        raise DomainError("FILE_TOO_LARGE", "正文过长，请分段导入并保留完整来源。")
    sid = snapshot_id or uid("snapshot")
    blocks = []
    for page_no, page in enumerate(pages or [text], 1):
        cleaned = redact(page)
        for para in re.split(r"\n\s*\n", cleaned):
            if para.strip():
                blocks.append({"block_id": f"{sid}-B{len(blocks)+1:03}", "text": para.strip(),
                               "source_locator": f"page:{page_no}/paragraph:{len(blocks)+1}"})
    if not blocks:
        raise DomainError("RESUME_EMPTY", "没有可用于岗位评估的正文。")
    snap = {"snapshot_id": sid, "scoring_text": "\n\n".join(b["text"] for b in blocks),
            "evidence_blocks": blocks, "completeness": completeness, "ocr_issues": issues or []}
    snap["snapshot_sha256"] = snapshot_hash(snap)
    return snap


def clean_import_snapshot(snapshot: dict):
    if snapshot_hash(snapshot) != snapshot["snapshot_sha256"]:
        raise DomainError("SNAPSHOT_MISMATCH", "简历快照 hash 与内容不一致。")
    blocks = snapshot["evidence_blocks"]
    if len({b["block_id"] for b in blocks}) != len(blocks):
        raise DomainError("EVIDENCE_INVALID", "证据段落 ID 重复。")
    joined = "\n\n".join(b["text"] for b in blocks)
    if joined != snapshot["scoring_text"]:
        raise DomainError("EVIDENCE_INVALID", "评分正文必须与完整证据块一致。")
    raw = snapshot["scoring_text"]
    safe = redact(raw)
    if safe != raw:
        return make_snapshot(raw, snapshot["completeness"], snapshot["ocr_issues"]), raw
    return snapshot, raw


def parse_file(content: bytes, filename: str):
    if len(content) > MAX_FILE_SIZE:
        raise DomainError("FILE_TOO_LARGE", "附件上限 20 MiB。")
    suffix = Path(filename).suffix.lower()
    if suffix not in {".txt", ".pdf", ".docx", ".png", ".jpg", ".jpeg"}:
        raise DomainError("NOT_SUPPORTED", "支持 TXT、PDF、DOCX、PNG 和 JPEG。")
    with tempfile.TemporaryDirectory(prefix="recruit-parse-") as directory:
        path = Path(directory) / ("input" + suffix)
        path.write_bytes(content)
        try:
            result = subprocess.run([sys.executable, str(ROOT / "recruit_agent" / "parser_process.py"), str(path)],
                                    capture_output=True, timeout=35, check=False,
                                    env={k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG"}})
        except subprocess.TimeoutExpired:
            raise DomainError("PARSER_TIMEOUT", "附件解析超时，文件未进入评分。")
        if result.returncode != 0:
            raise DomainError("PARSER_FAILED", "附件无法安全解析，请改用正文或可读文件。")
        parsed = json.loads(result.stdout)
        if parsed.get("error"):
            raise DomainError(parsed["error"], parsed["message"])
        if len(parsed["text"]) > MAX_TEXT_SIZE:
            raise DomainError("FILE_TOO_LARGE", "解析后的正文超过上限。")
        return parsed
