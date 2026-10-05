"""Explicit synthetic, hand-authored format fixture. This module never claims to be an AI scorer."""
import copy
import json

from .common import ROOT, BINDINGS, DomainError, read_json


def seed(service):
    previous = service.db.one("SELECT result FROM imports WHERE import_key='seed-demo-v1'")
    if previous:
        return {**json.loads(previous["result"]), "replayed": True}
    job = read_json(ROOT / "examples" / "job.example.json")
    if not service.db.one("SELECT 1 FROM jobs WHERE job_id=?", (job["job_id"],)):
        service.create_job(job)
        service.activate_job(job["job_id"], actor="synthetic_demo")
    job = service.get_job(job["job_id"])
    batch = read_json(ROOT / "examples" / "batch-10.example.json")
    batch.update({k: job[k] for k in BINDINGS})
    return service.import_batch(batch, "seed-demo-v1")


def fixture_evaluation(job, batch):
    cases = read_json(ROOT / "examples" / "policy-cases.json")["cases"]
    evaluations = []
    for i, candidate in enumerate(batch["candidates"]):
        case = cases[i % len(cases)]
        blocks = candidate["snapshot"]["evidence_blocks"]
        criteria = []
        for n, criterion in enumerate(job["criteria"]):
            rating = case["ratings"][n]
            block = blocks[min(n, len(blocks)-1)]
            ref = {"block_id": block["block_id"], "quote": block["text"], "evidence_kind": "resume_claim"}
            criteria.append({"criterion_id": criterion["criterion_id"], "rating": rating, "evidence_refs": [ref] if rating is not None else [],
                             "reason": "离线格式样例：预置人工测试等级，仅用于展示计算和证据定位，不代表 AI 判断。", "missing_information": ["样例待核实"] if rating is None else []})
        ref = {"block_id": blocks[0]["block_id"], "quote": blocks[0]["text"], "evidence_kind": "resume_claim"}
        evaluations.append({"candidate_id": candidate["candidate_id"], "snapshot_id": candidate["snapshot"]["snapshot_id"], "snapshot_sha256": candidate["snapshot"]["snapshot_sha256"],
                            "criteria": criteria, "hard_gates": [{"gate_id": g["gate_id"], "status": case["gate_status"], "evidence_refs": [] if case["gate_status"] == "unknown" else [ref], "reason": "离线人工格式测试向量。"} for g in job["hard_gates"]],
                            "summary": "合成简历 + 人工格式样例；不是已执行的 AI 评分。", "verification_questions": ["请说明项目任务和本人贡献。"],
                            "message_draft": "您好，想进一步了解您在相关项目中的职责和交付过程。", "action_refs": [a["action_id"] for a in candidate["actions"]], "content_flags": case["flags"]})
    return {"schema_version": "1.0", "batch_id": batch["batch_id"], **{k: batch[k] for k in BINDINGS}, "evaluations": evaluations}


def publish_fixture(service):
    seed(service)
    lease = service.claim_batch("offline_fixture", job_id="job-demo-001")
    if not lease.get("batch"):
        return {"message": "样例已评分或批次已被其他运行器领取。"}
    if lease["batch"]["mode"] != "synthetic":
        raise DomainError("DEMO_MISMATCH", "离线样例不能处理真实数据。")
    result = fixture_evaluation(lease["job"], lease["batch"])
    return service.submit(lease["batch"]["batch_id"], lease["lease_token"], lease["fencing_token"], result,
                          {"harness": "offline_fixture", "verification": "hand_authored_format_example_no_model", "input_mode": "synthetic"})
