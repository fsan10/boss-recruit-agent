import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from recruit_agent.common import ROOT, BINDINGS, DomainError, now, read_json
from recruit_agent.demo import fixture_evaluation, publish_fixture, seed
from recruit_agent.maintenance import backup, restore, retain
from recruit_agent.service import Service


def test_import_replay_same_names_and_identical_text_do_not_merge(seeded):
    result = seed(seeded)
    assert result["replayed"] is True
    assert seeded.status()["candidates"] == 10
    assert len(seeded.db.all("SELECT * FROM tasks WHERE kind='score'")) == 1
    same = seeded.db.one("SELECT label,COUNT(*) n,COUNT(DISTINCT candidate_id) ids FROM candidates GROUP BY label HAVING COUNT(*)>1")
    assert same and same["n"] == same["ids"]


def test_conflicting_replay_and_cross_account_are_rejected(seeded):
    b = read_json(ROOT / "examples" / "batch-10.example.json")
    job = seeded.get_job(b["job_id"])
    b.update({k:job[k] for k in BINDINGS})
    b["candidates"][0]["candidate_label"] = "changed"
    with pytest.raises(DomainError) as caught:
        seeded.import_batch(b, "seed-demo-v1")
    assert caught.value.code == "IDEMPOTENCY_CONFLICT"
    b["candidates"][0]["account_id"] = "other-account"
    with pytest.raises(DomainError) as caught:
        seeded.import_batch(b, "new-import")
    assert caught.value.code == "IDENTITY_UNVERIFIED"


def test_concurrent_claim_has_single_owner(seeded):
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda n:seeded.claim_batch(f"owner-{n}",force=False), range(8)))
    assert sum(c.get("batch") is not None for c in claims) == 1
    winner = next(c for c in claims if c.get("batch"))
    assert len(winner["batch"]["candidates"]) == 10


def test_stale_fencing_token_and_expired_lease_rejected(seeded, lease):
    output = fixture_evaluation(lease["job"], lease["batch"])
    with seeded.db.transaction() as con:
        con.execute("UPDATE tasks SET lease_until=? WHERE task_id=?", (now()-1,lease["task_id"]))
    new = seeded.claim_batch("replacement",force=False)
    assert new["fencing_token"] > lease["fencing_token"]
    with pytest.raises(DomainError) as caught:
        seeded.submit(lease["batch"]["batch_id"],lease["lease_token"],lease["fencing_token"],output)
    assert caught.value.code == "LEASE_LOST"
    result = seeded.submit(new["batch"]["batch_id"],new["lease_token"],new["fencing_token"],output)
    assert len(result["accepted"]) == 10


def test_eight_successes_survive_restart_and_only_two_are_retried(seeded,lease):
    output = fixture_evaluation(lease["job"],lease["batch"])
    output["evaluations"] = output["evaluations"][:8]
    result = seeded.submit(lease["batch"]["batch_id"],lease["lease_token"],lease["fencing_token"],output)
    assert len(result["accepted"]) == 8 and len(result["errors"]) == 2
    with seeded.db.transaction() as con:
        con.execute("UPDATE tasks SET next_run_at=0 WHERE kind='score'")
    restarted = Service(seeded.runtime)
    new = restarted.claim_batch("after-restart",force=False)
    assert len(new["batch"]["candidates"]) == 2
    output = fixture_evaluation(new["job"],new["batch"])
    restarted.submit(new["batch"]["batch_id"],new["lease_token"],new["fencing_token"],output)
    assert restarted.batch_report(new["batch"]["batch_id"])["completed"] == 10
    assert restarted.db.one("SELECT COUNT(*) n FROM evaluation_runs")["n"] == 10


def test_sub_ten_batch_flushes_after_180_seconds(service,job,batch):
    service.create_job(job)
    job = service.activate_job(job["job_id"])
    batch.update({k:job[k] for k in BINDINGS})
    batch["candidates"] = batch["candidates"][:3]
    service.import_batch(batch)
    assert not service.db.all("SELECT * FROM evaluation_batches")
    with service.db.transaction() as con:
        con.execute("UPDATE pending_evaluations SET queued_at=?", (now()-181,))
    service.flush_batches()
    claim = service.claim_batch(force=False)
    assert len(claim["batch"]["candidates"]) == 3


def test_context_split_retains_all_text(service,job,batch):
    cfg=service.config;cfg["harness"]["context_tokens"]=20000;service.update_config(cfg)
    service.create_job(job);job=service.activate_job(job["job_id"])
    batch.update({k:job[k] for k in BINDINGS});service.import_batch(batch)
    stored=service.db.all("SELECT payload FROM evaluation_batches")
    assert len(stored)>1
    payloads=[json.loads(r["payload"]) for r in stored]
    assert all(p["parent_batch_id"] for p in payloads)
    candidates=[c for p in payloads for c in p["candidates"]]
    assert len(candidates)==10
    assert {c["snapshot"]["scoring_text"] for c in candidates}=={c["snapshot"]["scoring_text"] for c in batch["candidates"]}


def test_human_override_keeps_original_and_uses_new_current_score(seeded):
    result=publish_fixture(seeded)
    cid=result["accepted"][0]
    detail=seeded.candidate_detail(cid,"job-demo-001")
    old=detail["runs"][0];e=copy.deepcopy(old["evaluation"])
    e["criteria"][0]["rating"]=1
    updated=seeded.override(old["run_id"],e,"人工核实后修正")
    assert updated["run_id"]!=old["run_id"]
    detail=seeded.candidate_detail(cid,"job-demo-001")
    assert len(detail["runs"])==2
    assert detail["runs"][0]["is_current"]
    assert detail["runs"][1]["evaluation"]==old["evaluation"]
    assert seeded.batch_report(result["report"]["batch_id"])["members"][0]["score"]==updated["score"]


def test_rubric_change_creates_new_evaluation_without_deleting_history(seeded):
    publish_fixture(seeded)
    job=seeded.get_job("job-demo-001");old=job["rubric_version"]
    job["criteria"][0]["weight"]-=5;job["criteria"][1]["weight"]+=5
    new=seeded.activate_job(job["job_id"],job)
    assert new["rubric_version"]!=old
    assert all(c["score"] is None for c in seeded.list_candidates())
    assert seeded.db.one("SELECT COUNT(*) n FROM evaluation_runs")["n"]==10


def test_budget_does_not_reset_on_restart(service):
    service.reserve_budget("test",1)
    restarted=Service(service.runtime)
    with pytest.raises(DomainError) as caught:
        restarted.reserve_budget("test",1)


def test_daily_budget_waits_until_reset_without_resuming_auth_pause(seeded):
    from recruit_agent.worker import Worker
    lease=seeded.claim_batch('budget-test')
    seeded.reserve_budget('harness_runs',1)
    with pytest.raises(DomainError) as caught:seeded.reserve_budget('harness_runs',1)
    error=caught.value;reset=error.details['resumes_at']
    seeded.db.set_setting('harness_pause',error.as_dict())
    seeded.queue.fail(lease,error)
    worker=Worker(seeded)
    with seeded.db.transaction() as con:worker.resume_daily_budgets(con,reset-1)
    assert seeded.db.one('SELECT state FROM tasks WHERE task_id=?',(lease['task_id'],))['state']=='BLOCKED'
    with seeded.db.transaction() as con:worker.resume_daily_budgets(con,reset+1)
    assert seeded.db.one('SELECT state,attempts FROM tasks WHERE task_id=?',(lease['task_id'],))=={'state':'RETRY','attempts':0}
    assert seeded.db.setting('harness_pause') is None
    auth={'code':'HARNESS_AUTH_REQUIRED','message':'login required'}
    seeded.db.set_setting('harness_pause',auth)
    with seeded.db.transaction() as con:worker.resume_daily_budgets(con,reset+86401)
    assert seeded.db.setting('harness_pause')==auth
    assert caught.value.code=="BUDGET_EXHAUSTED"


def test_manual_import_concurrent_replay(service,job):
    job["is_synthetic"]=False;service.create_job(job);service.activate_job(job["job_id"])
    rows=[{"text":"使用 Python 完成交付。","label":"同名"},{"text":"使用 Python 完成交付。","label":"同名"}]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:service.import_texts(job["job_id"],rows,"manual-replay"),range(2)))
    assert results[0]["candidate_ids"]==results[1]["candidate_ids"]
    assert service.status()["candidates"]==2


def test_backup_restore_and_deletion(seeded,tmp_path):
    publish_fixture(seeded)
    archive=backup(seeded)
    restored_path=restore(archive,tmp_path/"restored")
    restored=Service(restored_path)
    assert len(restored.list_candidates())==10
    assert not restored.config["platform"]["live_enabled"]
    assert not restored.config["harness"]["enabled"]
    restored.delete_candidate("candidate-001")
    assert len(restored.list_candidates())==9
    assert not restored.db.all("SELECT * FROM evaluation_runs WHERE candidate_id='candidate-001'")
    assert all("candidate-001" not in {c["candidate_id"] for c in json.loads(b["payload"])["candidates"]} for b in restored.db.all("SELECT payload FROM evaluation_batches"))


def test_csv_formula_escape_and_evidence_columns(seeded):
    publish_fixture(seeded)
    with seeded.db.transaction() as con:
        con.execute("UPDATE candidates SET label='=HYPERLINK(1)' WHERE candidate_id='candidate-001'")
    report=seeded.export("job-demo-001")
    assert "'=HYPERLINK(1)" in report
    assert "证据摘要" in report and "信息缺口" in report
