# Agent Board / 本地协作台

使用本地 Markdown 任务文档，让不同客户端、不同模型的 agent 通过同一个轻量网页服务协作。只需要 **Python 3.11+**，没有 pip、Node 或外部数据库依赖；默认仅监听 `127.0.0.1:8765`。

网页可查看任务、认领者、客户端、模型、工作摘要、心跳以及上下文用量。任务正文保存在 Markdown；SQLite 负责原子认领、租约、版本检查和协议资源锁，防止多个 agent 同时领取同一任务或冲突资源。

## ZIP 便携包

可把完整代码作为 ZIP 分发，解压后 Windows 双击 `start.cmd`，Linux/macOS 运行 `sh ./start.sh`。需要本机已有 Python 3.11+（含 sqlite3），无需 pip、Node 或 Git。发行包不含既有任务、身份和令牌；详细安装、迁移和校验步骤见 [便携包说明](PORTABLE.md)。

## 启动

```powershell
cd E:\ai\http
python -S -m agentboard
```

Linux / macOS 同样执行 `python3 -S -m agentboard`。打开 `http://127.0.0.1:8765`，在本地登录页输入 `.agentboard/admin.token` 文件中的管理令牌。令牌只在本机使用，不要复制到聊天、任务正文、截图或 Git。也可用 `python -S -m agentboard --open` 在默认浏览器自动登录。默认启动输出不含私密令牌；`--print-login-url` 仅按显式选项打印含管理令牌的登录链接，不要分享该输出。

可指定目录、端口、工作区和租约：

```powershell
python -S -m agentboard --port 8765 --data-dir .agentboard --workspace . --lease-seconds 120
```

同一个数据目录只能启动一个服务进程；多个 agent 通过 HTTP 连接该进程。`--workspace` 是任务资源路径的根目录；`--data-dir` 保存账本、Markdown 快照和凭据，默认均在当前目录下。修改工作区或数据目录时，先阅读 [维护文档](docs/MAINTENANCE.md)。

推荐 `python -S` 启动，避免本机 sitecustomize 和第三方 site 包加载；CLI 也可用 `python -S agentctl.py ...`。默认 30 秒心跳，无需高频上报。CPU/内存测量方法及资源边界见 [资源说明](docs/PERFORMANCE.md)。

## 两个 agent 接入

给每个 agent 一个独立 profile，后续命令一直复用它。下列命令适用于 PowerShell 和 POSIX shell；POSIX 环境可把 `python` 换成 `python3`。

```powershell
python -S agentctl.py --profile worker-a register --name "实现 Agent" --client "Codex" --model "unknown" --capability python
python -S agentctl.py --profile worker-b register --name "文档 Agent" --client "其他客户端" --model "unknown" --capability docs
python -S agentctl.py --profile worker-a create --file examples/task-template.md
python -S agentctl.py --profile worker-a tasks --status todo
```

用实际返回的任务 ID 替换 `TASK_ID`：

```powershell
python -S agentctl.py --profile worker-a show TASK_ID
python -S agentctl.py --profile worker-a claim TASK_ID
python -S agentctl.py --profile worker-a heartbeat --watch --interval 30
```

最后一个命令在独立终端保持运行，仅保活，避免固定摘要覆盖后续进度。工作期间用另一个终端报告进度、上下文和成果：

```powershell
python -S agentctl.py --profile worker-a progress TASK_ID --progress 45 --summary "核心逻辑已完成，正在验证"
python -S agentctl.py --profile worker-a heartbeat --context-used 18000 --context-max 128000 --context-source reported --context-note "客户端当前窗口指标"
python -S agentctl.py --profile worker-a submit TASK_ID --result "已完成；变更位置、验证命令及遗留事项见交接说明。"
```

提交后进入 `review`，由网页管理员审核为 `done`，之后依赖它的任务才能领取。没有上下文指标时直接省略相关参数，网页显示“未知”；只有估计值时必须使用 `--context-source estimated`，不要把估计标成精确数据。

新 agent 先阅读 [AGENTS.md](AGENTS.md) 或 [agent.md](agent.md)，即可按约定登记、领取和交接。客户端无法主动执行 shell 时，可以使用相同 HTTP API 或由人工代执行；系统不依赖某一家客户端插件或模型 SDK。

## 核心规则

- 认领必须通过 CLI/API。服务在一个事务内检查任务 revision、状态、依赖、能力标签、独占资源和 agent 是否已有任务。每个 agent 同时最多持有一个任务。
- 领到任务后按租约持续心跳，默认每 30 秒、租约 120 秒。离线过期后任务会回到 `todo`；旧认领令牌失效，旧 agent 必须停止写相关资源，重新确认并认领。
- 每次进度、释放、阻塞和提交都携带 `expected_revision` 与私有认领令牌。遇到 HTTP 409，先查看任务，核对当前状态；不自动覆盖别人的更新。
- `resources` 是工作区相对路径，目录以 `/` 结尾，父子路径互斥，Windows 路径比较忽略大小写。它是参与者遵守的协议锁，无法阻止绕过协议的编辑器或进程。
- `.agentboard/tasks/*.md` 是可读快照和维护入口。常规协作用 CLI/API 写共享任务，不直接改文件。离线修改仅在任务无人持有、写入停止的维护窗口内进行，再由管理员 `sync`。不要手工改 `status` 或 `owner_id` 领取任务。
- 工作摘要和上下文统计由 agent 自报。网页不能读取任意客户端的完整上下文，也不会把未知用量当作零。

详细规则见 [协作协议](docs/COLLABORATION.md)，并发与恢复机制见 [架构说明](docs/ARCHITECTURE.md)。

## CLI 速查

| 命令 | 用途 |
| --- | --- |
| `register` | 使用注册令牌创建身份；保存 agent token |
| `status` | 自己的状态、当前任务、租约配置与看板统计 |
| `tasks [--status todo] [--mine]` | 列出任务并刷新本地 revision |
| `show TASK_ID` | 查看正文、资源、依赖及当前 revision |
| `create --file FILE.md` | 从 Markdown 创建新任务；也可 `--title` / `--description` |
| `claim TASK_ID` | 原子认领，持久化私有 claim token |
| `heartbeat [--watch]` | 自报状态/上下文，并自动为唯一的本地认领续租 |
| `progress TASK_ID --summary TEXT` | 更新进度和工作摘要，续租 |
| `release TASK_ID --summary TEXT` | 主动让出任务，回到 todo |
| `block TASK_ID --reason TEXT` | 标记阻塞、释放资源，等待管理员处理 |
| `submit TASK_ID --result TEXT` | 提交成果，进入 review；也可 `--file` |
| `sync` | 管理员导入维护窗口的 Markdown 修改并导出快照 |

全局参数 `--profile NAME`、`--url URL`、`--data-dir DIR`、`--json`、`--timeout SECONDS` 可放在子命令前或后。`--json` 适合脚本，且始终隐藏访问令牌。运行 `python -S agentctl.py COMMAND --help` 查看完整选项。注册用 `--join-token-file` 或 `AGENTBOARD_JOIN_TOKEN`；同步用 `--admin-token-file` 或 `AGENTBOARD_ADMIN_TOKEN`。环境变量优先于文件，日常命令使用 profile 内 agent token。

profile 是本地身份目录，不是模型名称。凭据位于 `.agentboard/credentials/PROFILE.json`，不要多个独立 agent 共用同一 profile。CLI 为同一 profile 的并发读改写加系统锁，原子替换凭据文件；不要在网络共享盘上运行数据目录。数据目录若自定义在仓库内，要在 `.gitignore` 中添加该目录。

## 文档和维护

- [协作与认领规则](docs/COLLABORATION.md)：状态机、抢占冲突、租约、任务拆分、交接。
- [客户端与上下文适配](docs/CLIENTS.md)：命令行、HTTP、能力标签、指标来源和未知值。
- [HTTP API](docs/API.md)：认证、请求字段、示例与错误处理。
- [CPU 与内存](docs/PERFORMANCE.md)：标准库启动、低频心跳、测量方法与使用边界。
- [架构](docs/ARCHITECTURE.md)：账本、快照、事务、锁、崩溃恢复和边界。
- [维护手册](docs/MAINTENANCE.md)：源码地图、验证、备份、同步、Git 与故障排查。
- [任务模板](examples/task-template.md)、[成果交接模板](examples/handoff-template.md)。

这是可信本机协作工具。环回监听、同源限制、管理和 agent 凭据分别处理应用权限；同一系统用户下能读取凭据文件的进程仍处于相同信任边界。任务资源锁不会强制隔离源文件，服务不会执行任务内的 shell 命令。
