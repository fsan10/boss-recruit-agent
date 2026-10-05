# 现有中枢与 Boss 接入

这里是本版适配器的实际协议。认证在用户已选产品中完成，工作台不收集登录口令。外部工具独立安装，不随源码包分发。先探测，再用十人合成数据真实调用，最后测试获授权的真实资料。

## WorkBuddy / ZCode：MCP

在工作台选择中枢 `mcp`，填写用于版本记录的客户端 / 模型标识，保存并重新审核启用岗位。设置页“WorkBuddy / ZCode MCP 接入”按钮生成配置，包含本机虚拟环境 Python 的绝对路径和同一 runtime 数据库路径。

服务命令为：

```bash
.venv/bin/python -m recruit_agent mcp --runtime /absolute/path/to/runtime
```

Windows 使用项目 `.venv\Scripts\python.exe`。MCP 客户端应设置 `PYTHONPATH` 为项目根目录；工作台生成的 JSON 已包括这个字段。stdout 专用于 MCP 协议；不通过 stdout 输出调试消息。

调用顺序：get_job_profile → get_next_batch → 定期 heartbeat_batch → submit_batch_evaluation → get_batch_report。领取响应含完整 job、batch、instructions 与 evaluation_schema。租约默认 90 秒，客户端每 30 秒续租；没有任务时返回空结果，不需要反复抢占。

客户端必须把 lease_token、fencing_token 留在提交参数中，不把它们塞入业务 evaluation。输出按 candidate_id 对应，不能按列表位置回写。合法成员可单独完成，报告显示失败成员原因。MCP 共有九个默认工具，另外包含候选人详情、入口查询、草稿创建、运行状态；没有批准或发送工具。

所用 WorkBuddy / ZCode 产品的实际配置入口、登录和模型额度由该产品管理。这次只做过官方 SDK stdio 客户端到本项目的协议集成测试，没有在这两个产品里真实执行十人任务。

## DSH：默认常驻执行器

来源：[DeepSeek Harness 锁定源码](https://github.com/deepseek-ai/deepseek-harness/tree/5badb15009ae1756c3afe0ae0cef1faafc290ccc)。先按该版本官方文档安装和配置专用 headless profile。

设置页填写：

- 中枢 `dsh` 和实际可执行路径；
- 专用 `dsh_config_dir`，对应 DSH_HOME；
- 实际 `dsh_profile` 和模型标识；
- 真实资料处理允许开关；
- 自动评分开关、每日调用预算与单次超时。

模型标识用于追溯；DSH 模型本身由专用 profile 配置，须与填入标识一致。不能只改工作台标识就认为切换了 DSH 模型。

本项目把完整量表、十人评分正文、证据块和输出契约通过 stdin 交给：

```text
dsh --profile <configured-profile> --patch <temporary-scoring.patch.yml> --json
```

临时 patch 插入 `config/scoring-guard.mjs`，通过官方插件 tools.guard 拒绝所有工具调用。独立配置目录不能加载 Boss Cookie、浏览器/CDP 或通用 MCP 工具；应在用户机器实测插件加载和工具拒绝。

只有退出码 0、`status.phase=turn_end` / `reason=completed`、恰好一个 final.text，且 final.text 通过业务校验后才发布结果。出现工具调用、错误、空 final、无终结事件或无完整 JSON 都失败。模型没有被调用成功时不回退到关键词算法。

本次未安装 DSH，真实认证、插件加载及十人模型调用未验收。

## Codex CLI

本次检查了开发环境 `codex-cli 0.159.2` 的 exec 参数。适配器使用完整 stdin 输入，`--json`、`--ephemeral`、`--ignore-user-config`、`--output-schema` 和 `--output-last-message`，只读沙箱、禁止审批、禁用搜索及相关工具特性。最终文件需配合 turn.completed 事件确认，JSONL 事件本身不是评分结果。

适配器保留产品认证相关的必要环境，但不继承 Boss 变量。请在用户机器先确认认证与命令版本；环境中如通过 CODEX_HOME 指向产品配置目录，应由用户自己管理。填写准确模型标识，启用前重新审核岗位版本。

本次真正的十人合成调用在 300 秒后超时，最小 JSON 调用在 30 秒后也超时，没有评分发布。未确认具体原因。执行参数和超时处理已由专用模拟进程回归验证，不能据此宣称在线可用。

## OpenCode

适配器采用 `opencode run --format json --file <input-file>`，解析 text 事件，不假设全文 stdout 是业务 JSON。真实最终事件和工具权限尚未实测；必须在隔离环境核验仅有本批输入文件可读、其他工具被禁用，再配置 `opencode_tools_disabled_verified`。当前不作为已验收的常驻中枢。

WorkBuddy Enterprise `codebuddy` CLI 尚无专用适配器。支持某个产品的 MCP 不等于已支持所有该产品的 CLI 形态。

## Boss CLI：真实资料采集

外部来源：[boss-agent-cli](https://github.com/can4hou6joeng4/boss-agent-cli)，MIT，锁定提交 `74754f5ba368450d71e769abfec76c98709e593f`。请按上游说明安装到独立环境，并配置用户自己的招聘端 profile。账号认证和验证码由官方产品处理。

设置页填写 CLI 可执行路径、专用 data_dir 和目标账号绑定值；在岗位页填写实际 Boss 职位 ID。若未安装，`probe` 返回 CAPABILITY_UNAVAILABLE，已经导入的简历仍可处理。

本项目包装 `boss --json [--data-dir <directory>] <command>`：

| 调用 | 用途 |
| --- | --- |
| schema / doctor / --version | 本地能力报告，不代表真实登录成功 |
| status --live | 当前 profile 的只读登录检查 |
| hr applications / chat / recommendations | 按职位列表读取，分页及页内 offset 不跳过十人之后的尾部 |
| hr resume | 用稳定人 ID、职位与 security 参数获取在线简历 |
| me --section user | 平台动作前核验稳定当前账号 ID；目前上游缺字段，动作阻止 |
| hr chatmsg / download-resume | 仅在身份成立后，核验真正已共享的附件卡片和具体 message ID |

`config/boss-field-map.json` 是待真实校准的字段图，不是已通过所有账号验证的协议。修改它需要保留脱敏样本及来源版本。缺少稳定 ID 或正文字段时暂停并返回 FIELD_MAPPING_REQUIRED；不按姓名匹配。

初始实时读取关闭。默认 15 分钟轮询、至少 3 秒读间隔、每天最多 300 次 CLI 操作、60 份新详情。开启主动推荐需要单独启用主动寻源。主动搜索渠道尚未实现。

当前上游缺少可读稳定账号 ID，所以入口刷新、附件和真实发消息会阻止。只读采集仍须经过当前账号字段校准与用户机器实测；不能仅看到 logged_in=true 就宣称平台完整接通。

## 能力更新规则

安装版本可运行、协议测试通过、真实模型通过、真实平台通过、连续 24 小时通过分别记录。切换模型、prompt、schema、profile 或中枢后，重新审核启用岗位评分版本，旧运行保留，旧未启动任务由版本更新取消。禁止把不同来源的结果悄悄混成同一评分版本。
