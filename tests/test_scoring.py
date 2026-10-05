import copy

import pytest

from recruit_agent.common import ROOT, DomainError, read_json
from recruit_agent.demo import fixture_evaluation
from recruit_agent.resumes import clean_import_snapshot, make_snapshot, redact, snapshot_hash
from recruit_agent.scoring import assess_envelope, calculate_score, validate_job, validate_member

CASES = read_json(ROOT / "examples" / "policy-cases.json")["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["case_id"] for c in CASES])
def test_policy_vectors(job, batch, case):
    candidate = copy.deepcopy(batch["candidates"][0])
    candidate["snapshot"]["completeness"] = case["completeness"]
    member = fixture_evaluation(job, batch)["evaluations"][0]
    for c, rating in zip(member["criteria"], case["ratings"]):
        c["rating"] = rating
    for gate in member["hard_gates"]:
        gate["status"] = case["gate_status"]
    member["content_flags"] = case["flags"]
    score = calculate_score(job, candidate, member)
    for key, value in case["expected"].items():
        assert score[key] == value


def test_information_precedes_known_gate_gap(job, batch):
    c = batch["candidates"][0]
    c["snapshot"]["completeness"] = "partial"
    e = fixture_evaluation(job, batch)["evaluations"][0]
    e["hard_gates"][0]["status"] = "fail"
    result = calculate_score(job, c, e)
    assert result["route"] == "NEEDS_INFORMATION"
    assert result["confirmed_gate_gaps"] == [job["hard_gates"][0]["gate_id"]]


def test_schema_and_evidence_attacks_are_member_failures(job, batch):
    output = fixture_evaluation(job, batch)
    output["evaluations"][0]["criteria"][0]["evidence_refs"][0]["block_id"] = batch["candidates"][1]["snapshot"]["evidence_blocks"][0]["block_id"]
    output["evaluations"][1]["criteria"][0]["evidence_refs"][0]["quote"] = "虚构经历"
    output["evaluations"][2]["action_refs"] = [batch["candidates"][3]["actions"][0]["action_id"]]
    output["evaluations"][3]["summary"] = "请访问 https://evil.invalid"
    output["evaluations"][4]["criteria"][0]["rating"] = True
    output["evaluations"][5]["criteria"][0]["rating"] = 5
    good, errors = assess_envelope(job, batch, output)
    assert len(good) == 4
    assert len(errors) == 6
    assert errors[batch["candidates"][0]["candidate_id"]]["code"] == "EVIDENCE_INVALID"


def test_unknown_id_rejects_envelope_but_missing_member_does_not(job, batch):
    output = fixture_evaluation(job, batch)
    output["evaluations"] = output["evaluations"][:8]
    good, errors = assess_envelope(job, batch, output)
    assert len(good) == 8 and len(errors) == 2
    output["evaluations"][0]["candidate_id"] = "unknown-person"
    with pytest.raises(DomainError, match="未知候选人"):
        assess_envelope(job, batch, output)


def test_duplicate_member_is_not_guessed(job, batch):
    output = fixture_evaluation(job, batch)
    output["evaluations"][1] = copy.deepcopy(output["evaluations"][0])
    good, errors = assess_envelope(job, batch, output)
    assert len(good) == 8 and len(errors) == 2
    assert errors[batch["candidates"][0]["candidate_id"]]["code"] == "DUPLICATE_CANDIDATE"


def test_reversed_members_preserve_identity(job, batch):
    output = fixture_evaluation(job, batch)
    output["evaluations"].reverse()
    good, errors = assess_envelope(job, batch, output)
    assert not errors and set(good) == {c["candidate_id"] for c in batch["candidates"]}


def test_unknown_not_zero_and_non_null_requires_evidence(job, batch):
    c = batch["candidates"][0]
    e = fixture_evaluation(job, batch)["evaluations"][0]
    e["criteria"][0]["evidence_refs"] = []
    with pytest.raises(DomainError):
        validate_member(job, c, e)
    e["criteria"][0]["rating"] = None
    assert validate_member(job, c, e)


def test_sensitive_attributes_cannot_enter_rubric_or_reason(job, batch):
    e = fixture_evaluation(job, batch)["evaluations"][0]
    e["criteria"][0]["reason"] = "因为年龄较小而加分"
    with pytest.raises(DomainError):
        validate_member(job, batch["candidates"][0], e)
    job["criteria"][0]["name"] = "性别匹配"
    with pytest.raises(DomainError):
        validate_job(job)


def test_hash_and_body_consistency(batch):
    for c in batch["candidates"]:
        assert snapshot_hash(c["snapshot"]) == c["snapshot"]["snapshot_sha256"]
        clean_import_snapshot(c["snapshot"])
    bad = copy.deepcopy(batch["candidates"][0]["snapshot"])
    bad["scoring_text"] += "未知补写"
    with pytest.raises(DomainError):
        clean_import_snapshot(bad)


def test_redaction_preserves_relevant_evidence():
    raw = "姓名：张三\n性别：男\n年龄：35\n邮箱：person@example.com\n手机：13800000000\n\n使用 Python 完成交付。\n外部作品 https://evil.invalid/x"
    snapshot = make_snapshot(raw)
    assert "张三" not in snapshot["scoring_text"]
    assert "13800000000" not in snapshot["scoring_text"]
    assert "@example" not in snapshot["scoring_text"]
    assert "https" not in snapshot["scoring_text"]
    assert "Python 完成交付" in snapshot["scoring_text"]
    assert snapshot_hash(snapshot) == snapshot["snapshot_sha256"]
