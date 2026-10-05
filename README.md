# 招募中枢 · Boss 智能招聘工作台

版本：0.1.0。基于现有 agent harness 的招聘工作台，支持最多十份完整简历组批、岗位证据判断、确定性算分、断点恢复与人工沟通草稿。

**先看 [START_HERE.md](START_HERE.md)。实际验证范围见 [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)。** 本版已完成本机闭环和自动化回归；真实 Boss 账号、真实 DSH / WorkBuddy 调用与连续 24 小时运行尚未验收。当前环境 Codex 的真实调用超时，不能把合成样例当成在线评分成功。

## 运行

需要 Python 3.12 或更高版本，以及首次安装依赖所需的网络。工作台运行不需要 Node.js。

- Windows：双击 `start.bat`。
- macOS / Linux：在项目目录运行 `bash start.sh`。
- 终端显示启动完成后，在同一台电脑访问 `http://127.0.0.1:8765`。

脚本创建本地虚拟环境、按 `requirements.lock` 安装依赖，然后启动服务及守护任务。支持传入 `--port 8766` 等参数；Windows 使用 `start.bat --port 8766`，macOS / Linux 使用 `bash start.sh --port 8766`。第一次启动是空数据库，不会暗中下载简历或发送消息。

手工启动：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m recruit_agent serve
```

Windows 手工命令中的 Python 路径改为 `.venv\Scripts\python.exe`。已用 `uv` 创建、没有 pip 的环境，可用 `uv pip install --python .venv/bin/python -r requirements.lock`。

## 功能

| 功能 | 当前实现 |
| --- | --- |
| 工作台 | 总览、候选人、岗位与量表、批次任务、沟通草稿、连接设置六个视图 |
| 简历输入 | 正文、JSON、TXT、文本 PDF、DOCX；图片可通过本地中文 Tesseract OCR 解析 |
| 十人组批 | 满十人提交；不足十人等待 180 秒，可手动立即组批；上下文超额拆分，不截断正文 |
| 智能中枢 | DSH 默认；Codex / OpenCode CLI 适配器；WorkBuddy / ZCode 使用本项目 MCP |
| 评分 | AI 返回逐维度 0–4 或未知与原文证据；程序校验后计算分数、区间、覆盖率和复核队列 |
| 恢复 | SQLite WAL、持久任务、租约续期、过期结果隔离、逐人结果保存、最多三次有限重试 |
| 常驻 | 单机四个后台循环、每日预算到次日零点恢复、健康检查、每日备份、保留期限与删除 |
| Boss | 外部 `boss-agent-cli` 读取适配器及分页游标；真实字段须校准；平台动作受账号身份核验约束 |
| 沟通 | 保存草稿；具体人、具体内容批准后才可尝试发送；发送结果不确定时禁止自动重发 |

扫描 PDF 尚未实现逐页 OCR。解析器无法获得正文会明确失败；工作台不会用候选人卡片摘要冒充完整简历。

## 评分示例

岗位量表维度权重合计 100，已知维度等级为 0–4：

```text
证据下界 = Σ(维度权重 × 已知等级 / 4)
可能上界 = 证据下界 + 未知维度权重之和
信息覆盖率 = 已知维度权重之和 / 100
```

例如 40% 的维度等级 3，30% 等级 2，剩余 30% 未知：证据区间为 45.0–75.0，覆盖率 70%。未知不会作为零分。证据不足、OCR 问题、必需条件未知或存在冲突时进入补充信息队列。分数用于岗位复核；录用和淘汰由人决定。

## 接入与部署

- [docs/CONNECTORS.md](docs/CONNECTORS.md)：DSH、Codex、MCP 与 Boss 实际接入步骤。
- [docs/OPERATIONS.md](docs/OPERATIONS.md)：常驻服务、备份还原、观察 24 小时与故障处理。
- [docs/AI_HANDOFF.md](docs/AI_HANDOFF.md)：交给下一位 AI 开发者的入口、未完成任务与验收标准。
- [AGENTS.md](AGENTS.md)：代码维护约束。
- [docs/BOSS_RECRUIT_AGENT_SPEC.md](docs/BOSS_RECRUIT_AGENT_SPEC.md)：原始目标规格；其中的目标不等于本版已验收。
- [docs/RESEARCH.md](docs/RESEARCH.md)：公开项目、锁定提交与官方资料。
- [CAPABILITIES.json](CAPABILITIES.json)：本次交付的机器可读验证状态。

## 开发与测试

```bash
.venv/bin/python -m pytest -q
```

可选界面 DOM 与真实 HTTP 集成测试，需要 Node.js 24：

```bash
npm ci
npm test
```

界面无需前端编译。DOM 集成测试不等同实际浏览器截图或真实 WorkBuddy 客户端验收。

## 目录

```text
recruit_agent/       领域服务、队列、平台与 harness 适配器、HTTP、MCP、静态工作台
contracts/          输入输出 JSON Schema
prompts/            批量评估指令
config/             默认配置、Boss 字段映射、DSH 禁工具插件
migrations/         SQLite 顺序迁移
examples/           十人合成输入、人工格式样例与评分测试向量
tests/              后端、协议、恢复与适配器测试
scripts/            可选界面测试与交付包脚本
deploy/             服务管理模板
docs/               方案、接入、运维、AI 接棒说明及验证证据
runtime/            首次启动生成的本地数据库、附件、备份；不在交付包内
```

项目代码按 MIT 提供，见 `LICENSE`。外部 CLI 的源码和认证信息不随包分发；各自许可证和版本记录见研究文档。
