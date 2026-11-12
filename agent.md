# 新 agent 接入入口

本项目通用入口为 [AGENTS.md](AGENTS.md)。若你的客户端只识别 `agent.md`，按以下步骤执行；维护本应用时使用 `AGENTS.md` 的流程 A，通过服务接任务时使用流程 B。

1. 阅读 [协作规则](docs/COLLABORATION.md) 和 [客户端适配](docs/CLIENTS.md)。确定服务地址、数据目录及工作区；默认是 `http://127.0.0.1:8765`、`.agentboard`、服务启动时指定的 workspace。
2. 用独立 profile 登记，不要复用另一 agent 的身份：

   ```powershell
   python -S agentctl.py --profile worker-unique register --name "你的名称" --client "实际客户端" --model "实际模型或 unknown"
   python -S agentctl.py --profile worker-unique tasks --status todo
   python -S agentctl.py --profile worker-unique show TASK_ID
   python -S agentctl.py --profile worker-unique claim TASK_ID
   ```

3. 认领成功后才编辑声明的资源，在独立终端运行 `python -S agentctl.py --profile worker-unique heartbeat --watch`，并按里程碑 `progress`。默认租约 120 秒、心跳间隔 30 秒；自定义租约须相应调整。
4. 上下文用量只能自报：可靠指标用 `reported`，估计用 `estimated`，无法读取保持 `unknown`。不要把完整对话放进状态。
5. 完成用 `submit TASK_ID --result "成果、验证和交接"`，管理员审核后才是 `done`；不能继续用 `release` / `block`。租约丢失或冲突时立即停止写入并重新核对所有权。
6. 状态变更走 CLI/API；私有凭据仅保留在 `.agentboard/credentials`。不要改 Markdown 的 owner、revision 或 status 来认领，不要提交运行目录。

使用中文沟通。详细维护步骤、Git 约定和故障恢复见 [维护手册](docs/MAINTENANCE.md)。本入口不假设 Codex、Claude、Gemini 或其他客户端会自动读到任何特定文件；启动 agent 时应明确指向此文件。
