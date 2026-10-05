# GitHub 与 harness 核验记录

调研日期 2026 年 10 月 4 日。只读检查了公开 README、选定源码、GitHub 仓库元数据和官方文档，未登录 Boss、未执行这些项目的真实招聘操作，也未做安全审计。Star 是检索时快照；pushed_at 是仓库最近推送时间，不等于最后功能发布或线上兼容性证明。

## 招聘端相关项目

| 项目 | 已核验内容 | Star 快照 | 最近推送 UTC | 许可证记录 | 本项目选择 |
| --- | --- | --- | --- | --- | --- |
| [can4hou6joeng4/boss-agent-cli](https://github.com/can4hou6joeng4/boss-agent-cli) | 招聘端申请、简历、聊天、推荐、附件命令及 JSON 信封 | 2040 | 2026-10-03 | MIT | 优先封装采集适配器，当前账号仍须实测 |
| [Viy1204/boss-daily-brief](https://github.com/Viy1204/boss-daily-brief) | 现有 harness 的招聘简报 Skill，依赖 boss-cli 和飞书 CLI | 109 | 2026-04-28 | MIT | 参考流程，不引入飞书依赖和原筛选规则 |
| [Snseam/boss-zhipin-mcp](https://github.com/Snseam/boss-zhipin-mcp) | 招聘端 MCP、浏览器简历截图、分享链接和简单评分 | 139 | 2026-03-31 | API 未识别且根目录未见 LICENSE | 参考能力划分，独立实现 |
| [joohw/boss-cli](https://github.com/joohw/boss-cli) | 招聘端 CDP CLI；简历截图及百度云 OCR 配置 | 142 | 2026-09-20 | GPL-3.0 | 可选外部工具，首版无需引入 |
| [LIghtJUNction/BossKit](https://github.com/LIghtJUNction/BossKit) | CLI/MCP，含受限招聘端简历和推荐读取 | 2 | 2026-08-22 | MIT | 参考结构化读取与动作核验，小样本项目 |
| [wensia/boss-zhipin-automation](https://github.com/wensia/boss-zhipin-automation) | README 展示二维码登录、任务/候选人管理和日志 | 56 | 2026-02-11 | API 未识别 | 仅作产品布局参考，未读全量源码 |
| [Littlesheepxy/sourcing-copilot](https://github.com/Littlesheepxy/sourcing-copilot) | README 展示 Chrome 插件与模型 API 招聘助手 | 1 | 2025-08-14 | API 未识别 | 仅作形态参考，非首选常驻底座 |

MIT/GPL 标记来自 GitHub 元数据及仓库可见许可证。未识别不等于已经获得使用授权。本方案未复制上述项目源码；今后引入依赖须保存各自许可证和版本记录。

## 影响方案的源码发现

boss-agent-cli 的招聘命令文档分别定义在线简历、附件请求接收与下载，JSON schema 是可用能力入口。其文档同时注明部分附件协议整合使用离线回归测试，不代表所有账号都可用。故优先适配是设计判断，不能等同真实平台验收。

- [锁定提交的招聘命令文档](https://github.com/can4hou6joeng4/boss-agent-cli/blob/74754f5ba368450d71e769abfec76c98709e593f/docs/commands.md)
- [在线简历命令源码](https://github.com/can4hou6joeng4/boss-agent-cli/blob/74754f5ba368450d71e769abfec76c98709e593f/src/boss_agent_cli/commands/recruiter/resume.py)
- [附件消息核验源码](https://github.com/can4hou6joeng4/boss-agent-cli/blob/74754f5ba368450d71e769abfec76c98709e593f/src/boss_agent_cli/api/recruiter_resume.py)

boss-zhipin-mcp 的 evaluator.py 使用关键词加减分；server.py 的批量评分还有年龄过滤及学历等默认加分。ocr.py 直接依赖 Anthropic API。它的功能组织可参考，不能把原评分器当作本需求的完整智能量表。

- [evaluator.py](https://github.com/Snseam/boss-zhipin-mcp/blob/06a1a7d804aa80131a066bddc1879ac4bc72f841/evaluator.py)
- [server.py](https://github.com/Snseam/boss-zhipin-mcp/blob/06a1a7d804aa80131a066bddc1879ac4bc72f841/server.py)
- [ocr.py](https://github.com/Snseam/boss-zhipin-mcp/blob/06a1a7d804aa80131a066bddc1879ac4bc72f841/ocr.py)

boss-daily-brief 的 Skill 将年龄上限和年龄降权列入流程。本项目只参考“一次对话执行招聘简报”的组织方式，重新定义岗位证据评分，不沿用这些条件，也不默认向飞书发送候选人资料。

- [锁定 Skill 源文件](https://github.com/Viy1204/boss-daily-brief/blob/0c7e84da0801d018a8d067d4e1f039b9f4127a32/SKILL.md)

joohw/boss-cli 默认主要输出文本，其 OCR 源码使用百度在线识别并需额外密钥。因此不应默认它已经提供本需求所需的十人统一 JSON 和免费本地 OCR。

- [简历 OCR 源码](https://github.com/joohw/boss-cli/blob/50eea0e2ec2aa22b3c5b1ca489bdd5264ad0d428/src/ocr/resume_ocr.ts)

BossKit 包含招聘端批量在线简历读取及精确动作参数，但其规模和本地规则不构成本项目评分可靠性的证据。真实链接、身份和收件人仍由本项目核验。

## 锁定提交

| 项目 | 本次读取的提交 |
| --- | --- |
| boss-agent-cli | 74754f5ba368450d71e769abfec76c98709e593f |
| boss-daily-brief | 0c7e84da0801d018a8d067d4e1f039b9f4127a32 |
| boss-zhipin-mcp | 06a1a7d804aa80131a066bddc1879ac4bc72f841 |
| joohw/boss-cli | 50eea0e2ec2aa22b3c5b1ca489bdd5264ad0d428 |
| BossKit | bf1e1094b8d0b6032b573b27bc119b5610e7ef11 |
| DeepSeek Harness | 5badb15009ae1756c3afe0ae0cef1faafc290ccc |
| ZCode CLI 文档及主分支核验 | 29628c9acdb81b703bbd4080c207a0e7ce5e276e |

上游可能继续更新。开发时可升级到经过验证的版本，但须更新能力报告及兼容测试；不在生产运行中自动追随最新提交。

## 现有 harness 的官方能力依据

| harness | 本次确认的入口 | 尚未实测/不能据此推断 |
| --- | --- | --- |
| DSH | 官方 headless profile、stdin 输入、--json 事件流；源码 final.text | 用户机器的认证、工具权限和具体模型额度 |
| Codex CLI | 本环境 codex-cli 0.159.2 的 exec --help，stdin、--output-schema、--json、--output-last-message | 用户安装版本及当前认证可否无人值守 |
| OpenCode | 官方 run、--format json、--file、serve/attach 文档 | 当前实际最终事件结构和权限配置 |
| WorkBuddy 桌面 | 官方文档检索可确认 MCP 与连接器形态 | 桌面版是否提供可调度无头 CLI |
| WorkBuddy Enterprise CLI | 腾讯官方文档的 codebuddy -p、--output-format、Schema 和 --serve | 用户是否有该产品/认证；不能等同桌面版 |
| ZCode | 官方 CLI 仓库文档有 MCP 和插件配置 | 未在本环境运行，未据此确认具体无头协议 |

来源：

- [DSH headless 文档](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/bundle/headless/README.md)
- [DSH final 事件源码](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/bundle/headless/src/json-stream.ts)
- [DSH MCP client 官方配置](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/mcp/mcp-client/README.md)
- [OpenCode CLI](https://opencode.ai/docs/cli/) 与 [服务接口](https://opencode.ai/docs/server/)
- [WorkBuddy 官方 MCP 文档](https://www.workbuddy.ai/docs/zh/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/MCP-Guide) 与 [连接器文档](https://open.workbuddy.cn/docs/connector)；本次网页正文访问失败，使用官方索引摘要，不据此填写具体 CLI 参数。
- [腾讯 WorkBuddy Enterprise CLI 命令参考](https://cloud.tencent.com/document/product/1831/137026)
- [ZCode CLI 官方文档](https://github.com/zai-org/ZCode/blob/29628c9acdb81b703bbd4080c207a0e7ce5e276e/apps/zcode-cli/README.md)

没有将用户所列的全部 harness 宣称为已适配。选择 DSH 作为默认中枢是基于其官方一次任务入口和用户偏好的设计判断。守护、租约、身份、批次契约和评分量表均是本方案新增设计。
