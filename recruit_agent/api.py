from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .actions import ActionBroker
from .common import ROOT, DomainError, canonical, now, uid
from .demo import publish_fixture, seed
from .harnesses import HarnessAdapter, scoring_batch
from .jobs import initial_draft
from .maintenance import backup
from .platforms import BossAgentCliAdapter
from .resumes import MAX_FILE_SIZE, parse_file
from .service import Service
from .worker import Worker


def create_app(runtime: Path, start_worker=True):
    service = Service(runtime)
    worker = Worker(service)

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            worker.start()
        yield
        if start_worker:
            worker.stop()

    app = FastAPI(title="Boss 招聘工作台", version="0.1.0", lifespan=lifespan)
    app.state.service, app.state.worker = service, worker
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_guard(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"error": {"code": "ORIGIN_REJECTED", "message": "仅接受本机工作台操作。"}}, status_code=403)
            length = request.headers.get("content-length")
            if length and (not length.isdigit() or int(length) > 100 * 1024 * 1024):
                return JSONResponse({"error": {"code": "FILE_TOO_LARGE", "message": "请求超过总上传预算。"}}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return JSONResponse({"error": error.as_dict()}, status_code=error.status)

    @app.exception_handler(KeyError)
    async def missing_field(request, error):
        return JSONResponse({"error": {"code": "INPUT_INVALID", "message": "请求缺少必要字段。"}}, status_code=400)

    @app.get("/")
    def index():
        return FileResponse(ROOT / "recruit_agent" / "static" / "index.html")

    @app.get("/api/health/live")
    def live():
        return {"alive": True, "version": "0.1.0"}

    @app.get("/api/health/ready")
    def ready():
        state = service.status()
        return {"database": True, "worker": state["worker"], "platform": state["accounts"], "harness": state["harness"], "fully_connected": False}

    @app.get("/api/status")
    def status():
        return service.status()

    @app.get("/api/config")
    def config():
        return {"config": service.config, "profile_version": service.profile_version()}

    @app.put("/api/config")
    def configure(body: dict):
        return service.update_config(body)

    @app.post("/api/capabilities/probe")
    def probe():
        reports = []
        for kind in ("dsh", "codex", "opencode", "mcp"):
            cfg = dict(service.config["harness"], primary=kind)
            reports.append(HarnessAdapter(cfg).probe())
        result = {"harnesses": reports, "boss": BossAgentCliAdapter(service).probe()}
        service.db.set_setting("capabilities", result)
        return result

    @app.get("/api/capabilities")
    def capabilities():
        return service.db.setting("capabilities", {"message": "尚未运行本机探测。"})

    @app.get("/api/jobs")
    def jobs():
        return [{**j, "note": service.db.setting("job-note:" + j["job_id"])} for j in service.jobs()]

    @app.post("/api/jobs")
    def create_job(body: dict):
        if "profile" in body:
            return service.create_job(body["profile"], body.get("platform_job_id"))
        return initial_draft(service, body.get("title", ""), body.get("jd_text", ""), body.get("platform_job_id"))

    @app.get("/api/jobs/{job_id}")
    def job(job_id):
        return {**service.get_job(job_id), "note": service.db.setting("job-note:" + job_id)}

    @app.post("/api/jobs/{job_id}/activate")
    def activate(job_id: str, body: dict):
        return service.activate_job(job_id, body.get("profile"), body.get("actor", "workbench"), body.get("platform_job_id"))

    @app.post("/api/imports")
    def imports(body: dict):
        if "batch" in body:
            if isinstance(body["batch"], dict) and body["batch"].get("mode") == "live":
                raise DomainError("SOURCE_NOT_VERIFIED", "实时平台数据仅由后台采集器写入；外部导入请使用 manual 模式。")
            return service.import_batch(body["batch"], body.get("import_key"))
        if not body.get("import_key"):
            raise DomainError("INPUT_INVALID", "请提供导入幂等键。")
        return service.import_texts(body["job_id"], body["candidates"], body["import_key"])

    @app.post("/api/imports/files")
    async def import_files(job_id: str = Form(...), import_key: str = Form(...), files: list[UploadFile] = File(...)):
        if not 1 <= len(files) <= 10:
            raise DomainError("INPUT_INVALID", "一次上传 1 至 10 个文件。")
        rows, errors = [], []
        # Parsing runs in bounded subprocesses; avoid blocking the API event loop.
        import asyncio
        for file in files:
            content = await file.read(MAX_FILE_SIZE + 1)
            try:
                parsed = await asyncio.to_thread(parse_file, content, file.filename or "resume.txt")
                rows.append({"label": Path(file.filename or "简历").stem, **parsed})
            except DomainError as error:
                errors.append({"filename": Path(file.filename or "附件").name, **error.as_dict()})
            finally:
                await file.close()
        result = service.import_texts(job_id, rows, import_key) if rows else {"count": 0, "candidate_ids": []}
        return {**result, "file_errors": errors}

    @app.get("/api/candidates")
    def candidates(job_id: str | None = None, route: str | None = None):
        return service.list_candidates(job_id, route)

    @app.get("/api/candidates/{candidate_id}")
    def candidate(candidate_id: str, job_id: str):
        return service.candidate_detail(candidate_id, job_id)

    @app.delete("/api/candidates/{candidate_id}")
    def delete(candidate_id: str):
        return service.delete_candidate(candidate_id)

    @app.post("/api/evaluations/{run_id}/override")
    def override(run_id: str, body: dict):
        return service.override(run_id, body["evaluation"], body.get("reason", ""), body.get("actor", "workbench"))

    @app.post("/api/evaluation-batches/flush")
    def flush(body: dict):
        return {"batch_ids": service.flush_batches(True, body.get("job_id"))}

    @app.get("/api/evaluation-batches")
    def batches():
        return [service.batch_report(row["batch_id"]) for row in service.db.all("SELECT batch_id FROM evaluation_batches ORDER BY created_at DESC LIMIT 100")]

    @app.post("/api/evaluation-batches/claim")
    def claim(body: dict):
        lease = service.claim_batch(body.get("owner", "manual"), body.get("job_id"), body.get("batch_id"))
        if lease.get("batch"):
            lease["batch"] = scoring_batch(lease["batch"])
        return lease

    @app.post("/api/evaluation-batches/{batch_id}/heartbeat")
    def heartbeat(batch_id: str, body: dict):
        task = service.db.one("SELECT task_id FROM tasks WHERE object_id=? AND kind='score'", (batch_id,))
        if not task:
            raise DomainError("NOT_FOUND", "批次不存在。", status=404)
        return service.queue.heartbeat(task["task_id"], body["lease_token"], body["fencing_token"], service.config["queue"]["lease_seconds"])

    @app.post("/api/evaluation-batches/{batch_id}/submit")
    def submit(batch_id: str, body: dict):
        supplied = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
        metadata = {"harness": str(supplied.get("harness", "api_client"))[:80],
                    "model_id": str(supplied.get("model_id", ""))[:120],
                    "verification": "api_client_submission", "server_invoked_model": False}
        return service.submit(batch_id, body["lease_token"], body["fencing_token"], body["evaluation"], metadata)

    @app.get("/api/evaluation-batches/{batch_id}/report")
    def report(batch_id: str):
        return service.batch_report(batch_id)

    @app.post("/api/evaluation-batches/{batch_id}/import-result")
    def import_result(batch_id: str, body: dict):
        lease = service.claim_batch("manual_result_import", batch_id=batch_id, force=False)
        if not lease.get("batch"):
            raise DomainError("BATCH_BUSY", "此批次正被处理、已经完成或尚在退避。", status=409)
        try:
            return service.submit(batch_id, lease["lease_token"], lease["fencing_token"], body["evaluation"], {"harness": "manual_result_import", "verification": "operator_supplied_not_server_invoked"})
        except DomainError as error:
            service.queue.fail({"task_id": lease["task_id"], "lease_token": lease["lease_token"], "fencing_token": lease["fencing_token"]}, error)
            raise

    @app.get("/api/evaluation-batches/{batch_id}/prompt")
    def prompt(batch_id: str):
        from .harnesses import build_prompt
        row = service.db.one("SELECT * FROM evaluation_batches WHERE batch_id=?", (batch_id,))
        if not row:
            raise DomainError("NOT_FOUND", "批次不存在。", status=404)
        return Response(build_prompt(json.loads(row["profile"]), json.loads(row["payload"])), media_type="text/plain; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{batch_id}-prompt.txt"'})

    @app.get("/api/tasks")
    def tasks():
        return service.db.all("SELECT task_id,kind,object_id,state,attempts,heartbeat,next_run_at,error,created_at FROM tasks ORDER BY created_at DESC LIMIT 150")

    @app.post("/api/tasks/{task_id}/retry")
    def retry(task_id: str):
        return service.retry_task(task_id)

    @app.post("/api/accounts")
    def account(body: dict):
        aid = body.get("account_id") or uid("account")
        with service.db.transaction() as con:
            service.ensure_account(con, aid)
        return {"account_id": aid}

    @app.post("/api/accounts/{account_id}/pause")
    def pause(account_id: str, body: dict):
        return service.pause_account(account_id, body.get("reason", "用户暂停"))

    @app.post("/api/accounts/{account_id}/resume")
    def resume(account_id: str):
        return service.resume_account(account_id)

    @app.post("/api/collections")
    def collection(body: dict):
        account = service.db.one("SELECT * FROM accounts WHERE account_id=?", (body["account_id"],))
        if not account or account["status"] == "DEMO":
            raise DomainError("ACCOUNT_NOT_READY", "请先创建真实采集账号。")
        job = service.get_job(body["job_id"])
        if not job["active"] or job["is_synthetic"] or not job["platform_job_id"]:
            raise DomainError("JOB_NOT_READY", "请启用真实岗位并绑定平台职位 ID。")
        channel = body.get("channel", "applications")
        if channel not in {"applications", "existing_chat", "recommendation"}:
            raise DomainError("INPUT_INVALID", "采集渠道不支持。")
        if channel == "recommendation" and not service.config["platform"]["active_sourcing_enabled"]:
            raise DomainError("SOURCING_DISABLED", "请先启用主动寻源。")
        with service.db.transaction() as con:
            con.execute("INSERT INTO collection_cursors(account_id,job_id,channel,next_poll) VALUES (?,?,?,?) ON CONFLICT(account_id,job_id,channel) DO UPDATE SET next_poll=excluded.next_poll", (body["account_id"], body["job_id"], channel, now()))
        return {"scheduled": True, "live_enabled": service.config["platform"]["live_enabled"]}

    @app.post("/api/actions/{action_id}/resolve")
    def resolve(action_id: str, body: dict):
        return ActionBroker(service).resolve(action_id, body["candidate_id"], body["job_id"])

    @app.post("/api/actions/{action_id}/download-attachment")
    def download_attachment(action_id: str, body: dict):
        action = service.db.one("SELECT * FROM action_targets WHERE action_id=?", (action_id,))
        if not action or action["candidate_id"] != body["candidate_id"] or action["job_id"] != body["job_id"]:
            raise DomainError("IDENTITY_UNVERIFIED", "附件动作不属于此候选人与岗位。", status=403)
        return BossAgentCliAdapter(service).download_attachment(action, body["attachment_message_id"])

    @app.get("/api/runtime/observation")
    def observation():
        events = service.db.all("SELECT object_id,event,created_at FROM audit_events WHERE event IN ('service_started','service_stopped','service_heartbeat') ORDER BY created_at")
        times = [e["created_at"] for e in events if e["event"] == "service_heartbeat"]
        span = times[-1]-times[0] if len(times)>1 else 0
        gaps = [b-a for a,b in zip(times,times[1:])]
        return {"elapsed_observed_seconds": round(span), "heartbeats": len(times), "maximum_gap_seconds": round(max(gaps,default=0)),
                "runtime_24h_observed": span >= 86400 and max(gaps,default=86400) <= 180,
                "real_platform_24h_accepted": False, "note": "服务观察与真实账号采集/评分验收分开；未经过实际 24 小时不能提前通过。", "events": events[-1500:]}

    @app.get("/api/outbox")
    def outbox():
        return service.db.all("SELECT o.*,c.label,c.synthetic FROM outbox o JOIN candidates c ON c.candidate_id=o.candidate_id ORDER BY o.created_at DESC")

    @app.post("/api/outbox/drafts")
    def drafts(body: dict):
        return service.create_drafts(body["job_id"], body["drafts"])

    @app.post("/api/outbox/approve")
    def approve(body: dict):
        return service.approve_drafts(body["approvals"], body.get("actor", "workbench"))

    @app.post("/api/outbox/{item_id}/execute")
    def execute(item_id: str):
        return ActionBroker(service).execute(item_id)

    @app.post("/api/outbox/{item_id}/verify")
    def verify(item_id: str, body: dict):
        return ActionBroker(service).verify_message(item_id, body["platform_message_id"])

    @app.get("/api/exports/{job_id}")
    def export(job_id: str, format: str = "csv"):
        if format not in {"csv", "md"}:
            raise DomainError("NOT_SUPPORTED", "请选择 CSV 或 Markdown。")
        return Response(service.export(job_id, format), media_type="text/csv; charset=utf-8" if format == "csv" else "text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="recruit-report.{format}"'})

    @app.post("/api/backup")
    def create_backup():
        path = backup(service)
        return FileResponse(path, media_type="application/zip", filename=path.name)

    @app.post("/api/demo/seed")
    def demo_seed():
        return seed(service)

    @app.post("/api/demo/fixture")
    def demo_fixture():
        return publish_fixture(service)

    @app.get("/api/mcp/config")
    def mcp_config():
        import sys
        return {"mcpServers": {"recruit": {"command": sys.executable, "args": ["-m", "recruit_agent", "mcp", "--runtime", str(service.runtime)], "env": {"PYTHONPATH": str(ROOT)}}}}

    app.mount("/static", StaticFiles(directory=ROOT / "recruit_agent" / "static"), name="static")
    return app
