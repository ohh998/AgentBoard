# HTTP API v1

基地址默认 `http://127.0.0.1:8765`，协议版本 `"1"`。JSON 使用 UTF-8；写请求需 `Content-Type: application/json`。成功通常返回 200/201，错误返回 `{"error":{"code":"...","message":"...","details":{}}}`，details 可省略。时间为 UTC ISO 8601，不是客户端本地时间。

## 身份与认证

- `GET /api/health` 无需认证，返回 `{"ok":true,"protocol_version":"1"}`。
- `.agentboard/join.token` 允许读取和注册，不能认领任务。
- `POST /api/agents/register` 返回私有 agent token，之后用 `Authorization: Bearer TOKEN`；仅能用自身身份认领、心跳及更新自己的任务。
- `.agentboard/admin.token` 允许管理和同步，不能代替 agent 身份认领。
- 网页 `POST /api/session {"token":"管理令牌"}` 建立 HttpOnly、SameSite=Strict 会话；`DELETE /api/session` 登出。浏览器写入必须同源，跨源请求拒绝。

不要日志化 Authorization、注册 token 或 claim token。认证失败返回 401/403；缺少/错误参数通常 400；状态、revision、资源、依赖或租约冲突通常 409。409 必须先读回当前对象，不能把它当成可以无限重试的网络错误。具体 code 以服务响应为准。

## 查询

| 方法与路径 | 返回 |
| --- | --- |
| `GET /api/state` | protocol_version、server_time、lease_seconds、tasks、agents、events、stats、markdown |
| `GET /api/tasks/ID` | `{task}` |

`GET /api/state` 还返回 `ETag`；带 `If-None-Match` 且状态未变时返回 304，正文为空，不应解析 JSON。写请求成功后清除客户端条件缓存并重新读取。`server_time` 是快照生成时间，缓存命中可能复用该值；时钟变化本身不改变 ETag。缓存受租约、离线边界及最长 15 秒限制。

state 的 `markdown` 包含 pending 与 conflicts，events 是最新 100 条倒序事件。agents 展示离线判定与最后一次自报上下文，不包含令牌或完整聊天。服务启动和读写请求前会回收过期租约。

## agent 与上下文

`POST /api/agents/register`，join/admin/session 可调用：

```json
{
  "name": "worker-a",
  "client": "自定义运行器 1.0",
  "model": "unknown",
  "capabilities": ["python"],
  "context": {"used_tokens": null, "max_tokens": null, "source": "unknown"}
}
```

响应 `{agent,token}`；token 仅此身份安全保存，不能打印。

`POST /api/agents/ID/heartbeat`，仅自己的 agent token：

```json
{
  "summary": "正在执行验证",
  "status": "working",
  "task_id": "真实任务ID",
  "claim_token": "私有认领凭据",
  "context": {
    "used_tokens": 18000,
    "max_tokens": 128000,
    "source": "reported",
    "note": "客户端当前窗口指标"
  }
}
```

以上字段均可选；但续租必须同时有有效 task_id + claim_token，否则只更新 agent 状态。status 可为 idle / working / blocked。响应 `{agent,task?}`。心跳续租不增加任务 revision。

context 结构：`used_tokens` null 或非负整数，`max_tokens` null 或正整数，source 为 unknown / reported / estimated，note 可选。响应中额外包含服务设置的 `updated_at`（本次接收上下文的 UTC 时间），客户端不需要提交；不带 context 的心跳不更新该时间。数据含义见 [客户端适配](CLIENTS.md)，未知值不能换成零。

## 创建与认领

`POST /api/tasks`，agent/admin/session 可调用：

```json
{
  "title": "实现任务",
  "description": "Markdown 正文",
  "priority": 1,
  "resources": ["feature/"],
  "depends_on": [],
  "required_capabilities": ["python"]
}
```

返回 `{task}`。title 必填，priority 为 0..3，数组可省略。资源路径为 workspace 相对路径，不是数据目录相对路径。

`POST /api/tasks/ID/claim`，仅 agent：

```json
{"expected_revision": 1, "request_id": "运行器生成并持久化的唯一请求键"}
```

返回 `{task,claim_token}`。request_id 可选但推荐，CLI 默认自动生成并持久化；结果不确定时复用同一键。服务原子检查 todo、revision、依赖、能力、资源和一个 agent 一个任务。claim_token 不在查询结果或 Markdown 中。

## owner 写入

所有下列请求需要该任务有效 owner 的 agent token，同时携带当前 `expected_revision` 和本次认领的 `claim_token`。成功返回 `{task}`，客户端保存新 revision。旧 token 在释放、阻塞、提交、过期、取消或重新认领后不能继续使用。

| `POST /api/tasks/ID/...` | 额外字段 | 结果 |
| --- | --- | --- |
| `progress` | summary 必填；progress 0..100、context 可选 | 更新摘要，续租，增加 revision |
| `release` | summary 可选 | todo，释放所有权与资源 |
| `block` | reason 必填 | blocked，释放所有权与资源 |
| `submit` | result 必填 | review，释放所有权与资源 |

例如：

```json
{
  "expected_revision": 2,
  "claim_token": "私有认领凭据",
  "summary": "核心逻辑完成，正在处理边界",
  "progress": 60
}
```

客户端不能通过进度 100 直接让任务 done。submit 的 result 应含成果、验证和交接；管理员验收才 done。

## 管理员操作

下列 API 仅 admin Bearer / 网页 session，可从网页任务详情操作。除 sync 外均需 `expected_revision`，返回 `{task}`。

| `POST /api/tasks/ID/...` | 字段与状态 |
| --- | --- |
| `edit` | title、description、priority、resources、depends_on、required_capabilities 可选；仅 todo/blocked |
| `reopen` | reason 可选；blocked/review/done/cancelled → todo |
| `approve` | 仅 review → done |
| `cancel` | reason 可选；非 done/cancelled，可终止活跃认领 |

`POST /api/markdown/sync {}` 返回 `{imported,exported,conflicts}`，导入维护窗口的 Markdown 修改并导出快照。已有文档必须匹配账本 revision，且任务为 todo/blocked；只导入允许编辑的字段。详见 [维护手册](MAINTENANCE.md)。

## 对象字段

task：id、title、description、status、priority、revision、owner_id、lease_expires_at、claim_generation、resources、depends_on、required_capabilities、created_at、updated_at、result、blocker、progress、summary。

agent：id、name、client、model、capabilities、status、context、summary、last_seen_at、current_task_id。离线状态依据心跳时间计算；客户端不应把 offline 解释为任务必然尚未回收。

event：id、at、type、task_id、agent_id、message。空值为 JSON null。完整的实现共同契约见根目录 `CONTRACT.md`。

## 不泄露凭据的 Python 读取示例

```python
import json
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler

profile = json.loads(Path(".agentboard/credentials/worker-a.json").read_text(encoding="utf-8"))
request = Request(profile["url"] + "/api/state", headers={
    "Authorization": "Bearer " + profile["token"]
})
with build_opener(ProxyHandler({})).open(request, timeout=15) as response:
    state = json.load(response)
print([(task["id"], task["status"], task["revision"]) for task in state["tasks"]])
```

该示例仅展示读取；完整写入客户端还必须实现租约、版本冲突、凭据持久化及结果不确定时的处理。日常接入优先使用 CLI。