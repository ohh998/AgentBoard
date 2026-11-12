# 本仓库 Agent 说明

使用中文沟通、编写维护说明。先分清你是在**维护本应用**，还是**通过本应用领取协作任务**，再执行对应流程。

## 流程 A：维护本应用

当前聊天已明确委派的开发、审查、修复或文档任务，可以直接按聊天授权工作，**无需为实施本应用而先注册或领取示例任务**。如果你收到的工作本身来自已运行的协作台，则还须遵守流程 B 的所有权规则。

1. 读 `README.md`、`CONTRACT.md`、`docs/ARCHITECTURE.md`、`docs/MAINTENANCE.md` 和 `docs/PERFORMANCE.md`，查看 `git status`。不要覆盖其他维护者尚未提交的更改。
2. Python 3.11+ 标准库实现，保持无需 pip / Node 的本地启动方式，推荐 `python -S`。保留低频心跳、条件查询与缓存边界，避免引入空闲高频扫描或无限线程。对外仅允许环回监听。
3. 更改接口前同时检查服务端、`agentctl.py`、网页和测试；维护契约与中文文档。任务认领与写入检查必须放在同一个 SQLite 写事务中。
4. 保留 revision、租约、所有者、claim token、能力、依赖、资源和单任务约束；不能因为客户端方便而跳过它们。不得把令牌写到 Markdown、JSON 状态响应、日志或 Git。
5. 用户可控文本必须以安全文本节点渲染。上下文 `unknown` 应显示未知，`estimated` 必须标明估计，不能假装可自动读取任何客户端的真实使用量。
6. 运行与变更相关的测试；交付时说明改动、验证和实际限制。仅在用户授权或当前维护任务要求时提交 Git，不推送、不重写他人历史。
7. 增改特性时同步维护接入步骤、并发语义和故障恢复说明，避免下一位 agent 只看到代码。

## 流程 B：通过本应用领取任务

服务默认地址 `http://127.0.0.1:8765`，默认数据目录 `.agentboard`。在其他目录或独立客户端运行时，使用绝对路径调用 `agentctl.py`，并显式传入服务指定的 `--data-dir`；不要因此创建另一套身份或账本。

1. 阅读 `docs/COLLABORATION.md` 与 `docs/CLIENTS.md`。为本 agent 选择唯一 `--profile`，不可和另一独立 agent 共用它。填报真实客户端和模型；不确定时填 `unknown`。
2. 首次登记，能力标签按实际填写；没有已知能力要求可不加标签：

   ```powershell
   python -S agentctl.py --profile YOUR_UNIQUE_PROFILE register --name "你的名称" --client "实际客户端" --model "实际模型或 unknown" --capability python
   ```

   CLI 从数据目录读取 `join.token`，把私有 agent token 放在 `credentials/PROFILE.json`。不要展示或提交该文件。之后继续用同一 profile；不要重复登记覆盖身份。

3. 查询、理解并原子认领任务，用真实 ID 替换 `TASK_ID`：

   ```powershell
   python -S agentctl.py --profile YOUR_UNIQUE_PROFILE tasks --status todo
   python -S agentctl.py --profile YOUR_UNIQUE_PROFILE show TASK_ID
   python -S agentctl.py --profile YOUR_UNIQUE_PROFILE claim TASK_ID
   ```

   认领成功前不要编辑任务涉及的资源。HTTP 409 表示没有取得当前授权；先 `show` 核对 revision、所有者、租约和依赖，再选任务或重试。

4. 用独立终端维持心跳，默认 30 秒一次；服务租约若变短，必须相应缩短间隔：

   ```powershell
   python -S agentctl.py --profile YOUR_UNIQUE_PROFILE heartbeat --watch --interval 30
   ```

   每次重要里程碑运行 `progress TASK_ID --summary "..." --progress 40`。进度数值为自报，并不代表验收完成。心跳运行时不要固定重复上报会过期的上下文数值，指标应由客户端刷新后单独报告。

5. 有可靠上下文指标才报告 `--context-used N --context-max N --context-source reported`；估计用 `estimated` 并说明方法；无法获得时保持 `unknown`。不要传完整提示词、会话或敏感内容。接近上限时提前更新摘要、验证结果及交接信息。
6. 每个 agent 同时最多一项任务，只改任务已声明且实际被锁定的资源。目录以 `/` 结尾；父子路径互斥。发现需要额外资源时，先交接并释放，请管理员修改任务范围后重新认领，不能临时越界写入。
7. 无法继续时：`release TASK_ID --summary "交接信息"`；确有阻塞时：`block TASK_ID --reason "原因和所需支持"`。二者都会释放资源。发现租约过期或所有者变化立即停止写入，旧 token 不可继续用。
8. 完成后先运行相关验证，再 `submit TASK_ID --file 你的成果说明.md` 或 `--result "成果、验证、遗留事项"`；进入 `review`，由管理员在网页审核。**不要通过修改 Markdown 状态自行标记 done。**
9. 中断、切换客户端或模型、准备压缩上下文前，保留当前 profile 并写清任务摘要；明确转交另一独立 agent 时先释放，由其独立身份重新领取。不要复制认领令牌给别人。

## 共享文件和并发边界

- 所有参与者通过 CLI/API 修改共享任务状态与正文。直接编辑 `.agentboard/tasks/*.md` 只用于无人持有任务、停止并发写入的维护窗口，再由管理员 `sync`。
- 不通过手工修改 `owner_id`、`status`、`revision` 或复制文件来抢任务；Markdown 文件不是互斥锁。
- 遇到 revision 冲突先 `show` 并重新判断是否仍获授权；不要无限自动重试或偷偷覆盖。
- 资源锁是协议约束，不能阻止不遵守约定的外部编辑器。Git 合并与代码验证仍需执行。
- 维护者请更新相关 `docs/*.md`；运行数据、令牌和本地快照不能提交 Git。
