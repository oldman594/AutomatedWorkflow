const state = {
  tasks: [],
  current: null,
  detail: null,
  artifactKind: "plan",
  stream: null,
  health: null,
  runners: [],
};

const stages = [
  ["product", "产品", "需求对齐", "product"],
  ["planner", "规划", "拆分任务"],
  ["reader", "阅读", "代码理解", "reader"],
  ["design", "设计", "确定方案", "architecture"],
  ["coder", "编码", "实现变更", "coder"],
  ["build", "构建", "编译修复"],
  ["test", "测试", "回归修复"],
  ["review", "审查", "检查风险", "reviewer"],
  ["acceptance", "验收", "指标校验", "acceptance"],
  ["delivery", "交付", "提交审批"],
];

const statusLabels = {
  draft: "待运行", queued: "排队中", assigned: "已下发", accepted: "已接收",
  running: "运行中", wait_permission: "等待授权",
  waiting_approval: "待审批", completed: "已完成", failed: "失败", cancelled: "已取消",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try { message = formatApiError(await response.json(), message); } catch (_) {}
    throw new Error(message);
  }
  return response.json();
}

function formatApiError(payload, fallback) {
  const detail = payload?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((error) => {
      const location = Array.isArray(error.loc)
        ? error.loc.filter((part) => part !== "body").join(".")
        : "请求参数";
      return `${location || "请求参数"}：${error.msg || "格式不正确"}`;
    }).join("；");
  }
  if (detail && typeof detail === "object") {
    return detail.message || JSON.stringify(detail);
  }
  return fallback;
}

function initIcons() {
  if (window.lucide) window.lucide.createIcons({ attrs: { "stroke-width": 1.8 } });
}

async function loadHealth() {
  try {
    state.health = await api("/health");
    $("#health-dot").classList.add("online");
    $("#health-text").textContent = state.health.mock_llm
      ? "Mock 模式"
      : state.health.provider === "codex_cli" ? "Codex CLI"
        : state.health.provider === "deepseek" ? "DeepSeek API"
          : state.health.provider === "doubao" ? "豆包 API"
            : state.health.provider === "qwen" ? "千问 API" : "OpenAI API";
    $("#model-name").textContent = state.health.model;
    $("#model-input").placeholder = state.health.model;
  } catch (error) {
    $("#health-text").textContent = "服务离线";
  }
}

async function loadTasks(selectId = null) {
  try {
    state.tasks = await api("/tasks");
    renderTaskList();
    if (selectId) await selectTask(selectId);
    else if (state.current) await selectTask(state.current);
    else if (state.tasks.length) await selectTask(state.tasks[0].id);
  } catch (error) { toast(error.message); }
}

async function loadRunners() {
  try {
    state.runners = await api("/runners");
    const select = $("#runner-select");
    const selected = select.value;
    select.innerHTML = `<option value="">当前服务器</option>`;
    state.runners.forEach((runner) => {
      const option = document.createElement("option");
      option.value = runner.id;
      option.textContent = `${runner.name} · ${runner.online ? "在线" : "离线"}`;
      select.appendChild(option);
    });
    select.value = state.runners.some((runner) => runner.id === selected) ? selected : "";
    updateRepositoryPlaceholder();
  } catch (_) {}
}

function renderTaskList() {
  const list = $("#task-list");
  list.innerHTML = "";
  state.tasks.forEach((task) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = `task-item ${task.id === state.current ? "active" : ""}`;
    item.innerHTML = `<strong></strong><div><span class="mini-dot ${task.status}"></span><span>${statusLabels[task.status] || task.status}</span><span>·</span><span>${relativeTime(task.updated_at)}</span></div>`;
    item.querySelector("strong").textContent = task.title;
    item.addEventListener("click", () => selectTask(task.id));
    list.appendChild(item);
  });
}

async function selectTask(id) {
  if (state.stream) { state.stream.close(); state.stream = null; }
  state.current = id;
  try {
    state.detail = await api(`/tasks/${id}`);
    const hasDelivery = state.detail.artifacts.some((item) => item.kind === "delivery");
    if (hasDelivery && ["waiting_approval", "completed"].includes(state.detail.task.status)) {
      state.artifactKind = "delivery";
    }
    $("#empty-state").classList.add("hidden");
    $("#task-detail").classList.remove("hidden");
    renderTaskList();
    renderDetail();
    const status = state.detail.task.status;
    if (["queued", "assigned", "accepted", "running", "wait_permission"].includes(status)) connectStream(id);
  } catch (error) { toast(error.message); }
  $(".sidebar").classList.remove("open");
}

function renderDetail() {
  const { task, events } = state.detail;
  const acceptance = latestJsonArtifact("acceptance");
  const delivery = latestJsonArtifact("delivery");
  $("#breadcrumb-title").textContent = task.title;
  $("#task-title").textContent = task.title;
  $("#task-id").textContent = `RUN-${task.id.toUpperCase()}`;
  $("#task-status").textContent = task.status === "waiting_approval" && delivery?.source_sync_requested
    ? delivery.source_applied ? "已写入本地" : acceptance?.accepted ? "本地同步失败" : "待人工处理"
    : task.status === "waiting_approval" && acceptance?.accepted === false
      ? "待人工处理"
      : statusLabels[task.status] || task.status;
  $("#task-status").className = `status-pill ${task.status}`;
  $("#task-repo").textContent = task.repository;
  $("#task-branch").textContent = task.branch || "自动生成";
  const worktree = [...state.detail.artifacts].reverse().find((item) => item.kind === "worktree");
  $("#worktree-line").classList.toggle("hidden", !worktree);
  $("#task-worktree").textContent = worktree?.content || "";
  $("#runner-line").classList.toggle("hidden", !task.runner_id);
  $("#task-runner").textContent = task.runner_id || "";
  $("#task-requirement").textContent = task.requirement;
  $("#progress-value").textContent = `${task.progress}%`;
  $("#progress-bar").style.width = `${task.progress}%`;
  renderStages(task.stage, task.status);
  renderEvents(events);
  renderArtifact();
  renderActions(task);
  initIcons();
}

function renderStages(current, status) {
  const rail = $("#stage-rail");
  const index = stages.findIndex(([key]) => key === current);
  rail.innerHTML = stages.map(([key, title, subtitle, routeRole], i) => {
    const done = index > i || status === "completed";
    const active = index === i && !["completed", "cancelled"].includes(status);
    const marker = done ? "✓" : String(i + 1).padStart(2, "0");
    const role = routeRole || key;
    const route = state.health?.agent_routes?.[role];
    const routeLabel = route ? `${route.provider} · ${route.model || "未配置"}` : "本地工具";
    return `<div class="stage ${done ? "done" : ""} ${active ? "current" : ""}"><span class="stage-index">${marker}</span><strong>${title}</strong><span>${subtitle}</span><em title="${escapeHtml(routeLabel)}">${escapeHtml(routeLabel)}</em></div>`;
  }).join("");
}

function renderEvents(events) {
  $("#event-count").textContent = events.length;
  $("#event-list").innerHTML = [...events].reverse().map((event) => {
    const output = event.data?.output ? `<pre class="event-output">${escapeHtml(trimOutput(event.data.output))}</pre>` : "";
    return `<article class="event ${event.level}"><p>${escapeHtml(event.message)}</p><div><span>${event.stage || "system"}</span><span>${formatTime(event.created_at)}</span></div>${output}</article>`;
  }).join("") || `<div class="artifact-empty">等待运行记录</div>`;
}

function renderArtifact() {
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.kind === state.artifactKind));
  const artifact = [...state.detail.artifacts].reverse().find((item) => item.kind === state.artifactKind);
  $("#artifact-empty").classList.toggle("hidden", Boolean(artifact));
  $("#artifact-content").classList.toggle("hidden", !artifact);
  if (artifact) $("#artifact-content code").textContent = prettyArtifact(artifact);
}

function prettyArtifact(artifact) {
  if (artifact.kind === "delivery") {
    try { return formatDelivery(JSON.parse(artifact.content)); } catch (_) {}
  }
  if (["product_spec", "requirement_assessment", "plan", "reading", "review", "acceptance"].includes(artifact.kind)) {
    try { return JSON.stringify(JSON.parse(artifact.content), null, 2); } catch (_) {}
  }
  return artifact.content;
}

function formatDelivery(delivery) {
  const files = delivery.changed_files?.length
    ? delivery.changed_files.map((path) => `- ${path}`).join("\n")
    : "- 无";
  const notes = delivery.notes?.length ? delivery.notes.map((note) => `- ${note}`).join("\n") : "- 无";
  const sourceStatus = delivery.source_sync_requested
    ? delivery.source_applied
      ? `已直接更新：${delivery.source_repository}`
      : `未更新：${delivery.source_apply_error || "未知错误"}`
    : "未启用";
  return `功能结果\n${delivery.summary}\n\n验收状态\n${delivery.accepted ? "通过" : "需人工处理"} · ${delivery.acceptance_score}/100\n\n本地仓库\n${sourceStatus}\n\n变更文件\n${files}\n\n运行命令\n${delivery.run_command || "未提供"}\n\n实际输出\n${delivery.run_output || "未采集"}\n\n构建命令\n${delivery.build_command || "未提供"}\n\n测试命令\n${delivery.test_command || "未提供"}\n\n备注\n${notes}`;
}

function renderActions(task) {
  const actions = $("#top-actions");
  actions.innerHTML = "";
  if (task.status === "draft") actions.append(actionButton("play", "开始运行", "primary", startCurrent));
  if (task.status === "failed") actions.append(actionButton("rotate-ccw", "重新运行", "primary", startCurrent));
  if (["queued", "assigned", "accepted", "running", "wait_permission"].includes(task.status)) actions.append(actionButton("square", "取消", "secondary danger", cancelCurrent));
  if (task.status === "waiting_approval") {
    const acceptance = latestJsonArtifact("acceptance");
    actions.append(actionButton("check", acceptance?.accepted === false ? "结束任务" : "确认完成", "secondary", () => approveCurrent("complete")));
    if (acceptance?.accepted !== false && !task.sync_to_source) {
      actions.append(actionButton("git-commit-horizontal", "提交代码", "primary", () => approveCurrent("commit")));
    }
  }
  const hasWorkspace = state.detail.artifacts.some((item) => item.kind === "worktree");
  const delivery = latestJsonArtifact("delivery");
  if (!task.runner_id && hasWorkspace && delivery?.source_applied !== true && ["waiting_approval", "completed", "failed"].includes(task.status)) {
    actions.prepend(actionButton("download", "下载产物", "secondary", downloadCurrent));
  }
}

function latestJsonArtifact(kind) {
  const artifact = [...(state.detail?.artifacts || [])].reverse().find((item) => item.kind === kind);
  if (!artifact) return null;
  try { return JSON.parse(artifact.content); } catch (_) { return null; }
}

function actionButton(icon, label, className, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.title = label;
  button.innerHTML = `<i data-lucide="${icon}" aria-hidden="true"></i><span>${label}</span>`;
  button.addEventListener("click", handler);
  return button;
}

function connectStream(id) {
  state.stream = new EventSource(`/api/tasks/${id}/events`);
  state.stream.addEventListener("workflow", (message) => {
    const event = JSON.parse(message.data);
    if (!state.detail.events.some((item) => item.id === event.id)) state.detail.events.push(event);
    renderEvents(state.detail.events);
  });
  state.stream.addEventListener("status", (message) => {
    state.detail.task = JSON.parse(message.data);
    renderDetail();
    const status = state.detail.task.status;
    if (["waiting_approval", "completed", "failed", "cancelled"].includes(status)) {
      state.stream.close(); state.stream = null;
      refreshCurrent();
    }
  });
  state.stream.onerror = () => { if (state.stream) { state.stream.close(); state.stream = null; } };
}

async function startCurrent() {
  await runAction(`/tasks/${state.current}/start`, "任务已进入队列");
  await selectTask(state.current);
}

async function cancelCurrent() { await runAction(`/tasks/${state.current}/cancel`, "已请求取消"); }
async function approveCurrent(action) { await runAction(`/tasks/${state.current}/approve`, "审批已完成", { action }); }
function downloadCurrent() { window.location.assign(`/api/tasks/${state.current}/download`); }

async function runAction(path, success, body = null) {
  try {
    await api(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });
    toast(success);
    await refreshCurrent();
  } catch (error) { toast(error.message); }
}

async function refreshCurrent() {
  if (!state.current) return loadTasks();
  state.detail = await api(`/tasks/${state.current}`);
  state.tasks = await api("/tasks");
  renderTaskList(); renderDetail();
}

function openCreate() {
  loadRunners();
  $("#create-overlay").classList.remove("hidden");
  $("#create-overlay").setAttribute("aria-hidden", "false");
  setTimeout(() => $("#task-form input[name=title]").focus(), 0);
}

function closeCreate() {
  $("#create-overlay").classList.add("hidden");
  $("#create-overlay").setAttribute("aria-hidden", "true");
  $("#form-error").classList.add("hidden");
}

async function submitTask(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type=submit]");
  const data = Object.fromEntries(new FormData(form));
  data.runner_id = data.runner_id || null;
  data.include_local_changes = form.include_local_changes.checked;
  data.sync_to_source = form.sync_to_source.checked;
  data.auto_apply = form.auto_apply.checked;
  data.auto_commit = form.auto_commit.checked;
  data.branch = data.branch || "";
  ["model", "build_command", "test_command"].forEach((key) => {
    if (!data[key]) data[key] = null;
  });
  button.disabled = true;
  try {
    const task = await api("/tasks", { method: "POST", body: JSON.stringify(data) });
    await api(`/tasks/${task.id}/start`, { method: "POST" });
    form.reset(); form.auto_apply.checked = true;
    closeCreate(); toast("任务已创建并开始运行");
    await loadTasks(task.id);
  } catch (error) {
    $("#form-error").textContent = error.message;
    $("#form-error").classList.remove("hidden");
  } finally { button.disabled = false; }
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message; element.classList.remove("hidden");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => element.classList.add("hidden"), 3500);
}

function relativeTime(value) {
  const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

function formatTime(value) {
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value));
}
function trimOutput(value) { const lines = value.split("\n"); return lines.slice(-8).join("\n"); }
function escapeHtml(value) { return value.replace(/[&<>'"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[c]); }
function updateRepositoryPlaceholder() {
  const runner = state.runners.find((item) => item.id === $("#runner-select").value);
  $("#repository-input").placeholder = runner?.roots?.[0] || "/home/user/project";
}

document.addEventListener("DOMContentLoaded", () => {
  initIcons(); loadHealth(); loadRunners(); loadTasks();
  $("#new-task-button").addEventListener("click", openCreate);
  $$('[data-open-create]').forEach((button) => button.addEventListener("click", openCreate));
  $("#close-create").addEventListener("click", closeCreate);
  $("#cancel-create").addEventListener("click", closeCreate);
  $("#create-overlay").addEventListener("click", (event) => { if (event.target.id === "create-overlay") closeCreate(); });
  $("#task-form").addEventListener("submit", submitTask);
  $("#runner-select").addEventListener("change", updateRepositoryPlaceholder);
  $("#refresh-button").addEventListener("click", () => loadTasks());
  $("#mobile-menu").addEventListener("click", () => $(".sidebar").classList.toggle("open"));
  $("#artifact-tabs").addEventListener("click", (event) => {
    const tab = event.target.closest(".tab");
    if (tab) { state.artifactKind = tab.dataset.kind; renderArtifact(); }
  });
  $("#copy-worktree").addEventListener("click", async () => {
    const path = $("#task-worktree").textContent;
    if (!path) return;
    try { await navigator.clipboard.writeText(path); toast("工作区路径已复制"); }
    catch (_) { toast(path); }
  });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeCreate(); });
});
