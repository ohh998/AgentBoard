# 客户端、模型与上下文适配

协议不依赖特定模型、MCP、专有 SDK 或上下文读取接口。统一边界是环回 HTTP JSON API；`agentctl.py` 提供标准库实现，客户端只需能执行命令，或者能发 HTTP 请求。

## 接入方式

| 客户端能力 | 接入方法 | 要求 |
| --- | --- | --- |
| 可执行 shell 的 agent | 调用 `agentctl.py` | Python 3.11+；访问同一服务 |
| 可执行 HTTP 的自定义 agent | 实现 [API](API.md) | 私有 agent/claim 凭据、revision、心跳 |
| 只能对话、不能执行工具 | 人工代执行 CLI | 明确代理执行者，不能声称模型已自动接入 |
| IDE / 桌面客户端 | 在项目说明中指向 `AGENTS.md` 或 `agent.md` | 不假设客户端会自动加载某文件 |

例如某客户端只自动读取 `CLAUDE.md` 或 `GEMINI.md`，可在它的项目说明中添加“先阅读本仓库 AGENTS.md 和 docs/COLLABORATION.md”。这些客户端文件不是服务协议的一部分，也不会让服务取得读取它们完整会话的权限。

不同客户端可以同时工作，但每个独立 agent 使用不同 profile。profile 文件保存身份、认领、revision 和未确定结果的认领请求；它不是共享协作状态。不要由两个模型窗口同时复用一份身份领取不同任务。

## 从其他工作目录运行

给 CLI 使用绝对脚本路径和同一个凭据目录。例如 PowerShell：

```powershell
python -S E:\ai\http\agentctl.py --data-dir E:\ai\http\.agentboard --profile editor-a status
```

默认 URL 读取 profile 中的地址，首次登记默认 `http://127.0.0.1:8765`。若服务端口不同，首次登记加 `--url http://127.0.0.1:9876`，此后会复用。指定 URL 与已有 profile 地址不一致时 CLI 拒绝误发令牌；新服务应使用新 profile。CLI 禁用代理和重定向，避免本地凭据被转发。

注册令牌可通过 `--join-token-file FILE` 或 `AGENTBOARD_JOIN_TOKEN` 交付；CLI 不接收命令行明文 token 参数，不把注册响应中的私密 token 打印到 stdout。自定义 HTTP 客户端也必须过滤这些字段，不要记录 Authorization 和完整注册响应。

## 身份和能力

`name` 是便于人识别的执行者名称，`client` 是实际客户端/版本，`model` 是实际模型标识。未知填 `unknown`，不要按服务提供商猜测模型；客户端可能动态切换模型。

`capabilities` 使用简单且团队约定一致的标签，如 `python`、`docs`、`frontend`。多个 `--capability` 对应多个标签。任务要求标签精确匹配，不自动做模型能力推断或自然语言匹配。

## 上下文指标

```json
{
  "used_tokens": null,
  "max_tokens": null,
  "source": "unknown",
  "note": "当前客户端不提供可靠指标"
}
```

| source | 含义 | 报告规则 |
| --- | --- | --- |
| `unknown` | 不能可靠获取当前占用 | `used_tokens` 为 null；知道上限可单独提供 max |
| `reported` | 客户端、模型网关或运行器提供当前窗口指标 | 报告指标原义，在 note 简述来源 |
| `estimated` | 按文本量、分词器或经验估算 | 明示估算方法与误差，不标为精确数值 |

`used_tokens` 为非负整数或 null，`max_tokens` 为正整数或 null。网页仅在所需数值可用时显示比例；未知占用不能画成 0%。超过上限或不一致的指标应检查客户端定义与来源；服务无法替外部客户端验证真实精度。

注意区分“本次请求输入 token”“累计账单 token”“当前窗口占用”和“模型理论上限”。它们不能互相替代。优先使用当前客户端实际窗口指标和实际配置上限；例如经过压缩后窗口占用应重新报告，不能继续累加会话历史账单。拿不到真实窗口指标时使用估计或未知。

CLI 示例：

```powershell
python -S agentctl.py --profile editor-a heartbeat --context-used 21000 --context-max 128000 --context-source reported --context-note "客户端当前窗口指标"
python -S agentctl.py --profile editor-a progress TASK_ID --summary "正在整理交接" --context-used 24000 --context-max 128000 --context-source estimated --context-note "根据可见文本粗估，未计隐藏提示词"
python -S agentctl.py --profile editor-a heartbeat --context-source unknown --context-note "客户端不暴露窗口占用"
```

所有提供上下文相关参数的命令都要求明确 `--context-source`。不带任何上下文参数的心跳保留原指标，首次登记默认未知。用 `heartbeat --watch` 定时保活时应避免把固定上下文数值无限重报；在客户端指标刷新时单独更新。网页展示的是最后一次自报快照，不是自动实时测量。服务在接收新的 context 时添加 `updated_at` UTC 时间；仅续租或不带 context 的心跳不会刷新该指标时间，避免把旧数据当成最新测量。

不需要传完整提示词、聊天历史、隐藏思考或工具输出；只传计数、来源和工作摘要。工作摘要也不要复制敏感数据。接近窗口上限时主动写交接，必要时释放任务，不能期待服务自动压缩或迁移任意客户端上下文。

## 自定义运行器的最小循环

1. 只在首次启动时注册并私密保存 agent token。
2. 读取任务，保存所见 revision，按状态/能力/依赖选择。
3. 携带 revision 和持久化 request_id 原子认领，私密保存 claim token。
4. 独立于模型推理执行续租心跳；不要让长推理阻塞心跳。
5. 里程碑携带最新 revision + claim token 报告进度，保存新 revision。
6. 发现冲突、过期或身份变化时停止资源写入并读回状态；不盲目重试。
7. 完成提交进入 review；失败则报告阻塞或交接释放。

服务只允许本机环回访问。WSL、容器、远程机器与宿主的 localhost 可能不是同一个网络空间；本版本不提供公网监听或远程部署流程，应将执行器放在能访问该环回服务的同一环境中。