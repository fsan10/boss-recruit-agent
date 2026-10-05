from __future__ import annotations

import copy
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from .common import ROOT, DomainError, canonical, iso, now
from .scoring import native_codex_schema

LIMIT = 12 * 1024 * 1024


def scoring_batch(batch):
    clean = copy.deepcopy(batch)
    for c in clean["candidates"]:
        c["candidate_label"] = "候选人 " + c["candidate_id"]
        for a in c["actions"]:
            a["handle_url"] = None
            a["observed_platform_url"] = None
    return clean


def build_prompt(job, batch, instructions=None):
    clean_job = {k: v for k, v in job.items() if k not in {"platform_job_id", "active", "approved_by"}}
    prompt = instructions or (ROOT / "prompts" / "evaluate_batch.md").read_text(encoding="utf-8")
    job_json, batch_json = canonical(clean_job), canonical(scoring_batch(batch))
    prompt = prompt.replace("{{JOB_JSON}}", "使用下文 JOB_JSON。") .replace("{{BATCH_JSON}}", "使用下文 BATCH_JSON。")
    return (prompt + "\n\n本任务所有数据已经提供。禁止调用任何工具或读文件；只输出契约 JSON。"
            " evidence_refs 的 quote 必须逐字复制本人的 evidence_blocks。不要在输出中写链接。"
            + "\n\nJOB_JSON\n" + job_json + "\n\nBATCH_JSON\n" + batch_json +
            "\n\nEVALUATION_SCHEMA\n" + canonical(native_codex_schema()))


def parse_business_json(text):
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.S)
    if fenced:
        text = fenced[1].strip()
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        raise DomainError("OUTPUT_INVALID", "最终回答不是一份完整 JSON；事件日志不能当作评分结果。")
    if not isinstance(value, dict):
        raise DomainError("OUTPUT_INVALID", "最终回答必须是评估对象。")
    return value


def parse_events(stdout):
    events = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            raise DomainError("OUTPUT_INVALID", "harness stdout 含不合法 JSON 事件。")
        if not isinstance(event, dict):
            raise DomainError("OUTPUT_INVALID", "harness 事件应为对象。")
        events.append(event)
    return events


def parse_dsh(stdout, exit_code):
    events = parse_events(stdout)
    ends = [e for e in events if e.get("type") == "status" and e.get("phase") == "turn_end"]
    finals = [e for e in events if e.get("type") == "final"]
    if exit_code != 0 or not ends or ends[-1].get("reason") != "completed" or len(finals) != 1:
        raise DomainError("HARNESS_FAILED", "DSH 未以 completed 结束，final 事件本身不能证明成功。")
    if any(e.get("type") in {"tool_call", "error"} for e in events):
        raise DomainError("REQUIRES_INTERACTION", "评分会话请求了工具或出现错误，未发布结果。")
    return parse_business_json(finals[0].get("text", "")), events


def sanitized_env(kind, config):
    common = {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "SystemRoot", "TEMP", "TMP", "LANG", "LC_ALL", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS"}
    keys = common | ({"CODEX_HOME", "OPENAI_API_KEY"} if kind == "codex" else
                     {"DEEPSEEK_API_KEY", "DSH_HOME", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"} if kind == "dsh" else
                     {"OPENCODE_CONFIG", "XDG_CONFIG_HOME", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"})
    env = {k: v for k, v in os.environ.items() if k in keys}
    env["NO_COLOR"] = "1"
    if kind == "dsh":
        if config["dsh_config_dir"]:
            env["DSH_HOME"] = config["dsh_config_dir"]
        env["DSH_PERMISSION_MODE"] = "read-only"
    return env


def terminate_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class HarnessAdapter:
    def __init__(self, config):
        self.config = config
        self.kind = config["primary"]

    def executable(self):
        path = self.config["executables"].get(self.kind, self.kind)
        resolved = shutil.which(path)
        if not resolved:
            raise DomainError("CAPABILITY_UNAVAILABLE", f"找不到 {self.kind} 可执行程序；可在设置页填写路径，或使用 WorkBuddy MCP。")
        return resolved

    def probe(self):
        if self.kind == "mcp":
            return {"harness": "mcp", "available": True, "version": "official-sdk-2.3.0", "authentication": "client_owned", "noninteractive": False, "unattended_ready": False, "method": "stdio tools"}
        try:
            exe = self.executable()
            result = subprocess.run([exe, "--version"], capture_output=True, timeout=10, env=sanitized_env(self.kind, self.config))
            version = result.stdout.decode("utf-8", errors="replace").strip()[:160]
            return {"harness": self.kind, "available": result.returncode == 0, "version": version,
                    "authentication": "unknown_until_smoke_test", "noninteractive": True,
                    "json_method": "JSONL final.text" if self.kind == "dsh" else "last-message file" if self.kind == "codex" else "JSON text events",
                    "tools": "guard-denied" if self.kind == "dsh" else "feature-disabled/read-only" if self.kind == "codex" else "requires_verified_config",
                    "unattended_ready": False, "real_batch_test": "not_performed_by_probe"}
        except (DomainError, subprocess.TimeoutExpired) as error:
            return {"harness": self.kind, "available": False, "authentication": "unknown", "unattended_ready": False,
                    "error": error.code if isinstance(error, DomainError) else "PROBE_TIMEOUT"}

    def evaluate(self, job, batch, on_process=None, *, prompt_override=None, schema_override=None):
        exe = self.executable()
        if batch["mode"] != "synthetic" and not self.config["allowed_for_real_resumes"]:
            raise DomainError("DATA_PROCESSOR_NOT_ENABLED", "此 harness 尚未配置为允许处理真实简历。")
        if batch["mode"] != "synthetic" and not self.config["model_id"]:
            raise DomainError("MODEL_NOT_CONFIGURED", "真实简历评分须填写模型标识，形成可追溯评分版本。")
        if self.kind == "opencode" and not self.config["opencode_tools_disabled_verified"]:
            raise DomainError("CAPABILITY_UNAVAILABLE", "OpenCode 工具权限尚未核验；请先按接入说明配置只读评估环境。")
        if self.kind == "dsh" and not self.config["dsh_config_dir"]:
            raise DomainError("CAPABILITY_UNAVAILABLE", "请配置专用 DSH_HOME；评分器不复用包含其他 MCP 和浏览器工具的配置目录。")
        started = time.monotonic()
        started_wall = now()
        with tempfile.TemporaryDirectory(prefix="recruit-score-") as temp:
            cwd = Path(temp)
            final = cwd / "final.json"
            prompt = prompt_override or build_prompt(job, batch)
            if self.kind == "dsh":
                guard = cwd / "scoring.patch.yml"
                guard.write_text("- insert:\n    - id: recruit-scoring-guard\n      name: " + json.dumps(str(ROOT / "config" / "scoring-guard.mjs")) + "\n", encoding="utf-8")
                argv = [exe, "--profile", self.config["dsh_profile"], "--patch", str(guard), "--json"]
                input_text = prompt
            elif self.kind == "codex":
                schema = cwd / "schema.json"
                schema.write_text(canonical(native_codex_schema(schema_override)), encoding="utf-8")
                argv = [exe, "exec", "--json", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check", "--sandbox", "read-only",
                        "-c", "approval_policy=\"never\"", "-c", "web_search=\"disabled\"",
                        "--output-schema", str(schema), "--output-last-message", str(final)]
                for feature in ("shell_tool", "unified_exec", "apps", "plugins", "hooks", "browser_use", "computer_use", "image_generation", "view_image", "multi_agent"):
                    argv += ["--disable", feature]
                if self.config["model_id"]:
                    argv += ["--model", self.config["model_id"]]
                argv += ["-"]
                input_text = prompt
            elif self.kind == "opencode":
                path = cwd / "evaluation-input.txt"
                path.write_text(prompt, encoding="utf-8")
                argv = [exe, "run", "--format", "json", "--file", str(path)]
                if self.config["model_id"]:
                    argv += ["--model", self.config["model_id"]]
                argv += ["使用附带的量表和简历，仅返回约定 JSON。禁止工具调用。"]
                input_text = None
            else:
                raise DomainError("NOT_SUPPORTED", "MCP 模式由客户端领取并提交，不启动本地模型。")
            flags = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=sanitized_env(self.kind, self.config), **flags)
            if on_process:
                on_process(process)
            try:
                stdout, stderr = process.communicate(input_text.encode("utf-8") if input_text is not None else None,
                                                     timeout=self.config["timeout_seconds"])
            except subprocess.TimeoutExpired:
                terminate_tree(process)
                process.communicate()
                raise DomainError("HARNESS_TIMEOUT", "评分超过超时限制，进程树已停止。", retryable=True)
            finally:
                if on_process:
                    on_process(None)
            if len(stdout) > LIMIT or len(stderr) > LIMIT:
                raise DomainError("OUTPUT_INVALID", "harness 输出超过允许大小。")
            # Do not persist stderr reasoning, raw event text or credentials in generic logs.
            if process.returncode != 0:
                signal_text = (stderr + stdout).decode("utf-8", errors="replace").lower()
                if any(s in signal_text for s in ("unauthorized", "authentication", "api key", "401", "login required", "not logged")):
                    raise DomainError("HARNESS_AUTH_REQUIRED", "harness 认证不可用；请在该产品中完成登录。")
                if any(s in signal_text for s in ("quota", "rate limit", "429", "insufficient credit")):
                    raise DomainError("HARNESS_LIMIT", "harness 额度或速率受限，自动评分已暂停。")
                raise DomainError("HARNESS_FAILED", f"{self.kind} 退出码 {process.returncode}，未发布评分。", retryable=True)
            decoded = stdout.decode("utf-8")
            if self.kind == "dsh":
                envelope, events = parse_dsh(decoded, process.returncode)
            else:
                events = parse_events(decoded)
                if any(e.get("type") in {"tool_use", "tool_call"} or e.get("item", {}).get("type") in {"command_execution", "mcp_tool_call", "web_search", "file_change"} for e in events):
                    raise DomainError("REQUIRES_INTERACTION", "评分会话发生工具调用，结果未发布。")
                if self.kind == "codex":
                    if not final.exists() or not any(e.get("type") == "turn.completed" for e in events):
                        raise DomainError("OUTPUT_INVALID", "Codex 未返回已完成回合的最终响应文件。")
                    envelope = parse_business_json(final.read_text(encoding="utf-8"))
                else:
                    parts = [e.get("part", {}).get("text", "") for e in events if e.get("type") == "text"]
                    envelope = parse_business_json("".join(parts))
            usage = next((e.get("usage") for e in reversed(events) if e.get("usage")), None)
            session = next((e.get("sessionId") or e.get("thread_id") for e in events if e.get("sessionId") or e.get("thread_id")), None)
            metadata = {"harness": self.kind, "harness_run_id": session, "model_id": self.config["model_id"] or "harness_default_unresolved",
                        "started_at": iso(started_wall), "ended_at": iso(), "elapsed_seconds": round(time.monotonic() - started, 2), "usage": usage,
                        "verification": "real_cli_execution", "input_mode": batch["mode"], "tools": "disabled_or_guard_denied"}
            return envelope, metadata
