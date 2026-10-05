from __future__ import annotations

import copy
import re
from decimal import Decimal, ROUND_HALF_UP

from jsonschema import Draft202012Validator

from .common import ROOT, BINDINGS, DomainError, read_json

SCHEMAS = {name: read_json(ROOT / "contracts" / f"{name}.schema.json") for name in ("job", "batch", "evaluation")}
URL = re.compile(r"https?://|www\.", re.I)
FORBIDDEN = re.compile(r"年龄|性别|婚育|婚姻|宗教|民族|健康状况|籍贯|名校|颜值|长相|男性|女性|已婚|未婚|gender|nationality|marital|religion", re.I)


def validate_schema(name, obj):
    errors = list(Draft202012Validator(SCHEMAS[name]).iter_errors(obj))
    if errors:
        paths = ["/".join(map(str, e.absolute_path)) + ": " + e.message[:180] for e in errors[:6]]
        raise DomainError("OUTPUT_INVALID" if name == "evaluation" else "INPUT_INVALID", "数据不符合契约。", details=paths)


def validate_job(job):
    validate_schema("job", job)
    if sum(c["weight"] for c in job["criteria"]) != 100:
        raise DomainError("RUBRIC_INVALID", "维度权重必须合计 100。")
    for field, key in (("criteria", "criterion_id"), ("hard_gates", "gate_id")):
        if len({x[key] for x in job[field]}) != len(job[field]):
            raise DomainError("RUBRIC_INVALID", "量表 ID 不能重复。")
        if any(FORBIDDEN.search(" ".join(str(v) for v in item.values())) for item in job[field]):
            raise DomainError("RUBRIC_EXCLUDED_ATTRIBUTE", "量表包含岗位评分排除属性，请修改后启用。")
    policy = job["review_policy"]
    if policy["standard_score"] > policy["high_priority_score"]:
        raise DomainError("RUBRIC_INVALID", "普通复核阈值不得高于优先复核阈值。")


def normalized(text):
    return re.sub(r"\s+", " ", text).strip()


def validate_member(job, candidate, member):
    shell = {"schema_version": "1.0", "batch_id": "validation", **{k: job[k] for k in BINDINGS}, "evaluations": [member]}
    validate_schema("evaluation", shell)
    snap = candidate["snapshot"]
    if member["candidate_id"] != candidate["candidate_id"] or any(member[k] != snap[k] for k in ("snapshot_id", "snapshot_sha256")):
        raise DomainError("SNAPSHOT_MISMATCH", "候选人身份或评分快照不匹配。")
    for field, key in (("criteria", "criterion_id"), ("hard_gates", "gate_id")):
        ids = [x[key] for x in member[field]]
        if len(set(ids)) != len(ids) or set(ids) != {x[key] for x in job[field]}:
            raise DomainError("OUTPUT_INVALID", "维度或必需条件 ID 不完整、重复或多余。")
    blocks = {b["block_id"]: normalized(b["text"]) for b in snap["evidence_blocks"]}
    for judgment in member["criteria"] + member["hard_gates"]:
        for ref in judgment["evidence_refs"]:
            if ref["block_id"] not in blocks or normalized(ref["quote"]) not in blocks[ref["block_id"]]:
                raise DomainError("EVIDENCE_INVALID", "证据不是当前候选人的原文片段。")
        if FORBIDDEN.search(judgment["reason"]):
            raise DomainError("EXCLUDED_ATTRIBUTE", "判断理由涉及评分排除属性。")
    allowed = {a["action_id"] for a in candidate["actions"]}
    if len(set(member["action_refs"])) != len(member["action_refs"]) or not set(member["action_refs"]) <= allowed:
        raise DomainError("ACTION_ID_INVALID", "入口引用必须属于当前候选人。")
    prose = [member["summary"], member["message_draft"] or "", *member["verification_questions"]]
    prose += [x["reason"] for x in member["criteria"] + member["hard_gates"]]
    prose += [t for x in member["criteria"] for t in x["missing_information"]]
    if any(URL.search(t) for t in prose):
        raise DomainError("ACTION_URL_INVALID", "模型不得创建或返回外部链接。")
    return member


def calculate_score(job, candidate, member, identity_verified=True):
    weights = {c["criterion_id"]: Decimal(c["weight"]) for c in job["criteria"]}
    known = Decimal(0)
    lower = Decimal(0)
    unknown = []
    for criterion in member["criteria"]:
        w = weights[criterion["criterion_id"]]
        if criterion["rating"] is None:
            unknown.append(criterion["criterion_id"])
        else:
            known += w
            lower += w * Decimal(criterion["rating"]) / Decimal(4)
    upper = lower + (Decimal(100) - known)
    policy = job["review_policy"]
    coverage = known / Decimal(100)
    gates = member["hard_gates"]
    gaps = [g["gate_id"] for g in gates if g["status"] == "fail"]
    information = (not identity_verified or candidate["snapshot"]["completeness"] != "full_text" or
                   bool(candidate["snapshot"]["ocr_issues"]) or
                   bool(set(member["content_flags"]) & {"text_conflict", "ocr_uncertain"}) or
                   any(g["status"] == "unknown" for g in gates) or
                   coverage < Decimal(str(policy["minimum_coverage"])))
    if information:
        route = "NEEDS_INFORMATION"
    elif gaps:
        route = "REVIEW_REQUIREMENT_GAP"
    elif lower >= Decimal(str(policy["high_priority_score"])):
        route = "REVIEW_HIGH_PRIORITY"
    elif lower >= Decimal(str(policy["standard_score"])):
        route = "REVIEW_STANDARD"
    else:
        route = "REVIEW_MATCH_GAP"
    rounded = lambda value: float(value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    return {"lower": rounded(lower), "upper": rounded(upper), "coverage": float(coverage), "route": route,
            "unknown_criteria": unknown, "confirmed_gate_gaps": gaps, "display_single_score": known > 0,
            "formula_version": policy["score_formula_version"], "human_decision_required": True}


def assess_envelope(job, batch, envelope):
    """Validate shared bindings, then members independently to preserve partial successes."""
    if not isinstance(envelope, dict) or set(envelope) != {"schema_version", "batch_id", *BINDINGS, "evaluations"}:
        raise DomainError("OUTPUT_INVALID", "响应字段不符合评估信封。")
    if envelope["schema_version"] != "1.0" or envelope["batch_id"] != batch["batch_id"] or any(envelope[k] != batch[k] for k in BINDINGS):
        raise DomainError("BATCH_MISMATCH", "响应岗位、批次或版本不匹配。")
    rows = envelope["evaluations"]
    if not isinstance(rows, list) or len(rows) > 10:
        raise DomainError("OUTPUT_INVALID", "评估成员列表无效。")
    expected = {c["candidate_id"]: c for c in batch["candidates"]}
    seen, duplicate = set(), set()
    for row in rows:
        cid = row.get("candidate_id") if isinstance(row, dict) else None
        if cid not in expected:
            raise DomainError("IDENTITY_UNVERIFIED", "响应包含未知候选人，整次响应拒收。")
        if cid in seen:
            duplicate.add(cid)
        seen.add(cid)
    successes, errors = {}, {}
    for row in rows:
        cid = row["candidate_id"]
        if cid in duplicate:
            errors[cid] = {"code": "DUPLICATE_CANDIDATE", "message": "响应多次使用相同候选人 ID。"}
            continue
        try:
            successes[cid] = validate_member(job, expected[cid], row)
        except DomainError as error:
            errors[cid] = error.as_dict()
    for cid in expected.keys() - seen:
        errors[cid] = {"code": "MEMBER_MISSING", "message": "模型未返回此候选人。"}
    return successes, errors


def native_codex_schema():
    """Codex structured outputs accept the structural subset; full business schema runs afterward."""
    schema = copy.deepcopy(SCHEMAS["evaluation"])
    def visit(node):
        if isinstance(node, dict):
            for key in ("$schema", "$id", "title", "allOf", "pattern", "minItems", "maxItems", "minimum", "maximum", "minLength"):
                node.pop(key, None)
            if "const" in node:
                node["enum"] = [node.pop("const")]
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(schema)
    return schema
