"use strict";

// 来自文件、API 与 Agent 的内容全部以纯文本渲染，禁止使用 innerHTML。
(() => {
  const $ = (id) => document.getElementById(id);
  const statusLabels = {todo: "待认领", in_progress: "进行中", blocked: "受阻", review: "待审核", done: "已完成", cancelled: "已取消"};
  const agentStatusLabels = {idle: "空闲", working: "工作中", blocked: "受阻", offline: "离线"};
  const priorityLabels = {0: "P0 · 紧急", 1: "P1 · 高", 2: "P2 · 普通", 3: "P3 · 低"};
  const viewLabels = {tasks: "任务看板", agents: "Agent 状态", events: "协作日志", guide: "接入指南"};
  const sourceLabels = {unknown: "来源未知", reported: "Agent 报告", estimated: "估算值"};
  const eventLabels = {
    "task.created": "创建任务", "task.claimed": "认领任务", "task.progress": "进度更新",
    "task.released": "释放任务", "task.blocked": "任务受阻", "task.submitted": "提交审核",
    "task.approved": "审核通过", "task.reopened": "重新开放", "task.cancelled": "取消任务",
    "task.edited": "编辑任务", "task.expired": "租约过期", "agent.registered": "Agent 注册",
    "agent.heartbeat": "Agent 心跳", "markdown.imported": "文档导入", "markdown.conflict": "同步冲突",
    task_created: "创建任务", task_claimed: "认领任务", task_progress: "进度更新",
    task_released: "释放任务", task_blocked: "任务受阻", task_submitted: "提交审核",
    task_approved: "审核通过", task_reopened: "重新开放", task_cancelled: "取消任务",
    task_release: "释放任务", task_block: "任务受阻", task_submit: "提交审核",
    task_approve: "审核通过", task_reopen: "重新开放", task_cancel: "取消任务",
    task_edited: "编辑任务", lease_expired: "租约过期", agent_registered: "Agent 注册",
    agent_heartbeat: "Agent 心跳", markdown_imported: "文档导入", markdown_conflict: "同步冲突"
  };
  const state = {
    data: null, view: "tasks", authenticated: false, loading: false, mutation: false,
    selectedTaskId: null, detailRevision: null, editing: null, action: null,
    lastSuccess: null, serverOffset: 0, timer: null, failures: 0, etag: null,
    stateController: null, dirtyViews: new Set(), refreshWaiters: [], focusBeforeDialog: new WeakMap()
  };
  function el(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null) node.textContent = String(content);
    return node;
  }
  function append(parent, ...children) { children.filter(Boolean).forEach((child) => parent.append(child)); return parent; }
  function textOr(value, fallback = "未提供") { return value === null || value === undefined || value === "" ? fallback : String(value); }
  function list(value) { return Array.isArray(value) ? value : []; }
  function tasks() { return list(state.data?.tasks); }
  function agents() { return list(state.data?.agents); }
  function taskById(id) { return tasks().find((task) => task.id === id); }
  function agentById(id) { return agents().find((agent) => agent.id === id); }
  const numberFormatter = new Intl.NumberFormat("zh-CN");
  const fullTimeFormatter = new Intl.DateTimeFormat("zh-CN", {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false});
  const shortTimeFormatter = new Intl.DateTimeFormat("zh-CN", {hour: "2-digit", minute: "2-digit", hour12: false});
  function formatNumber(value) { return numberFormatter.format(value); }
  function validNumber(value) { return typeof value === "number" && Number.isFinite(value) && value >= 0; }
  function percent(value) { return validNumber(value) ? Math.max(0, Math.min(100, value)) : 0; }
  function progressBar(className, value) {
    const bar = el("progress", className);
    bar.max = 100;
    bar.value = percent(value);
    return bar;
  }
  function dateValue(value) { const date = new Date(value); return value && !Number.isNaN(date.getTime()) ? date : null; }
  function fullTime(value) {
    const date = dateValue(value);
    return date ? fullTimeFormatter.format(date) : "未知";
  }
  function shortTime(value) {
    const date = dateValue(value);
    return date ? shortTimeFormatter.format(date) : "—";
  }
  function relativeTime(value) {
    const date = dateValue(value);
    if (!date) return "时间未知";
    const seconds = Math.max(0, (Date.now() + state.serverOffset - date.getTime()) / 1000);
    if (seconds < 60) return "刚刚";
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
    return `${Math.floor(seconds / 86400)} 天前`;
  }
  function leaseLabel(value) {
    const expires = dateValue(value);
    return expires ? `${Math.max(0, Math.ceil((expires.getTime() - Date.now() - state.serverOffset) / 1000))} 秒后到期` : "未占用";
  }
  function initials(name) { return Array.from(textOr(name, "A")).slice(0, 2).join("").toUpperCase(); }
  function setError(id, message) { $(id).textContent = message || ""; $(id).hidden = !message; }
  function toast(message, error = false) {
    const node = el("div", `toast${error ? " error" : ""}`, message);
    $("toast-region").append(node);
    setTimeout(() => { node.classList.add("exiting"); setTimeout(() => node.remove(), 220); }, error ? 7500 : 4500);
  }
  class ApiError extends Error {
    constructor(message, status, code, details) { super(message); this.status = status; this.code = code; this.details = details; }
  }
  async function api(path, options = {}) {
    const method = options.method || "GET";
    const stateRequest = path === "/api/state" && method === "GET";
    const controller = new AbortController();
    if (stateRequest) state.stateController = controller;
    const timeout = setTimeout(() => controller.abort(), 15000);
    const headers = options.body !== undefined ? {"Content-Type": "application/json"} : {};
    if (stateRequest && options.conditional !== false && state.etag) headers["If-None-Match"] = state.etag;
    try {
      const response = await fetch(path, {
        method, credentials: "same-origin", cache: "no-store", headers,
        ...(options.body !== undefined ? {body: JSON.stringify(options.body)} : {}), signal: controller.signal
      });
      // 304 没有响应正文，直接复用现有状态，避免 JSON 解析与整页 DOM 分配。
      if (stateRequest && response.status === 304) {
        if (!state.data) { state.etag = null; throw new Error("状态缓存不可用，请重新刷新。"); }
        return {notModified: true};
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new ApiError(data.error?.message || `请求失败（HTTP ${response.status}）`, response.status, data.error?.code, data.error?.details);
      if (stateRequest) {
        const etag = response.headers.get("ETag");
        const notModified = Boolean(state.data && etag && etag === state.etag);
        state.etag = etag;
        return {notModified, payload: data};
      }
      // 所有成功写请求使状态条件缓存失效；写后强制读取也不带 If-None-Match。
      if (method !== "GET") state.etag = null;
      return data;
    } catch (error) {
      if (stateRequest && controller.signal.aborted && document.hidden) return {paused: true};
      if (error.name === "AbortError") throw new Error("请求超时，请检查本地服务是否仍在运行。");
      if (error instanceof TypeError) throw new Error("无法连接本地服务，请确认服务已启动。");
      throw error;
    } finally {
      clearTimeout(timeout);
      if (state.stateController === controller) state.stateController = null;
    }
  }
  function connection(kind, label) {
    const node = $("connection-status");
    if (node.className !== `connection ${kind}`) node.className = `connection ${kind}`;
    if (node.lastElementChild.textContent !== label) node.lastElementChild.textContent = label;
  }
  function showLogin(message = "") {
    state.authenticated = false;
    stopPolling(); state.etag = null; state.failures = 0;
    document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
    $("app").hidden = true; $("login-screen").hidden = false;
    setError("login-error", message);
  }
  function showApp() {
    state.authenticated = true;
    $("app").hidden = false; $("login-screen").hidden = true; $("admin-token").value = "";

  }
  function stopPolling() { clearTimeout(state.timer); state.timer = null; }
  function pollingDelay() {
    if (state.failures) return Math.min(60000, 10000 * (2 ** Math.min(state.failures - 1, 3)));
    return tasks().some((task) => task.status === "in_progress") || agents().some((agent) => agent.status === "working") ? 10000 : 30000;
  }
  function schedulePoll() {
    stopPolling();
    if (!state.authenticated || document.hidden) return;
    state.timer = setTimeout(() => { state.timer = null; refresh(); }, pollingDelay());
  }
  function updateTimeDisplays() {
    const checkedLabel = `最近检查于 ${shortTime(state.lastSuccess?.toISOString())}`;
    if ($("last-updated").textContent !== checkedLabel) $("last-updated").textContent = checkedLabel;
    const view = $(`view-${state.view}`);
    view.querySelectorAll("[data-relative-time]").forEach((node) => {
      const text = (node.dataset.timePrefix || "") + relativeTime(node.dataset.relativeTime);
      if (node.textContent !== text) node.textContent = text;
    });
    const detail = taskById(state.selectedTaskId);
    if ($("task-detail-dialog").open && detail && $("detail-lease-value")) $("detail-lease-value").textContent = leaseLabel(detail.lease_expires_at);
    const label = $("polling-label");
    if (label) {
      const text = state.failures ? "连接恢复后自动更新" : pollingDelay() === 10000 ? "工作中 · 每 10 秒更新" : "空闲中 · 每 30 秒检查";
      if (label.textContent !== text) label.textContent = text;
    }
  }
  async function refresh({initial = false, manual = false, force = false} = {}) {
    if (state.loading) {
      if (!force) return;
      await new Promise((resolve) => state.refreshWaiters.push(resolve));
      return refresh({initial, manual, force});
    }
    stopPolling();
    if (document.hidden) return;
    state.loading = true; $("refresh-button").disabled = true;
    try {
      const result = await api("/api/state", {conditional: !force});
      if (result.paused) return;
      state.failures = 0;
      if (!result.notModified) {
        state.data = result.payload;
        const serverDate = dateValue(state.data.server_time);
        if (serverDate) state.serverOffset = serverDate.getTime() - Date.now();
      }
      state.lastSuccess = new Date(); showApp(); connection("connected", "本地服务已连接"); $("connection-banner").hidden = true;
      if (!result.notModified) render();
      updateTimeDisplays();
      if (manual) toast(result.notModified ? "已检查，协作状态没有变化" : "协作状态已更新");
    } catch (error) {
      state.failures = Math.min(state.failures + 1, 4);
      if (error.status === 401 || error.status === 403) showLogin(initial ? "" : "会话已失效，请重新输入管理令牌。");
      else if (!state.authenticated) setError("login-error", error.message);
      else {
        connection("disconnected", "连接中断");
        $("connection-banner").textContent = `无法更新：${error.message} 当前展示上次成功获取的状态，暂时不要据此判断任务所有权。`;
        $("connection-banner").hidden = false;
        updateTimeDisplays();
        if (manual) toast(error.message, true);
      }
    } finally {
      state.loading = false; $("refresh-button").disabled = false;
      state.refreshWaiters.splice(0).forEach((resolve) => resolve());
      schedulePoll();
    }
  }
  function switchView(view) {
    if (!viewLabels[view]) return;
    state.view = view;
    Object.keys(viewLabels).forEach((name) => { $(`view-${name}`).hidden = name !== view; });
    document.querySelectorAll(".nav-item").forEach((button) => {
      const selected = button.dataset.view === view;
      button.classList.toggle("active", selected);
      if (selected) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    });
    $("breadcrumb-current").textContent = viewLabels[view]; document.title = `${viewLabels[view]} · Agent Board`;
    renderActiveView();
    if (state.lastSuccess) updateTimeDisplays();
  }
  function render() {
    $("nav-task-count").textContent = tasks().filter((task) => !["done", "cancelled"].includes(task.status)).length;
    $("nav-agent-count").textContent = agents().filter((agent) => agent.status !== "offline").length;
    $("total-task-count").textContent = tasks().length; $("total-agent-count").textContent = agents().length;
    $("last-updated").textContent = `最近更新于 ${shortTime(state.lastSuccess?.toISOString())}`;
    state.dirtyViews = new Set(["tasks", "agents", "events"]);
    renderActiveView(); renderMarkdownBanner();
    const detail = taskById(state.selectedTaskId);
    if ($("task-detail-dialog").open && detail) {
      if (detail.revision !== state.detailRevision) renderTaskDetail(detail);
      else if ($("detail-lease-value")) $("detail-lease-value").textContent = leaseLabel(detail.lease_expires_at);
    }
  }
  function renderActiveView() {
    if (!state.data || !state.dirtyViews.has(state.view)) return;
    if (state.view === "tasks") { renderStats(); renderTasks(); }
    else if (state.view === "agents") renderAgents();
    else if (state.view === "events") renderEvents();
    state.dirtyViews.delete(state.view);
  }
  function renderStats() {
    const taskList = tasks();
    const definitions = [
      ["任务总数", taskList.length, "▦", `${taskList.filter((task) => task.status === "todo").length} 份任务等待认领`],
      ["正在进行", taskList.filter((task) => task.status === "in_progress").length, "↗", `${agents().filter((agent) => agent.status !== "offline").length} 个 Agent 在线协作`],
      ["等待审核", taskList.filter((task) => task.status === "review").length, "◷", `${taskList.filter((task) => task.status === "blocked").length} 份任务需要排除阻塞`],
      ["已完成", taskList.filter((task) => task.status === "done").length, "✓", "经过审核的协作成果"]
    ];
    const fragment = document.createDocumentFragment();
    definitions.forEach(([label, value, icon, note]) => {
      const card = el("article", "stat-card");
      append(card, append(el("div", "stat-top"), el("span", "", label), el("span", "stat-icon", icon)), el("div", "stat-value", formatNumber(value)), append(el("div", "stat-bottom"), el("span", "small-dot"), el("span", "", note)));
      fragment.append(card);
    });
    $("stats").replaceChildren(fragment);
  }
  function renderTasks() {
    const search = $("task-search").value.trim().toLocaleLowerCase();
    const status = $("status-filter").value; const priority = $("priority-filter").value;
    const filtered = Boolean(search || status !== "all" || priority !== "all");
    $("clear-filters").hidden = !filtered;
    const selected = tasks().filter((task) => (!search || `${task.title}\n${task.description}\n${task.id}`.toLocaleLowerCase().includes(search)) && (status === "all" || task.status === status) && (priority === "all" || String(task.priority) === priority))
      .sort((a, b) => (a.priority - b.priority) || String(b.updated_at).localeCompare(String(a.updated_at)));
    const board = $("task-board"); board.classList.toggle("filtered", filtered); board.classList.toggle("cancelled-visible", !filtered && selected.some((task) => task.status === "cancelled")); board.replaceChildren();
    const empty = $("tasks-empty"); empty.hidden = selected.length > 0; board.hidden = selected.length === 0;
    if (!selected.length) {
      empty.querySelector("h3").textContent = filtered ? "没有找到匹配的任务" : "从第一份任务开始";
      empty.querySelector("p").textContent = filtered ? "尝试其他关键词或清除筛选，查看全部协作任务。" : "写下目标与资源范围，Agent 就可以安全地认领工作。";
      $("empty-create-button").textContent = filtered ? "清除筛选" : "新建任务"; return;
    }
    if (filtered) { selected.forEach((task) => board.append(taskCard(task, true))); return; }
    const columns = ["todo", "in_progress", "review", "done", "blocked"];
    if (selected.some((task) => task.status === "cancelled")) columns.push("cancelled");
    columns.forEach((statusName) => {
      const columnTasks = selected.filter((task) => task.status === statusName);
      const column = el("section", "board-column"); column.dataset.status = statusName;
      column.setAttribute("aria-label", `${statusLabels[statusName]} ${columnTasks.length} 份任务`);
      column.append(append(el("div", "column-header"), el("span", "column-dot"), el("h3", "column-title", statusLabels[statusName]), el("span", "column-count", columnTasks.length), el("span", "column-icon", "⋯")));
      const cards = el("div", "column-cards");
      if (columnTasks.length) columnTasks.forEach((task) => cards.append(taskCard(task))); else cards.append(el("div", "column-empty", "暂无任务"));
      column.append(cards); board.append(column);
    });
  }
  function taskCard(task, showStatus = false) {
    const card = el("button", "task-card"); card.type = "button";
    card.setAttribute("aria-label", `查看任务：${task.title}，${statusLabels[task.status] || task.status}`);
    const priorityClass = [0, 1, 2, 3].includes(task.priority) ? `p${task.priority}` : "";
    append(card, append(el("div", "task-card-top"), el("span", `priority ${priorityClass}`, priorityLabels[task.priority] || "优先级未知"), el("span", "task-id", task.id)), el("h3", "", task.title));
    if (task.description) card.append(el("p", "task-excerpt", task.description));
    if (showStatus) card.append(el("span", `status-badge ${task.status}`, statusLabels[task.status] || task.status));
    const tags = list(task.resources).slice(0, 2);
    if (tags.length || list(task.depends_on).length) {
      const tagRow = el("div", "task-tags"); tags.forEach((resource) => tagRow.append(el("span", "task-tag", resource)));
      if (list(task.resources).length > 2) tagRow.append(el("span", "task-tag", `+${task.resources.length - 2}`));
      if (list(task.depends_on).length) tagRow.append(el("span", "task-tag", `${task.depends_on.length} 项依赖`));
      card.append(tagRow);
    }
    if (task.status === "blocked" && task.blocker) card.append(el("p", "task-blocker", task.blocker));
    if (task.status === "in_progress") { const bar = progressBar("task-progress", task.progress); bar.setAttribute("aria-label", "任务进度"); card.append(bar); }
    const owner = agentById(task.owner_id); const ownerNode = el("div", "task-owner");
    if (owner) append(ownerNode, el("span", "mini-avatar", initials(owner.name)), el("span", "", owner.name));
    else ownerNode.append(el("span", "", task.status === "todo" ? "等待认领" : task.status === "done" ? "已审核交付" : task.status === "review" ? "等待管理员审核" : "暂无认领者"));
    const timing = el("span", "task-timing", task.status === "in_progress" && validNumber(task.progress) ? `${percent(task.progress)}%` : relativeTime(task.updated_at)); timing.title = fullTime(task.updated_at);
    if (task.status !== "in_progress" || !validNumber(task.progress)) timing.dataset.relativeTime = task.updated_at || "";
    card.append(append(el("div", "task-card-footer"), ownerNode, timing)); card.addEventListener("click", () => openTaskDetail(task.id)); return card;
  }
  function renderAgents() {
    const fragment = document.createDocumentFragment();
    const sorted = [...agents()].sort((a, b) => (a.status === "offline") - (b.status === "offline") || String(a.name).localeCompare(String(b.name), "zh-CN"));
    sorted.forEach((agent) => {
      const card = el("article", "agent-card");
      const statusClass = Object.hasOwn(agentStatusLabels, agent.status) ? agent.status : "offline";
      const name = append(el("div"), el("h3", "agent-name", agent.name), el("div", "agent-id", agent.id));
      const status = append(el("span", `agent-state ${statusClass}`), el("span", "small-dot"), el("span", "", agentStatusLabels[agent.status] || "未知"));
      card.append(append(el("div", "agent-card-header"), el("div", "agent-avatar", initials(agent.name)), name, status));
      const meta = el("div", "agent-meta");
      append(meta, append(el("div"), el("span", "", "客户端"), el("strong", "", textOr(agent.client))), append(el("div"), el("span", "", "模型"), el("strong", "", textOr(agent.model))));
      card.append(meta, contextCard(agent.context));
      const currentTask = taskById(agent.current_task_id); const taskButton = el("button", "agent-task"); taskButton.type = "button"; taskButton.disabled = !currentTask;
      append(taskButton, el("span", "agent-task-label", "当前任务"), el("span", "agent-task-title", currentTask?.title || (agent.current_task_id ? `任务 ${agent.current_task_id}` : "等待下一份任务")));
      if (currentTask) taskButton.addEventListener("click", () => openTaskDetail(currentTask.id));
      card.append(taskButton, el("p", "agent-summary", textOr(agent.summary, "尚未报告工作摘要")));
      if (list(agent.capabilities).length) { const row = el("div", "agent-capabilities"); list(agent.capabilities).forEach((value) => row.append(el("span", "task-tag", value))); card.append(row); }
      const heartbeat = el("span", "", `心跳 ${relativeTime(agent.last_seen_at)}`); heartbeat.title = fullTime(agent.last_seen_at);
      heartbeat.dataset.relativeTime = agent.last_seen_at || ""; heartbeat.dataset.timePrefix = "心跳 ";
      card.append(append(el("div", "agent-card-footer"), heartbeat, el("span", "", agent.status === "offline" ? "心跳已超时" : "已连接本地协议"))); fragment.append(card);
    });
    $("agents-grid").replaceChildren(fragment); $("agents-empty").hidden = sorted.length > 0;
  }
  function contextCard(context = {}) {
    context = context || {}; const container = el("div", "agent-context");
    const usedKnown = validNumber(context.used_tokens); const maxKnown = validNumber(context.max_tokens) && context.max_tokens > 0;
    const source = Object.hasOwn(sourceLabels, context.source) ? context.source : "unknown";
    container.append(append(el("div", "context-heading"), el("span", "", "上下文使用"), el("span", "context-source", sourceLabels[source])));
    const value = el("div", "context-value", usedKnown ? formatNumber(context.used_tokens) : "未知"); value.append(el("small", "", maxKnown ? `/ ${formatNumber(context.max_tokens)} tokens` : "/ 容量未知"));
    const rawRatio = usedKnown && maxKnown ? (context.used_tokens / context.max_tokens) * 100 : null;
    if (rawRatio !== null) value.append(el("span", "context-percent", `${rawRatio.toFixed(1)}%`));
    container.append(value);
    const track = rawRatio === null ? el("div", "context-track unknown") : progressBar(`context-track${rawRatio >= 90 ? " critical" : rawRatio >= 75 ? " high" : ""}`, rawRatio);
    track.setAttribute("aria-label", rawRatio === null ? "上下文容量占用未知" : `上下文容量占用 ${rawRatio.toFixed(1)}%，${sourceLabels[source]}`);
    if (context.updated_at) {
      const timestamp = el("p", "context-timestamp", `统计上报于 ${relativeTime(context.updated_at)}`);
      timestamp.title = fullTime(context.updated_at);
      timestamp.dataset.relativeTime = context.updated_at; timestamp.dataset.timePrefix = "统计上报于 ";
      container.append(timestamp);
    }
    container.append(track, el("p", "context-note", context.note || (source === "estimated" ? "此值为估算，实际消耗取决于客户端的统计方式。" : !usedKnown ? "客户端尚未提供可用的上下文统计。" : source === "unknown" ? "来源尚未标明，请结合客户端统计判断。" : "由 Agent 所在客户端提供，仅用于观察协作情况。"))); return container;
  }
  function renderEvents() {
    const fragment = document.createDocumentFragment(); const events = list(state.data?.events).slice(0, 100);
    events.forEach((event) => {
      const row = el("tr"); const links = el("div", "event-links");
      if (event.task_id) {
        const button = el("button", "text-button", taskById(event.task_id)?.title || event.task_id); button.type = "button";
        button.addEventListener("click", () => openTaskDetail(event.task_id)); links.append(button);
      }
      if (event.agent_id) links.append(el("span", "event-agent", agentById(event.agent_id)?.name || event.agent_id));
      if (!event.task_id && !event.agent_id) links.append(el("span", "event-agent", "本地服务"));
      const time = el("td", "", fullTime(event.at)); time.title = textOr(event.at, "时间未知");
      append(row, time, append(el("td"), el("span", "event-type", eventLabels[event.type] || textOr(event.type, "事件"))), append(el("td"), links), el("td", "", event.message)); fragment.append(row);
    });
    $("events-body").replaceChildren(fragment); $("events-empty").hidden = events.length > 0; document.querySelector(".event-table").hidden = events.length === 0;
  }
  function conflictsFrom(value) {
    if (Array.isArray(value)) return value;
    if (value && typeof value === "object") return Object.entries(value).map(([path, reason]) => ({path, reason}));
    if (typeof value === "number" && value > 0) return [{reason: `发现 ${value} 个冲突，请查看本地文件与服务日志。`}]; return [];
  }
  function countFrom(value) { return Array.isArray(value) ? value.length : typeof value === "number" ? value : value && typeof value === "object" ? Object.keys(value).length : 0; }
  function renderMarkdownBanner() {
    const markdown = state.data?.markdown || {}; const conflicts = conflictsFrom(markdown.conflicts); const pending = countFrom(markdown.pending);
    const banner = $("markdown-banner"); banner.replaceChildren(); banner.hidden = !conflicts.length && !pending && !markdown.error;
    if (conflicts.length) {
      banner.append(el("span", "", `有 ${conflicts.length} 个 Markdown 同步冲突。原文件已保留，解决冲突后再同步。`));
      const button = el("button", "text-button", "查看冲突 →"); button.addEventListener("click", () => showSyncResult({conflicts, imported: 0, exported: 0}, true)); banner.append(button);
    } else if (markdown.error) banner.append(el("span", "", `Markdown 快照导出失败：${markdown.error}。点击「同步 Markdown」重试，并检查本地文件权限。`));
    else if (pending) banner.append(el("span", "", `${pending} 项 Markdown 快照等待同步。点击右上角「同步 Markdown」重试。`));
  }
  function openDialog(dialog) { state.focusBeforeDialog.set(dialog, document.activeElement); if (!dialog.open) dialog.showModal(); }
  function openTaskDetail(id) {
    const task = taskById(id); if (!task) { toast("任务不在当前状态中，请刷新后重试。", true); return; }
    state.selectedTaskId = id; renderTaskDetail(task); openDialog($("task-detail-dialog"));
  }
  function detailSection(title, content) { return append(el("section", "detail-section"), el("h3", "", title), content); }
  function detailText(text, fallback) { return el("div", text ? "detail-prose" : "empty-value", textOr(text, fallback)); }
  function renderTaskDetail(task) {
    state.detailRevision = task.revision;
    const content = $("task-detail-content");
    const badges = append(el("div", "detail-badges"), el("span", `status-badge ${task.status}`, statusLabels[task.status] || task.status), el("span", `priority p${task.priority}`, priorityLabels[task.priority]), el("span", "mono", `${task.id} · revision ${task.revision}`));
    const meta = el("dl", "detail-meta"); const owner = agentById(task.owner_id);
    const fields = [
      ["当前认领者", owner?.name || textOr(task.owner_id, "暂无认领者")], ["租约", leaseLabel(task.lease_expires_at)],
      ["认领代次", task.claim_generation ?? 0], ["创建时间", fullTime(task.created_at)],
      ["更新时间", fullTime(task.updated_at)], ["当前进度", validNumber(task.progress) ? `${percent(task.progress)}%` : "未报告"]
    ];
    fields.forEach(([label, value]) => {
      const dd = el("dd", "", value); if (label === "租约") dd.id = "detail-lease-value";
      meta.append(append(el("div"), el("dt", "", label), dd));
    });
    content.replaceChildren(el("h3", "detail-title", task.title), badges, meta);
    content.append(detailSection("任务描述", detailText(task.description, "尚未填写任务描述")));
    if (task.summary) content.append(detailSection("最近进展", detailText(task.summary)));
    const resources = el("div", "detail-resources"); list(task.resources).forEach((resource) => resources.append(el("code", "", resource)));
    if (!list(task.resources).length) resources.append(el("span", "empty-value", "未声明独占资源"));
    content.append(detailSection("独占资源", resources));
    const dependencies = el("div", "detail-dependencies");
    list(task.depends_on).forEach((id) => {
      const target = taskById(id); const button = el("button", "", `${target?.status === "done" ? "✓ " : "↳ "}${target?.title || id} · ${id}`); button.type = "button";
      button.addEventListener("click", () => openTaskDetail(id)); dependencies.append(button);
    });
    if (!list(task.depends_on).length) dependencies.append(el("span", "empty-value", "无前置依赖"));
    content.append(detailSection("依赖任务", dependencies));
    const capabilities = el("div", "detail-resources"); list(task.required_capabilities).forEach((capability) => capabilities.append(el("code", "", capability)));
    if (!list(task.required_capabilities).length) capabilities.append(el("span", "empty-value", "无特定能力要求"));
    content.append(detailSection("所需能力", capabilities));
    if (task.blocker) content.append(detailSection("阻塞原因", detailText(task.blocker)));
    if (task.result) content.append(detailSection("交付结果与验证", detailText(task.result)));
    const actions = $("task-detail-actions"); actions.replaceChildren(el("span", "detail-action-note", "管理操作使用当前 revision 校验，避免覆盖并发修改。"));
    if (["todo", "blocked"].includes(task.status)) {
      const edit = el("button", "button secondary", "编辑任务"); edit.addEventListener("click", () => { $("task-detail-dialog").close(); openTaskForm(task); }); actions.append(edit);
    }
    if (["blocked", "review", "done", "cancelled"].includes(task.status)) {
      const reopen = el("button", "button secondary", "重新开放"); reopen.addEventListener("click", () => openAction("reopen", task)); actions.append(reopen);
    }
    if (!["done", "cancelled"].includes(task.status)) {
      const cancel = el("button", "button danger-outline", "取消任务"); cancel.addEventListener("click", () => openAction("cancel", task)); actions.append(cancel);
    }
    if (task.status === "review") {
      const approve = el("button", "button primary", "审核通过 ✓"); approve.addEventListener("click", () => openAction("approve", task)); actions.append(approve);
    }
  }
  function openTaskForm(task = null) {
    state.editing = task ? {id: task.id, revision: task.revision} : null; $("task-form").reset();
    $("task-form-title").textContent = task ? "编辑任务" : "新建任务"; $("task-form-eyebrow").textContent = task ? "EDIT A TASK" : "CREATE A TASK";
    $("task-form-intro").textContent = task ? `编辑 ${task.id} · revision ${task.revision}。如果任务被其他协作者修改，保存将被拒绝。` : "清楚的目标与资源范围，是安全协作的起点。";
    $("task-form-submit").textContent = task ? "保存修改" : "创建任务";
    $("task-title").value = task?.title || ""; $("task-description").value = task?.description || ""; $("task-priority").value = String(task?.priority ?? 2);
    $("task-capabilities").value = list(task?.required_capabilities).join(", "); $("task-resources").value = list(task?.resources).join("\n"); $("task-dependencies").value = list(task?.depends_on).join(", ");
    setError("task-form-error", ""); openDialog($("task-form-dialog")); $("task-title").focus();
  }
  function splitValues(value) { return [...new Set(value.split(/[,，\n]/).map((item) => item.trim()).filter(Boolean))]; }
  async function saveTask(event) {
    event.preventDefault(); if (state.mutation) return;
    const title = $("task-title").value.trim(); if (!title) { setError("task-form-error", "请填写任务标题。"); return; }
    const editing = state.editing;
    const payload = {title, description: $("task-description").value, priority: Number($("task-priority").value), resources: $("task-resources").value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean), depends_on: splitValues($("task-dependencies").value), required_capabilities: splitValues($("task-capabilities").value)};
    if (editing) payload.expected_revision = editing.revision;
    state.mutation = true; $("task-form-submit").disabled = true; setError("task-form-error", "");
    try {
      const response = await api(editing ? `/api/tasks/${encodeURIComponent(editing.id)}/edit` : "/api/tasks", {method: "POST", body: payload});
      $("task-form-dialog").close(); toast(editing ? "任务修改已保存" : "任务已创建，等待 Agent 认领"); await refresh({force: true});
      if (state.authenticated && !document.hidden && response.task?.id) openTaskDetail(response.task.id);
    } catch (error) {
      if (error.status === 401 || error.status === 403) showLogin("管理会话已失效，请重新登录。");
      else { setError("task-form-error", error.status === 409 ? `${error.message} 你的输入已保留，请先查看最新任务，再重新打开编辑。` : error.message); if (error.status === 409) await refresh({force: true}); }
    } finally { state.mutation = false; $("task-form-submit").disabled = false; }
  }
  function openAction(action, task) {
    state.action = {type: action, taskId: task.id, revision: task.revision};
    const definitions = {
      approve: ["审核通过", `确认「${task.title}」已达到验收要求？通过后会标记为已完成，依赖此任务的后续任务可以被认领。`, "确认通过"],
      reopen: ["重新开放任务", `将「${task.title}」重新放回待认领。后续认领将使用新的租约与认领代次。`, "重新开放"],
      cancel: ["取消任务", `确认取消「${task.title}」？若正在执行，当前所有权及资源将立即释放。`, "确认取消"]
    };
    const [title, description, button] = definitions[action];
    $("action-title").textContent = title; $("action-description").textContent = description; $("action-submit").textContent = button;
    $("action-submit").className = `button ${action === "cancel" ? "danger" : "primary"}`; $("action-reason-group").hidden = action === "approve"; $("action-reason").value = "";
    setError("action-error", ""); openDialog($("action-dialog"));
  }
  async function applyAction(event) {
    event.preventDefault(); if (state.mutation || !state.action) return;
    const action = {...state.action}; state.mutation = true; $("action-submit").disabled = true; setError("action-error", "");
    const payload = {expected_revision: action.revision}; if (action.type !== "approve") payload.reason = $("action-reason").value;
    try {
      await api(`/api/tasks/${encodeURIComponent(action.taskId)}/${action.type}`, {method: "POST", body: payload});
      $("action-dialog").close(); toast({approve: "任务已审核完成", reopen: "任务已重新开放", cancel: "任务已取消"}[action.type]); await refresh({force: true});
    } catch (error) {
      if (error.status === 401 || error.status === 403) showLogin("管理会话已失效，请重新登录。");
      else { setError("action-error", error.status === 409 ? `${error.message} 请关闭此确认框，查看最新任务后重新操作。` : error.message); if (error.status === 409) await refresh({force: true}); }
    } finally { state.mutation = false; $("action-submit").disabled = false; }
  }
  function showSyncResult(result, existing = false) {
    const content = $("sync-content"); const conflicts = conflictsFrom(result.conflicts); const summary = el("div", "sync-summary");
    [["导入任务", countFrom(result.imported)], ["导出快照", countFrom(result.exported)], ["冲突", conflicts.length]].forEach(([label, count]) => summary.append(append(el("div", "sync-stat"), el("span", "", label), el("strong", "", count))));
    content.replaceChildren(summary);
    if (conflicts.length) {
      content.append(el("p", "muted", "发生冲突的文件已保留。请对照网页中的最新任务，合并需要保留的改动，并在确认 revision 与账本一致后再同步。不要通过修改状态或所有者绕过认领。"));
      const items = el("div", "sync-conflicts");
      conflicts.forEach((conflict) => {
        const item = el("div", "conflict-item");
        if (typeof conflict === "string") item.append(el("strong", "", conflict));
        else if (conflict && typeof conflict === "object") { item.append(el("strong", "", textOr(conflict.path || conflict.file || conflict.task_id || conflict.id, "Markdown 文件冲突"))); item.append(el("p", "", textOr(conflict.reason || conflict.message || conflict.error, JSON.stringify(conflict)))); }
        else item.append(el("strong", "", String(conflict)));
        items.append(item);
      }); content.append(items);
    } else content.append(el("p", "muted", existing ? "目前没有记录到同步冲突。" : "同步完成。可编辑的本地任务变更已导入，账本状态已导出为 Markdown 快照。"));
    openDialog($("sync-dialog"));
  }
  async function syncMarkdown() {
    if (state.mutation) return; state.mutation = true; $("sync-button").disabled = true;
    try { const result = await api("/api/markdown/sync", {method: "POST", body: {}}); await refresh({force: true}); if (state.authenticated) showSyncResult(result); }
    catch (error) { if (error.status === 401 || error.status === 403) showLogin("管理会话已失效，请重新登录。"); else toast(`同步失败：${error.message}`, true); }
    finally { state.mutation = false; $("sync-button").disabled = false; }
  }
  function clearFilters() { $("task-search").value = ""; $("status-filter").value = "all"; $("priority-filter").value = "all"; renderTasks(); }
  async function login(token) {
    const button = $("login-form").querySelector("button[type=submit]"); button.disabled = true; setError("login-error", "");
    try { await api("/api/session", {method: "POST", body: {token}}); $("admin-token").value = ""; await refresh({initial: true}); }
    catch (error) { setError("login-error", error.message); } finally { button.disabled = false; }
  }
  document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
  document.querySelector(".brand").addEventListener("click", (event) => { event.preventDefault(); switchView("tasks"); });
  $("login-form").addEventListener("submit", (event) => { event.preventDefault(); login($("admin-token").value.trim()); });
  async function logout() {
    $("logout-button").disabled = true;
    try { await api("/api/session", {method: "DELETE"}); showLogin(); } catch (error) { toast(`退出失败：${error.message}`, true); } finally { $("logout-button").disabled = false; }
  }
  $("logout-button").addEventListener("click", logout);
  $("mobile-logout-button").addEventListener("click", logout);
  $("refresh-button").addEventListener("click", () => refresh({manual: true, force: true})); $("sync-button").addEventListener("click", syncMarkdown);
  $("task-search").addEventListener("input", renderTasks); $("status-filter").addEventListener("change", renderTasks); $("priority-filter").addEventListener("change", renderTasks); $("clear-filters").addEventListener("click", clearFilters);
  $("create-task-button").addEventListener("click", () => openTaskForm()); $("empty-create-button").addEventListener("click", () => { if (!$("clear-filters").hidden) clearFilters(); else openTaskForm(); });
  $("task-form").addEventListener("submit", saveTask); $("action-form").addEventListener("submit", applyAction);
  document.querySelectorAll(".close-dialog").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
  document.querySelectorAll("dialog").forEach((dialog) => {
    dialog.addEventListener("click", (event) => { if (event.target !== dialog) return; const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); });
    dialog.addEventListener("close", () => { const focus = state.focusBeforeDialog.get(dialog); if (focus?.isConnected && !focus.closest("[hidden]")) focus.focus(); });
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopPolling();
      state.stateController?.abort();
    } else if (state.authenticated || !state.data) refresh({initial: !state.authenticated, force: true});
  });
  // URL fragment 不发送给服务器；在登录请求前清除，避免历史记录保留令牌。
  const fragment = new URLSearchParams(window.location.hash.slice(1)); const token = fragment.get("token");
  if (token !== null) { history.replaceState(null, "", window.location.pathname + window.location.search); login(token); } else refresh({initial: true});
})();
