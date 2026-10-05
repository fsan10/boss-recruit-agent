# 本机运行与 24 小时验收

本版单机运行：API、调度、采集、评分共用本地 SQLite。每批 CLI 会话启动一次，最终结果校验后退出。机器必须持续在线并保持唤醒；程序停机时任务保留，恢复后从持久断点继续。

## 常驻运行

先运行 start.bat / start.sh 安装依赖，配置并实测实际 harness 和 Boss。系统服务直接使用虚拟环境解释器启动，避免每次重启重新安装依赖。工作台仅监听 127.0.0.1，当前没有多用户认证，不作为公开网站部署。

### Windows

默认 Python 3.12 引导路径是 `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`，用户常用路径对应 `C:\Users\win\AppData\Local\Programs\Python\Python312\python.exe`。安装在其他位置时，将完整路径写入 RECRUIT_BOOTSTRAP_PYTHON 环境变量；不使用裸 python 或 py 的默认版本。

在任务计划程序创建“招募中枢”任务：登录当前用户时触发；程序填写本机 cmd.exe 的完整路径；参数填写 `/d /c "完整项目路径\run-worker.bat"`；起始于项目目录。设置“任务失败每 1 分钟重启，最多 3 次”“任务已运行时不启动新实例”，取消默认三天运行上限和空闲自动停止。使用需要访问产品 profile 的实际用户会话；WorkBuddy 桌面不能假定可在无交互服务账户中被操作。

run-worker.bat 只启动已经安装的服务，不含账号密码。运行日志在 runtime/logs。Windows 启动脚本和任务计划配置尚未在本次 Linux 开发环境实测。

### Linux

编辑 `deploy/recruit-agent.service` 中三个项目绝对路径，复制为 `~/.config/systemd/user/recruit-agent.service`。使用用户服务：

```bash
systemctl --user daemon-reload
systemctl --user enable --now recruit-agent
systemctl --user status recruit-agent
journalctl --user -u recruit-agent -n 100
```

如需用户退出登录后继续运行，机器管理员为该用户配置 linger。评分认证环境可放在模板中的专用 model.env，文件权限 0600；不要添加 Boss Cookie 或认证内容。上面命令是用户部署说明，本次没有在用户服务器注册服务。

### macOS

编辑 `deploy/local.recruit-agent.plist` 的绝对路径，先创建 runtime/logs，将模板放进用户 LaunchAgents 后，按 launchctl 的用户域方式加载。模型环境和 profile 须明确配置，GUI 登录环境可能不同于终端。模板做 XML / plist 结构校验，尚未做 macOS 真机验收。

## 恢复与预算

- 默认评分并发 1、采集并发 1；MCP 与守护共享这项限制，不能同时抢同一批。
- 租约 90 秒，后台独立续租；过期运行最多三次恢复，完成的成员不重评。
- 网络错误有限退避。认证、平台风险、字段映射、模型不可用明确暂停，处理原因后再恢复。
- 本项目自己的每日预算持久保存，到配置时区次日零点自动恢复。平台或供应商真正的限额 / 认证错误不会被自动解除。
- 服务重启时未确认的 SENDING 变成 OUTCOME_UNKNOWN；必须回读核实，不自动重发。
- 实际模型超时停止子进程树。供应商可能已经消耗额度，不能承诺计费也恰好一次。

## 备份与删除

设置页可下载一致性备份；守护每小时检查维护，到期每天备份一次。首次自动检查在启动约一小时后；需要立即备份可使用工作台按钮。

```bash
.venv/bin/python -m recruit_agent backup --runtime /absolute/path/runtime
.venv/bin/python -m recruit_agent restore --archive /absolute/path/backup.zip --runtime /absolute/path/empty-runtime
```

恢复目录必须为空，恢复后自动关闭真实采集、自动评分和发送；先确认数据与配置再启用。备份包含候选人本地数据及附件，不包含外部 CLI 认证目录，应受控保管。

原始文本默认保留 30 天，候选人评估按最后快照 90 天保留；备份默认 30 天。删除会清理候选人数据库关系、批次正文及其本地附件，但已有备份按期限删除，供应商或 DSH 会话日志不在本地删除范围。v0.1 不收集浏览器截图。

## 真正 24 小时的验收

不能以改系统时间、模拟心跳或几十分钟运行替代实际时间。开始前先通过真实账号、完整简历、十人真实 harness、断点恢复验收。

1. 保存开始时间、版本和开启的渠道 / 预算，不含 Cookie 或个人简历。
2. 连续观察至少 86,400 秒，服务心跳按真实时间记录，最大间断不超过 180 秒。
3. 查询 `/api/runtime/observation` 得到观察跨度、心跳数量和最大间断；同时看 `/api/status` 的实际账号采集与模型最后成功记录。
4. 核对真实候选人唯一标识、正文完整性、逐人评分、分页游标、积压、暂停原因和到期预算恢复；仅服务存活不算处理成功。
5. 保存脱敏验收报告及实际起止时间。真实平台证据成立后才能将能力报告 `real_platform_24h_accepted` 标为 true。

当前该标志始终 false，这次交付没有提前给实际 24 小时验收盖章。

## 常见状态

| 状态 / 错误 | 处理 |
| --- | --- |
| CAPABILITY_UNAVAILABLE | 检查可执行路径、安装版本、DSH 配置或平台账号稳定 ID；不可伪造可用状态 |
| SCORER_VERSION_CHANGED | 保存中枢配置后到岗位重新审核启用版本 |
| HARNESS_TIMEOUT | 核验产品自身认证和最小真实调用，查看明确超时；任务有限重试，不发布空结果 |
| FIELD_MAPPING_REQUIRED | 校准当前账号脱敏响应，保留版本，不按姓名 / 下标猜测 |
| OCR_FAILED | 提供完整正文或文本 PDF；安装本地中文 OCR；扫描 PDF 逐页 OCR 尚待实现 |
| NEEDS_INFORMATION | 阅读已有证据与核实问题，补充信息；未知不当零分 |
| OUTCOME_UNKNOWN | 在官方平台或精确消息回读中核验，不直接点再次发送 |
| BUDGET_EXHAUSTED | 本项目预算到次日恢复；供应商或平台限制需要按真实提示处理 |
