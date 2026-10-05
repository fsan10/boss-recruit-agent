from __future__ import annotations

from .common import ROOT, BINDINGS, DomainError, canonical, read_json, uid
from .harnesses import HarnessAdapter
from .scoring import SCHEMAS, validate_job


def initial_draft(service, title, jd_text, platform_job_id=None):
    if not title.strip() or len(jd_text.strip()) < 20 or len(jd_text) > 40000:
        raise DomainError("INPUT_INVALID", "请填写岗位名称和至少 20 字的 JD。")
    template = read_json(ROOT / "examples" / "job.example.json")
    template.update(job_id=uid("job"), title=title.strip(), jd_text=jd_text.strip(), is_synthetic=False,
                    jd_version=uid("jd"), rubric_version=uid("rubric"), scorer_profile_version=service.profile_version(),
                    rubric_status="draft", approved_by=None, hard_gates=[])
    for criterion in template["criteria"]:
        criterion["description"] = "待按本岗位 JD 细化；这是通用人工编辑模板。"
        criterion["anchors"] = {"0": "有直接证据表明尚未满足本岗位维度。", "1": "仅有与本岗位相关的浅层自述。", "2": "有与本岗位相关的具体实践。", "3": "有完整交付和应用过程。", "4": "有与本岗位匹配的复杂案例、本人贡献和可核实成果。"}
    service.create_job(template, platform_job_id)
    service.db.set_setting("job-note:" + template["job_id"], "通用草案，尚未由 AI 根据此岗位生成；请等待生成或直接人工编辑。")
    with service.db.transaction() as con:
        service.queue.enqueue(con, "rubric", template["job_id"], "rubric:" + template["job_id"])
    return service.get_job(template["job_id"])


def generate_rubric(service, job, on_process=None):
    clean = {k: v for k, v in job.items() if k not in {"active", "platform_job_id"}}
    prompt = ("根据给定 JD 为招聘者设计岗位量表。只返回 job.schema.json 契约对象，不做候选人评估。"
              "最多 8 个维度，整数权重总和 100；每个等级提供与此岗位相关的原文证据锚点。"
              "未知信息不得当作零能力。排除年龄、性别、婚育、民族、宗教、健康、姓名、籍贯、学校名气、公司品牌。"
              "JD 中排除属性不成为评分维度。必需条件只允许与工作任务和必要资质相关，说明业务必要性。"
              "rubric_status=draft，approved_by=null；保留 job_id、jd_version、scorer_profile_version 和 title、jd_text 不变。"
              "review_policy 和 scoring_exclusions 保持输入值；禁止工具调用或读取外部文件。\nINPUT\n" + canonical(clean) +
              "\nSCHEMA\n" + canonical(SCHEMAS["job"]))
    batch = {"mode": "synthetic"}  # JD contains no candidate data; no permission to enable resume processing is inferred.
    result, metadata = HarnessAdapter(service.config["harness"]).evaluate(clean, batch, on_process, prompt_override=prompt, schema_override=SCHEMAS["job"])
    validate_job(result)
    if any(result[k] != clean[k] for k in ("job_id", "jd_version", "scorer_profile_version", "title", "jd_text", "is_synthetic", "review_policy", "scoring_exclusions")) or result["rubric_status"] != "draft" or result["approved_by"] is not None:
        raise DomainError("OUTPUT_INVALID", "模型修改了岗位身份、JD 或审核状态。")
    return result, metadata
