# CPU 与内存使用

本应用采用 Python 标准库、SQLite 和静态网页，无第三方运行依赖、前端构建器或常驻文件监听器。资源目标是低频协调，模型推理与任务执行由各 agent 客户端承担，不在网页服务进程中运行。

## 推荐启动方式

```powershell
python -S -m agentboard --open
python -S agentctl.py --profile worker-a heartbeat --watch --interval 30
```

`-S` 跳过 Python 的 site 初始化，避免本机 `sitecustomize`、用户 site 和第三方 site 包随进程加载；本应用只用标准库，无需这些启动钩子。Linux / macOS 可使用 `python3 -S`。`-S` 不会限制 agent 模型或浏览器自身的资源，也不应靠关闭租约、版本检查或 SQLite 持久化来节省资源。

默认租约 120 秒，心跳 30 秒即可；进度在里程碑上报，上下文在客户端指标更新时上报。不要让每个 agent 每秒扫描整个看板或高频重写 Markdown。只需保活时不要携带完整摘要或上下文，以免增加写入并覆盖较新的自报状态。

## 已实现的资源策略

- 状态快照缓存：复用一份 UTF-8 看板响应，状态变更立即失效；缓存最迟 15 秒重查，并提前在租约到期或 agent 离线边界失效，不能拿缓存延长旧所有权。
- HTTP 条件查询：`GET /api/state` 返回 ETag，客户端带 `If-None-Match`，无变化返回 304 空正文；时钟变化本身不产生新的 ETag。减少传输、JSON 解析与页面分配。
- 自适应页面查询：有工作任务/agent 时每 10 秒，空闲每 30 秒；失败退避 10、20、40、60 秒。页面隐藏时停止定时器并中止正在进行的状态查询，恢复可见后立即刷新。
- 页面局部工作：304 复用状态，不重建业务 DOM；仅渲染当前可见视图。成功写入后清除条件缓存并立即读取最新状态，手动刷新也能主动获取状态。
- Markdown 增量处理：常规写入只处理待同步文档；全量检查按请求懒触发，间隔约 15 秒。缓存文件 stat 签名与 hash，未观察到变化的文件不反复读内容或写盘；管理员 sync 主动检查离线编辑。
- 并发上限：最多 8 个 HTTP 请求处理线程，超出请求等待调度，避免每次连接无限创建线程；高并发时仍可能排队或超时。SQLite 写事务保持串行约束。
- 无空闲扫描线程：租约与 Markdown 检查由请求/到期边界驱动；没有 agent 时不需要通过后台高频扫描维持服务。

这些优化减少重复工作，保留原有 revision、租约、claim token、资源锁与持久化检查。直接用其他 HTTP 客户端高频轮询，仍会产生认证、连接与处理成本；CLI 心跳使用持久常驻的 watch 进程可以减少反复启动 Python。

## 一次本机实测

环境为 Windows（win32）、Python 3.11.5，使用 `python -S`，完全独立的临时 workspace 和数据目录。账本含 100 个任务（每个正文 8 行、248 字节）和 8 条 idle agent 记录；未运行浏览器或 8 个模型客户端。预热一次状态读取后，先观察 20 秒空闲，再串行发送 200 个带 ETag 的状态请求。

| 指标 | 结果 |
| --- | --- |
| 空闲观察时间 | 20.0 秒 |
| 空闲服务 CPU 时间增量 | 0.0 秒（采样精度内未计到） |
| 空闲 Working Set | 25.26 MiB |
| 空闲 Private Bytes | 15.11 MiB |
| 条件请求 | 200 次，全部 304，响应正文均为 0 字节 |
| 200 次请求墙钟耗时 | 0.935 秒 |
| 200 次请求服务 CPU 时间增量 | 0.109375 秒 |
| 请求后 Working Set | 25.10 MiB |

仅统计服务进程，排除浏览器、模型客户端与基准脚本本身。0.0 秒表示这次采样精度内没有可见增量，不承诺绝对零 CPU；短正文、无心跳的空闲记录也不等于大量活跃 agent 的实际负载。200 次串行请求不是并发写入吞吐量测试，304 仍有 HTTP 响应头和连接成本。

可复现脚本为 [`scripts/benchmark.py`](../scripts/benchmark.py)：

```powershell
python -S scripts/benchmark.py --seconds 20 --tasks 100 --requests 200
```

脚本自动创建并清理临时工作区/数据，自启自停独立服务，不修改实际账本；支持 Windows 和 Linux。可调范围为 seconds 1..60、tasks 0..1000、requests 1..10000。输出 JSON 记录平台、Python 版本、数据量、CPU/内存与 304 次数；不同机器数值以实际运行结果为准。

## 测量方法

服务进程、浏览器和 agent 客户端分别测量，不能把浏览器渲染或模型推理的内存算作 Python 服务常驻内存。使用独立临时数据目录，记录操作系统、Python 版本、启动参数、任务/agent 数量、是否有浏览器轮询、测试时长和并发数。

至少测两组：

1. 空账本、无请求的空闲服务：启动完成并预热后，连续观察 20~60 秒，记录 CPU 增量和内存；长期运行需用外部采样持续观察。
2. 实际规模账本：登记若干 agent、创建任务，再模拟正常心跳、网页查询及并发写入；同时检查认领互斥和租约正确性，不只看吞吐量。

Windows PowerShell 示例，把 `SERVICE_PID` 换成服务进程 ID：

```powershell
$serviceProcess = Get-Process -Id SERVICE_PID
$cpuStart = $serviceProcess.CPU
$sampleWatch = [Diagnostics.Stopwatch]::StartNew()
Start-Sleep -Seconds 30
$serviceProcess.Refresh()
$sampleWatch.Stop()
[pscustomobject]@{
    CpuSeconds = $serviceProcess.CPU - $cpuStart
    SingleCorePercent = 100 * ($serviceProcess.CPU - $cpuStart) / $sampleWatch.Elapsed.TotalSeconds
    WorkingSetMiB = $serviceProcess.WorkingSet64 / 1MB
    PrivateMiB = $serviceProcess.PrivateMemorySize64 / 1MB
}
```

`SingleCorePercent` 以一个逻辑核为 100%，不是任务管理器可能展示的整机归一化百分比。Working Set 是驻留物理内存，Private Bytes 是私有提交量，不能混为一项。Linux 可用 `ps -p PID -o pid,%cpu,rss,vsz` 并取进程 CPU 时间增量；RSS 与 VSZ 也应分开报告。

## 使用边界

本工具面向本机少量 agent 的协作，任务列表包含全部任务，内存与响应大小会随任务数量和正文长度增长。浏览器、Python 版本、本机启动钩子、操作系统缓存和磁盘都会影响结果；一次短测量不能保证所有机器或长期运行都维持相同占用。

降低轮询频率会增加页面观察到变化的延迟；认领、续租和版本检查仍以服务事务为准。资源优化不能改变任务所有权语义，也不能把任务资源锁变成操作系统文件强制锁。备份与归档应按维护手册操作，不直接删改运行中的 SQLite 或 Markdown 来降低占用。