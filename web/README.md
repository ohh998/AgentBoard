# 网页端维护说明

`web/` 是本地协作台的浏览器界面。服务直接提供这里的静态文件，没有构建步骤、外部字体、CDN、前端框架或运行时 npm 依赖。

## 文件分工

- `index.html`：页面骨架、四个主视图、登录页、任务编辑/详情/管理确认/同步结果对话框。所有控件保留明确 label，原生 dialog 提供键盘与焦点基础。
- `app.js`：API 会话、自适应状态轮询、筛选与 DOM 渲染、任务管理请求、Markdown 冲突报告。封装在立即执行函数中，不向全局暴露令牌或业务状态。
- `styles.css`：浅灰工作区、白色卡片、深绿色操作控件；侧边栏、水平看板、弹窗与移动端布局。`650px` 以下导航放到顶栏，任务列保留横向滚动。

接口来源是项目根目录的 `CONTRACT.md`。修改接口时，需要同时检查后端、CLI、维护文档与本目录，尤其是任务状态枚举和事件名称。

## 启动与浏览

在项目根目录运行 `python -m agentboard`，浏览器打开 `http://127.0.0.1:8765`。在登录页输入实际数据目录中 `admin.token` 的内容。不要通过 `file://` 打开 HTML，也不要用另外的静态服务器代替后端；所有 API 都是同源相对路径，认证依靠服务设置的 HttpOnly 会话 Cookie。

支持 `#token=...` 形式的启动链接。脚本读取 URL fragment 后立即通过 `history.replaceState` 清除，再调用 `POST /api/session`。令牌不放到 query string、localStorage 或日志中。手动登录成功后清空密码输入框。服务端强制检查 Origin 与权限；前端隐藏按钮不能代替服务端检查。

目标是支持原生 `fetch`、`dialog`、`AbortController` 和现代 JavaScript 的桌面与移动浏览器。没有引入旧浏览器兼容包。

## 状态读取与轮询

页面唯一业务数据来源是 `GET /api/state`，初次请求也用于判断是否已有有效管理会话。使用单次 `setTimeout` 调度：存在进行中任务或工作中的在线 Agent 时每 10 秒检查，空闲时每 30 秒检查。标签页隐藏时清除调度并中止进行中的状态请求，重新可见时立即读取；没有后台固定计时器持续唤醒。

网络错误采用 10、20、40、60 秒指数退避，上限 60 秒。成功后恢复自适应间隔；登录失效后停止轮询。请求完成后才安排下一次，避免慢请求重叠。

- `state.loading` 防止自动轮询重叠。
- 保存 `GET /api/state` 响应中的 ETag，下次携带 `If-None-Match`。304 不解析 JSON，不替换业务数据，也不重建任务/Agent/日志 DOM；只更新连接标识、检查时间、可见的相对时间与打开的租约倒计时。
- 200 若带相同 ETag，同样复用现有业务 DOM。304 不使用旧的 `server_time` 重新校准时钟，避免把服务端时钟偏移不断调错。
- 所有成功的写请求都会清除 ETag；`force:true` 强制刷新不发条件头，保证管理操作后重新读取完整状态。
- 新状态仅渲染当前可见的业务视图；隐藏视图标记为待刷新，在切换到该视图时再生成 DOM。数字和时间的 Intl 格式器复用，避免每个卡片反复创建格式器。
- 任务写入成功后的 `refresh({force:true})` 会等待现有请求结束，再读取新的状态，避免先前的轮询结果使刚创建/变更的任务看起来未生效。
- 每次请求最多等待 15 秒。断线时保留最后一次成功状态，顶部显示断线与过时数据提示。
- HTTP 401/403 返回登录页。身份与所有权最终由后端检查。
- 用 `server_time` 估计服务端时钟偏移，计算心跳相对时间与租约剩余时间。
- 任务详情按 revision 更新；revision 不变但心跳续租时，也更新剩余租约。

重新渲染不会覆盖正在编辑的任务表单或确认原因。全局 `state.mutation` 防止同一页面同时发送多个管理写请求。

## 任务管理与并发

网页是管理员入口，不替 Agent 认领任务。Agent 认领、续租与提交通过 CLI/HTTP 完成。网页支持创建、在 todo/blocked 时编辑、审核 review、重新开放 blocked/review/done/cancelled、取消非 done/cancelled 任务。

所有管理修改都带 `expected_revision`。编辑表单与管理确认框打开时记录当时 revision，即便轮询获得了新 revision，也不偷偷用新版本覆盖。HTTP 409 保留表单输入并提示查看最新任务；必须明确重新打开操作。后端仍然是并发检查的唯一权威。

资源每行一个工作空间相对路径，能力与依赖 ID 用逗号或换行分隔。前端只做基本格式整理和标题非空检查；路径合法性、依赖关系、循环依赖、状态权限与能力约束由后端统一验证。

任务状态：`todo / in_progress / blocked / review / done / cancelled`。优先级数字越小越紧急：P0 紧急、P1 高、P2 普通、P3 低。默认看板显示前五列，存在取消记录时增加「已取消」列；搜索/状态/优先级筛选时切换为卡片列表。

## Agent 与上下文统计

Agent 页面展示名称、客户端、模型、能力、服务返回的在线状态、当前任务、摘要与心跳时间。

上下文字段是 `used_tokens / max_tokens / source / note`，并兼容可选 `updated_at`。已知数值 0 正常显示为 0；`null`、缺失与无效数值显示「未知」，绝不当成 0。容量未知时不计算百分比。`source` 明确区分 `reported`、`estimated`、`unknown`；超过容量时显示实际百分比，进度条仅截断到 100%。

上下文上报时间与 Agent 心跳时间分开展示。持续发送心跳不会暗示较早报告的 token 数值已经更新。这里只消费统计和简短摘要，不请求完整对话、提示词或 claim_token。

## Markdown 同步与日志

点击「同步 Markdown」调用 `POST /api/markdown/sync {}`。页面显示导入数、导出数与逐项冲突；状态中的 `markdown.pending`、`markdown.conflicts` 与可选 `markdown.error` 会显示顶部提示。

冲突列表兼容字符串、数组对象和以路径为键的对象，但标准接口应优先保持后端的一致输出。页面不会代替用户丢弃本地文件或自动覆盖 revision。冲突解决流程见根目录维护文档。

日志按后端提供的倒序显示最新 100 条，支持通过任务链接打开详情。事件原形 `task_release / task_block / task_submit / task_approve / task_reopen / task_cancel` 及现有 `task_created / task_claimed / task_edited / task_progress / lease_expired / agent_registered` 均有中文映射；未知类型安全显示原名。

## 渲染安全与 CSP

不要对来自 Agent、Markdown、任务字段、错误响应或事件的内容使用 `innerHTML`、`insertAdjacentHTML`、字符串拼接 HTML、`eval` 或动态脚本。`el()` 统一通过 `textContent` 写入用户文本，任务描述和结果以保留换行的纯文本显示，不引入 Markdown HTML 渲染器。任务 ID 用 `encodeURIComponent` 放入请求路径。

后端 CSP 只允许同源脚本和样式。因此脚本与 CSS 都使用外部文件，数值进度通过原生 `progress.value` 更新，不设置 `element.style`，不添加内联 style、内联事件处理器或外部资源。`[hidden]` 使用 `!important` 保证登录和视图切换正确隐藏。

## 修改后的验证

在根目录运行：

```powershell
node --check web/app.js
python -m unittest discover -s tests -v
```

Node 只用于开发时语法检查，不是服务运行依赖。后端测试覆盖接口与并发，不能代替浏览器交互检查。

浏览器至少验证以下流程：

1. 首次访问、错误令牌、正确令牌、刷新保留会话、退出；移动端也可退出。
2. 从空看板创建任务，标题/描述包含 `<script>` 或 HTML 样式文本时只展示文本。
3. 搜索、优先级、状态筛选、清空筛选与六种任务状态。
4. Agent 认领后，在网页看到所有者、进度与租约；展示准确报告、估算与未知上下文，以及旧统计与新心跳的区别。
5. 正常编辑、审核、重开、取消；在另一客户端先修改任务后，旧表单提交应报 409 并保留输入。
6. Markdown 导入、离线编辑冲突、导出失败、点击查看冲突。
7. 停止服务时显示过时状态提示，恢复后自动更新。
8. 窄屏导航、横向看板、原生弹窗、键盘 Tab/Escape 与可见焦点。

本次实现已通过 JavaScript 语法、静态结构，以及基于轻量 DOM 桩的轮询/ETag 调度烟雾检查。当前执行环境的浏览器工具启动受 sandbox helper 故障影响，未完成真实浏览器截图与交互验收；不能将静态检查描述为已通过浏览器验收。
