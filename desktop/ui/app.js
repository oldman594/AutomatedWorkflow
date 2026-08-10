const invoke = (...args) => window.__TAURI__.core.invoke(...args);
const providers = ["openai", "deepseek", "doubao", "qwen", "codex_cli"];
const roles = [
  ["product", "产品经理"], ["reader", "代码阅读"], ["planner", "任务规划"],
  ["architecture", "架构设计"], ["coder", "编码"], ["reviewer", "代码审查"],
  ["acceptance", "产品验收"],
];
let snapshot = null;
let statusTimer = null;

const $ = (selector) => document.querySelector(selector);

function providerOptions(selected) {
  return providers.map((provider) => `<option value="${provider}" ${provider === selected ? "selected" : ""}>${provider}</option>`).join("");
}

function renderRoutes(routes = {}) {
  $("#route-list").innerHTML = roles.map(([role, label]) => {
    const route = routes[role] || { provider: $("#provider").value || "deepseek", model: "" };
    return `<div class="route-row" data-role="${role}"><strong>${label}</strong><select>${providerOptions(route.provider)}</select><input value="${escapeAttribute(route.model)}" placeholder="继承默认模型"></div>`;
  }).join("");
}

function renderRoots(roots = []) {
  const list = $("#root-list");
  list.innerHTML = "";
  const values = roots.length ? roots : [""];
  values.forEach((root) => {
    const row = document.createElement("div");
    row.className = "root-row";
    row.innerHTML = `<input value="${escapeAttribute(root)}" placeholder="/home/user/projects" required><button type="button" title="删除目录">&times;</button>`;
    row.querySelector("button").addEventListener("click", () => { row.remove(); if (!list.children.length) renderRoots(); });
    list.appendChild(row);
  });
}

function renderSecretState(id, configured) {
  const element = $(id);
  element.textContent = configured ? "已保存在系统密钥库，留空保持不变" : "尚未配置";
}

function populate(data) {
  snapshot = data;
  const config = data.config;
  $("#server-url").value = config.serverUrl;
  $("#runner-id").value = config.runnerId;
  $("#runner-name").value = config.runnerName;
  $("#runner-heading").textContent = config.runnerName;
  $("#runner-command").value = config.runnerCommand;
  $("#worktree-root").value = config.worktreeRoot;
  $("#provider").innerHTML = providerOptions(config.provider);
  $("#model").value = config.model;
  $("#mock-llm").checked = config.mockLlm;
  renderRoots(config.repositoryRoots);
  renderRoutes(config.agentRoutes);
  renderSecretState("#runner-token-state", data.secretsConfigured.runner_token);
  renderSecretState("#openai-key-state", data.secretsConfigured.openai_api_key);
  renderSecretState("#deepseek-key-state", data.secretsConfigured.deepseek_api_key);
  renderSecretState("#doubao-key-state", data.secretsConfigured.doubao_api_key);
  renderSecretState("#qwen-key-state", data.secretsConfigured.qwen_api_key);
}

function collectConfig() {
  const routes = {};
  document.querySelectorAll(".route-row").forEach((row) => {
    const model = row.querySelector("input").value.trim();
    if (model) routes[row.dataset.role] = { provider: row.querySelector("select").value, model };
  });
  return {
    serverUrl: $("#server-url").value,
    runnerId: $("#runner-id").value,
    runnerName: $("#runner-name").value,
    repositoryRoots: [...document.querySelectorAll(".root-row input")].map((input) => input.value.trim()).filter(Boolean),
    runnerCommand: $("#runner-command").value,
    worktreeRoot: $("#worktree-root").value,
    provider: $("#provider").value,
    model: $("#model").value,
    mockLlm: $("#mock-llm").checked,
    agentRoutes: routes,
  };
}

function collectSecrets() {
  return {
    runnerToken: $("#runner-token").value || null,
    openaiApiKey: $("#openai-key").value || null,
    deepseekApiKey: $("#deepseek-key").value || null,
    doubaoApiKey: $("#doubao-key").value || null,
    qwenApiKey: $("#qwen-key").value || null,
  };
}

async function saveSettings(event) {
  event?.preventDefault();
  if (!$("#settings-form").reportValidity()) return false;
  const button = $("#save-settings");
  button.disabled = true;
  try {
    const data = await invoke("save_desktop_config", { config: collectConfig(), secrets: collectSecrets() });
    ["#runner-token", "#openai-key", "#deepseek-key", "#doubao-key", "#qwen-key"].forEach((selector) => { $(selector).value = ""; });
    populate(data);
    $("#save-state").textContent = "配置已保存";
    toast("配置已保存");
    return true;
  } catch (error) {
    $("#save-state").textContent = String(error);
    toast(error);
    return false;
  } finally { button.disabled = false; }
}

async function probeServer() {
  const button = $("#probe-server");
  button.disabled = true;
  try {
    await saveSettings();
    const result = await invoke("probe_server");
    $("#server-dot").classList.add("online");
    $("#server-label").textContent = result.status === "ready" ? "服务已就绪" : result.status;
    toast("Server、数据库、Worker 和沙箱已就绪");
  } catch (error) {
    $("#server-dot").classList.remove("online");
    $("#server-label").textContent = "连接失败";
    toast(error);
  } finally { button.disabled = false; }
}

async function refreshStatus() {
  try {
    const status = await invoke("runner_status");
    $("#runner-state").textContent = status.running ? "运行中" : "已停止";
    $("#runner-state").classList.toggle("running", status.running);
    $("#runner-pid").textContent = status.pid ? `PID ${status.pid}` : status.lastExit || "";
    $("#start-runner").classList.toggle("hidden", status.running);
    $("#stop-runner").classList.toggle("hidden", !status.running);
    $("#log-path").textContent = status.logPath;
  } catch (error) { toast(error); }
}

async function startRunner() {
  if (!await saveSettings()) return;
  const button = $("#start-runner");
  button.disabled = true;
  try { await invoke("start_runner"); await refreshStatus(); await refreshLog(); toast("Local Runner 已启动"); }
  catch (error) { toast(error); }
  finally { button.disabled = false; }
}

async function stopRunner() {
  const button = $("#stop-runner");
  button.disabled = true;
  try { await invoke("stop_runner"); await refreshStatus(); await refreshLog(); toast("Local Runner 已停止"); }
  catch (error) { toast(error); }
  finally { button.disabled = false; }
}

async function refreshLog() {
  try { $("#runner-log").textContent = await invoke("read_runner_log") || "暂无运行输出"; }
  catch (error) { toast(error); }
}

function toast(message) {
  const element = $("#toast");
  element.textContent = String(message);
  element.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.add("hidden"), 4200);
}

function escapeAttribute(value = "") {
  return String(value).replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
}

document.addEventListener("DOMContentLoaded", async () => {
  $("#browse-root").addEventListener("click", async () => {
    try {
      const selected = await invoke("pick_repository_root");
      if (!selected) return;
      const existing = [...document.querySelectorAll(".root-row input")].map((input) => input.value).filter(Boolean);
      if (!existing.includes(selected)) renderRoots([...existing, selected]);
    } catch (error) { toast(error); }
  });
  $("#add-root").addEventListener("click", () => {
    const existing = [...document.querySelectorAll(".root-row input")].map((input) => input.value);
    renderRoots([...existing, ""]);
    document.querySelector(".root-row:last-child input").focus();
  });
  $("#settings-form").addEventListener("submit", saveSettings);
  $("#probe-server").addEventListener("click", probeServer);
  $("#start-runner").addEventListener("click", startRunner);
  $("#stop-runner").addEventListener("click", stopRunner);
  $("#refresh-log").addEventListener("click", refreshLog);
  $("#open-dashboard").addEventListener("click", async () => {
    try { if (await saveSettings()) await invoke("open_dashboard"); } catch (error) { toast(error); }
  });
  try { populate(await invoke("load_desktop_config")); await refreshStatus(); await refreshLog(); }
  catch (error) { toast(error); }
  statusTimer = setInterval(() => { refreshStatus(); refreshLog(); }, 3000);
});

window.addEventListener("beforeunload", () => clearInterval(statusTimer));
