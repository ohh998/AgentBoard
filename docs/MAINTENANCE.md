# 维护、验证与恢复手册

先读根目录 `AGENTS.md` 的流程 A。用户在当前聊天委派的应用开发任务不要求为开发本身先加入协作台；若是从协作台领取的任务，照常遵守所有权与资源范围。

## 源码地图

| 文件 | 职责 |
| --- | --- |
| `CONTRACT.md` | 后端、网页、CLI 与测试共同接口契约 |
| `agentboard/__main__.py` | 启动参数、单实例及服务生命周期 |
| `agentboard/server.py` | HTTP/API、认证、同源校验、静态页面 |
| `agentboard/store.py` | SQLite 事务、租约、任务状态、事件与 Markdown |
| `agentctl.py` | 跨客户端 CLI、profile 锁与私有认领凭据 |
| `web/index.html` / `app.js` / `styles.css` | 中文网页、轮询、详情与管理员操作 |
| `tests/` | 并发、权限、租约、Markdown 与 HTTP 行为验证 |
| `docs/`、`examples/` | 维护协议、接入指南与可改模板 |

## 修改流程

1. `git status --short` 查看其他 agent 的工作；确定自己负责的文件，不回退他人改动。
2. 改接口时同步契约、服务器、CLI、UI、测试和文档。新增写入必须走事务内所有权/版本检查。
3. 用独立临时数据目录运行验证，不在用户实际 `.agentboard` 账本上做破坏性实验。
4. 运行相关检查，检查任务文档冲突及浏览器显示，再总结验证结果。
5. 审查 `git diff --check` 与 `git diff --stat`；只提交源代码、测试、模板和维护文档。

## 验证命令

```powershell
python -m unittest discover -s tests -v
python -m compileall -q agentboard agentctl.py
python -S agentctl.py --help
git diff --check
```

启动独立测试实例时指定新数据目录和端口，例如：

```powershell
python -S -m agentboard --data-dir .agentboard-smoke --port 8766 --lease-seconds 120
python -S agentctl.py --data-dir .agentboard-smoke --url http://127.0.0.1:8766 --profile smoke register --name "Smoke" --client "CLI" --model "unknown" --capability python
python -S agentctl.py --data-dir .agentboard-smoke --profile smoke create --file examples/task-template.md
```

完成后确认两个 agent 并发领取同一任务只有一个成功；重叠 resources 不能同时认领；非 owner 不能进度/提交；租约过期后旧 token 无效；未知/估计上下文不误导；CLI 不输出令牌；Markdown 冲突保留文件；服务重启后账本与待导出项可恢复。测试覆盖范围应随变更调整，不用重复测试无关行为。

## Markdown 维护窗口

常规协作用 API 修改共享任务正文和状态，避免直接编辑运行中的 `tasks/*.md`。确有离线维护需求时：

1. 确认受影响任务没有活跃 owner，暂停所有可能修改它们的 agent、网页操作和同步。必要时停止服务，保留租约/后台程序的停工安排。
2. 备份数据目录和准备修改的 Markdown。只修改 title、description、priority、resources、depends_on、required_capabilities；保留现有 id 与 revision，不改状态/owner/令牌。
3. 启动服务但暂不恢复参与者，运行 `python -S agentctl.py --data-dir DIR sync`。它从 `admin.token` 读取权限，不需要先登记 agent。
4. 检查响应和网页 Markdown 冲突。确认导入结果、资源和依赖都正确后，再恢复工作。

文件格式示例见 `examples/task-template.md`；JSON front matter 不支持任意 YAML 语法。创建新文件不要使用已有任务 id，revision 0、status todo。创建新任务更推荐 CLI `create --file`，这样无需等待同步。

服务可检测已观察到的外部内容/hash 和版本冲突，不能原子阻止任意编辑器在检查与替换之间写入。发现冲突不要先删除原文件，不要把 revision 改为最新数字后强行导入。

处理冲突时先把人工改动另存到维护窗口内的备份位置，读取 API 当前任务，对照差异并明确保留哪部分；通过管理员 edit 合并允许字段，或在已停止相关写入且确认备份完好后恢复服务认可的快照，再重新应用修改。保持一次明确的合并决定，避免原文件与新账本反复互相覆盖。

## 备份与恢复

最简单可靠的备份：通知参与者停止工作，停止服务，再复制**完整数据目录**到受保护位置。保留 admin/join 凭据、数据库、Markdown 和所需 CLI profile；这些是敏感本地备份，不提交 Git。复制时若 SQLite WAL/SHM 仍存在，不要只拿 `board.sqlite3`；同机活跃备份应使用 SQLite backup API，而不是随意拷贝运行中数据库。

恢复时先备份现有目录并停止所有连接，放回完整副本，用原 workspace 路径启动，检查账本和 Markdown 冲突。时间已过的租约会被回收，旧 agent 应停止并重新核对所有权；恢复备份不代表旧认领仍然安全有效。不要同时启动两个副本让同一组源文件被两套任务锁管理。

不要为了“修复端口”删除数据库、token、lock 或 profile。OS advisory lock 会随持有进程退出释放；锁文件还在不代表仍被占用。先查实际运行进程和端口，避免误杀无关程序。

## 资源验证

推荐使用 `python -S -m agentboard` 与 `python -S agentctl.py`，保持标准库启动，不加载本机 sitecustomize 或第三方 site 包。默认 30 秒心跳足够配合 120 秒租约；不以高频轮询或重写 Markdown 代替协议。测量服务、网页与 agent 客户端时分别记录资源，具体方法和边界见 [CPU 与内存](PERFORMANCE.md)。可用 `python -S scripts/benchmark.py --seconds 20 --tasks 100 --requests 200` 在独立临时环境复现资源测量。资源优化后必须保留并发、租约、revision、Markdown 冲突与重启恢复检查。

## 日志与令牌

默认启动仅输出普通服务地址与凭据文件位置，不打印 secret。`--open` 将 fragment 登录链接交给本地默认浏览器；`--print-login-url` 才会按显式请求打印含管理令牌链接，输出不可分享。网页登录后移除 fragment。

HTTP 内部错误只在 stderr 记录异常类型，不记录异常消息或 traceback，避免本地路径、请求正文或凭据落入日志。排障应在独立测试数据上复现；不要为了定位问题把完整 Authorization、注册响应、claim token 或用户对话写入日志。

## Git 约定

运行目录 `.agentboard/` 与 `.agentboard-*/` 已忽略。自定义 `--data-dir` 在仓库内时，应把该目录加入 `.gitignore`；不要依赖文件扩展名识别全部凭据。提交前检查 `git status --short` 和暂存区，确保没有 SQLite、token、credentials、真实用户任务或本地上下文快照。

源码与 `docs/`、示例模板使用 Git 管理。协作过程中的运行任务、心跳和 token 不进版本历史。多人同时维护源代码时仍需独立分支/工作树与合理资源划分；任务锁不是 Git 合并工具。

## 故障排查

| 现象 | 检查与处理 |
| --- | --- |
| 无法连接 | 服务是否运行、端口/URL 是否一致、是否位于同一环回网络空间 |
| 401/403 | profile 是否属于该服务、agent/join/admin 权限是否用对；不把 token 发到聊天 |
| 409 revision 冲突 | `show TASK_ID` 刷新并核对所有权，理解更新后再决定重试 |
| 租约丢失 | 停止源文件写入，核对任务是否已被他人领取；旧 token 不能继续用 |
| 无法认领但任务 todo | 查依赖是否 done、能力是否匹配、资源是否被占、agent 是否已有任务 |
| agent 显示离线 | 心跳是否持续、运行器是否阻塞、间隔是否小于租约；客户端上下文不会自动更新 |
| Markdown 冲突 | 保留双方内容，进入维护窗口手工比较；不要直接覆盖或改 revision |
| 同一目录已有服务 | 检查旧服务进程；正常停机后重启，不删数据库尝试绕过单实例锁 |
| profile_busy | 同一 profile CLI 调用占用过久；避免独立 agent 共用 profile，检查阻塞请求 |
| 上下文显示未知 | 正常的缺失指标；由客户端报告真实或估计值，不自动当零 |

CLI 正常退出码 0，参数解析错误 2，API 冲突 3，其他配置/网络/API 错误 1，Ctrl+C 为 130。`--json` 错误写 stderr；脚本应先判断退出码，不能只解析 stdout。
## 便携发行

发布前提交源码，运行 `python -S scripts/package_release.py`。脚本从已提交 Git 树取文件，检查允许的源码路径，拒绝令牌、数据库、环境文件及缓存，统一 CMD 的 CRLF 换行，并输出 ZIP、版本清单和 SHA256。产物在被 Git 忽略的 `releases/`；不把 ZIP 二进制提交到源码历史。修改源文件布局时先审查脚本白名单，避免漏打包或夹带运行数据。新发行需在独立解压目录核对清单、启动、HTTP 资源和测试；使用说明见 [PORTABLE.md](../PORTABLE.md)。
