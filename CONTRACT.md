# Agent Board v1 实现契约

任务对象增加 `labels: string[]`（旧任务默认空数组）；创建和管理员编辑
接受该字段，保留既有权限和 revision 检查。字符串不得包含逗号、中文
逗号或控制字符。`GET /api/health` 的 `features` 包含 `task_labels` 才
允许客户端写入标签，避免旧进程静默丢弃字段。标签不改变任务状态或能力。
任务 Markdown 支持导入导出标签，遵循现有版本和状态限制。

本文件是后端、网页、CLI、测试共同使用的接口约定。Python 3.11+，无第三方依赖，默认 `http://127.0.0.1:8765`。项目名 **Agent Board / 本地协作台**。

## 目录与启动

- `python -m agentboard` 启动，参数 `--host`（仅允许环回地址）、`--port 8765`、`--data-dir .agentboard`、`--workspace .`、`--lease-seconds 120`。
- `python agentctl.py ...` 为任意客户端提供接入。
- `agentboard/store.py` 实现 Store；`agentboard/server.py` HTTP；`agentboard/__main__.py` 入口。
- `web/index.html`, `web/app.js`, `web/styles.css` 静态界面。
- `.agentboard/board.sqlite3` 为协调账本，`tasks/*.md` 为可编辑的 Markdown 任务文档，`agents/*.md` 为状态快照，`events.md` 为事件快照。运行目录 Git 忽略。
- `.agentboard/admin.token` 为网页管理令牌，`.agentboard/join.token` 为注册令牌；`agentctl` 默认从本地目录读取 join.token，注册凭据放 `.agentboard/credentials/<profile>.json`。不得打印这些令牌到日志或提交 Git。

## HTTP

JSON UTF-8；成功 200/201，错误 `{error:{code,message,details?}}`，冲突 409，过期所有权 409，认证 401/403。写请求 `Content-Type: application/json`。网页先用 `POST /api/session {token}` 建立 HttpOnly SameSite=Strict cookie；agent 使用 `Authorization: Bearer ...`。所有浏览器 Origin 必须同源；管理令牌可用 Bearer。GET health 不需认证，其他 API 需要认证。join 令牌仅允许读取、注册。

- `GET /api/health` → `{ok:true,protocol_version:"1"}`。
- `POST /api/session {token}` → `{ok:true}`，仅 admin 令牌。
- `DELETE /api/session` 登出。
- `GET /api/state` 支持 `ETag` / `If-None-Match`；未变化返回304空正文（仍需认证），server_time为快照生成时间而非每次检查时间。成功200 → `{protocol_version:"1",server_time,lease_seconds,tasks,agents,events,stats,markdown:{pending,conflicts}}`。
- `GET /api/tasks/<id>` → `{task}`。
- `POST /api/agents/register {name,client,model,capabilities?:string[],context?}` → `{agent,token}`，join/admin/session 可调用。
- `POST /api/agents/<id>/heartbeat {summary?,status?:"idle"|"working"|"blocked",context?,task_id?,claim_token?}` → `{agent,task?}`，仅自己的 agent token。同时提供 task_id + claim_token 才续租，续租不改变任务 revision。
- `POST /api/tasks {title,description?,priority?:0..3（0最紧急，默认1）,resources?:string[],depends_on?:string[],required_capabilities?:string[],labels?:string[]}` → `{task}`，admin/session/agent 可调用。
- `POST /api/tasks/<id>/claim {expected_revision,request_id?}` → `{task,claim_token}`，仅 agent。单 agent 同时一个任务，依赖均 done、能力匹配、资源不冲突、状态 todo 才可认领。
- `POST /api/tasks/<id>/progress {expected_revision,claim_token,summary,progress?:0..100,context?}` → `{task}`，仅 owner；续租。
- `POST /api/tasks/<id>/release {expected_revision,claim_token,summary?}` → `{task}`，仅 owner，回 todo。
- `POST /api/tasks/<id>/block {expected_revision,claim_token,reason}` → `{task}`，仅 owner，入 blocked 并释放锁。
- `POST /api/tasks/<id>/submit {expected_revision,claim_token,result}` → `{task}`，仅 owner，入 review 并释放锁。
- `POST /api/tasks/<id>/edit {expected_revision,title?,description?,priority?,resources?,depends_on?,required_capabilities?}` → `{task}`，admin/session；仅 todo/blocked 可编辑。
- `POST /api/tasks/<id>/reopen {expected_revision,reason?}` → `{task}`，admin/session；仅 blocked/review/done/cancelled，回 todo。
- `POST /api/tasks/<id>/approve {expected_revision}` → `{task}`，admin/session；仅 review → done。
- `POST /api/tasks/<id>/cancel {expected_revision,reason?}` → `{task}`，admin/session；非 done/cancelled，清除 owner 并释放锁。
- `POST /api/markdown/sync {}` → `{imported,exported,conflicts}`，admin/session；导入离线编辑，revision 冲突拒绝覆盖。也支持新文件。

## 对象

task: `{id,title,description,status,priority,revision,owner_id,lease_expires_at,claim_generation,resources,depends_on,required_capabilities,labels,created_at,updated_at,result,blocker,progress,summary}`。时间使用 UTC ISO 8601 字符串，空值 null。id 为安全短字符串。claim_token 永不出现在 state 或 Markdown。

agent: `{id,name,client,model,capabilities,status,context,summary,last_seen_at,current_task_id}`。在线状态根据心跳时间显示为 offline，原存储状态可为 idle/working/blocked。

context: `{used_tokens:null|非负整数,max_tokens:null|正整数,source:"unknown"|"reported"|"estimated",note?:string,updated_at:UTC接收时间}`。禁止伪造精确消耗；未知就是 null，不能显示成零。context 不包含完整提示词或对话。

event: `{id,at,type,task_id,agent_id,message}`，网页取最新 100 条，倒序。

## 并发规则

SQLite WAL + BEGIN IMMEDIATE + busy_timeout；每次写入在一个事务中检查 revision、租约、owner、claim_token、依赖、能力、资源、单 agent 限制。claim_generation 单调递增；过期回 todo，旧 token 永远失效。服务启动及所有读写请求触发过期回收。资源为 workspace 相对路径，禁止绝对路径、..、通配符；目录可带尾斜杠，父子路径冲突，Windows 大小写折叠。

所有任务变更同时写事件与待同步标记；Markdown 用临时文件 + os.replace 原子替换。导出由单进程内锁及 SQLite 单实例启动锁保护；崩溃后从账本恢复待同步项。离线手工编辑与服务写入碰撞时保留原文件，报告冲突；不能把任务 Markdown 当锁文件，也不能通过改状态绕过 API。

任务 Markdown 格式：`---` 换行 JSON 对象 换行 `---` 换行正文。JSON 是合法 YAML 子集，含 `id,title,revision,status,priority,resources,depends_on,required_capabilities,labels` 及只读状态字段；正文即 description。导入仅允许 todo/blocked 的可编辑字段，不导入状态/owner/revision；已有文件必须匹配账本 revision。新文件不带 id 或 id 为文件名（仅安全字符），revision 0，status todo。新建推荐 CLI。导出不可覆盖被外部改过的文件；state 报告冲突，sync 成功导入后再导出。

## UI

中文、响应式；侧边导航任务看板/Agent 状态/协作日志/接入指南；顶部连接状态、刷新及同步 Markdown；任务创建弹窗、搜索/状态筛选、任务详情及审核/重开/取消管理操作；agent 显示客户端、模型、在线状态、摘要、任务、context 占用及来源；未知显示未知。自适应轮询：工作中10秒、空闲30秒、失败退避至60秒；后台标签页停轮询。GET state 支持 ETag/If-None-Match 与304，未变化不重建业务DOM。渲染用户文本必须 textContent，禁止不可信 innerHTML。登录令牌来自 URL fragment `#token=...`（登录后移除）或输入。可查看任务描述纯文本，避免引入 Markdown 库。

## 资源优化约定

服务保持一份UTF-8状态快照，写入立即失效；快照最长15秒，提前在租约或在线状态到期时失效。Markdown按待同步项写入，未变化文件复用stat/hash；全量stat检查最多每15秒按请求触发，显式sync强制读取。主循环空闲每秒唤醒，HTTP最多8个处理线程，无后台文件轮询线程。启动脚本使用Python -S跳过本机第三方site初始化。性能实测与复现方法见docs/PERFORMANCE.md。
