const state = {
  games: [],
  current: null,
  tab: "overview",
  originalValues: {},
  rawModified: false,
  expandedGroups: new Set(),
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("gamedeck-theme", theme);
  const toggle = $("#theme-toggle");
  if (toggle) {
    toggle.setAttribute("aria-label", theme === "light" ? "切换深色模式" : "切换浅色模式");
    toggle.title = theme === "light" ? "切换深色模式" : "切换浅色模式";
  }
}

function initializeTheme() {
  const saved = localStorage.getItem("gamedeck-theme");
  applyTheme(saved === "light" || saved === "dark" ? saved : "dark");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function accentRgb(hex) {
  const clean = hex.replace("#", "");
  const n = parseInt(clean, 16);
  return `${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}`;
}

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      ...(method !== "GET" && window.CSRF_TOKEN ? { "X-CSRF-Token": window.CSRF_TOKEN } : {}),
      ...(options.headers || {}),
    },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}

async function boot() {
  state.games = await api("/api/games");
  renderNav();
  selectGame(state.games[0].id);
  bindEvents();
}

function renderNav() {
  $("#game-nav").innerHTML = state.games.map((game) => `
    <button class="game-button ${state.current?.id === game.id ? "active" : ""}" data-game="${game.id}" title="${escapeHtml(game.name)}" aria-label="${escapeHtml(game.name)}">
      <span class="game-glyph">${escapeHtml(game.shortName)}</span>
      <img class="game-icon" src="${escapeHtml(game.iconUrl)}" alt="" width="38" height="38">
      <small>${game.state.running ? "LIVE" : "OFF"}</small>
    </button>
  `).join("");
  $$(".game-icon").forEach((image) => image.addEventListener("error", () => {
    image.hidden = true;
  }, { once: true }));
  $$("[data-game]").forEach((button) => button.addEventListener("click", () => selectGame(button.dataset.game)));
}

function selectGame(id) {
  state.current = state.games.find((game) => game.id === id);
  state.expandedGroups = new Set();
  state.originalValues = Object.fromEntries(state.current.fields.map((field) => [field.key, field.value]));
  document.documentElement.style.setProperty("--accent", state.current.accent);
  document.documentElement.style.setProperty("--accent-rgb", accentRgb(state.current.accent));
  renderNav();
  renderHeader();
  renderOverview();
  renderConfig();
  loadRaw();
}

function renderHeader() {
  const game = state.current;
  $("#game-eyebrow").textContent = game.eyebrow;
  $("#game-title").textContent = game.name;
  const status = $("#status-pill");
  status.classList.toggle("stopped", !game.state.running);
  status.lastElementChild.textContent = game.state.running ? "运行中" : "已停止";
  $("#start-button").style.display = game.state.running ? "none" : "";
  $("#stop-button").style.display = game.state.running ? "" : "none";
}

function findField(key) {
  return state.current.fields.find((field) => field.key === key);
}

function renderOverview() {
  const { state: server, version, latestVersion } = state.current;
  const heroArt = $("#hero-art");
  if (heroArt && state.current.backgroundUrl) {
    heroArt.style.backgroundImage = `url("${state.current.backgroundUrl}")`;
  }
  $("#hero-state").textContent = server.running ? "运行中" : "已停止";
  $("#hero-subtitle").textContent = server.running ? "世界正在等待玩家加入" : "启动服务后即可接受玩家连接";
  $("#pulse-orb").classList.toggle("stopped", !server.running);
  $("#players").textContent = `${server.players} / ${server.capacity}`;
  $("#uptime").textContent = server.uptime;
  $("#port").textContent = findField("PublicPort")?.value ?? findField("server-port")?.value ?? "—";
  $("#current-version").textContent = version;
  const latestVersionLabel = $("#latest-version-label");
  if (latestVersionLabel) {
    latestVersionLabel.textContent = state.current.id === "palworld" ? "Steam 最新构建" : "最新版本";
  }
  $("#latest-version").textContent = latestVersion;
  const latestBuildId = String(latestVersion || "").match(/\d+/)?.[0] || "";
  const buildId = String(server.buildId || "");
  const isLatest = state.current.id === "palworld"
    ? Boolean(buildId && latestBuildId && buildId === latestBuildId)
    : version === latestVersion;
  $("#version-chip").textContent = isLatest ? "已是最新" : "可更新";
  animateMetric($("#cpu-value"), Number(server.cpu), "%", 0);
  animateMetric($("#cpu-label"), Number(server.cpu), "%", 0);
  $("#cpu-ring").style.setProperty("--value", server.cpu);
  animateMetric($("#memory-label"), Number(server.memory), " GB", 1);
  $("#memory-meter").style.width = `${Math.min(100, server.memory / 16 * 100)}%`;
  $("#install-dir").textContent = state.current.installDir;
  $("#config-name").textContent = state.current.configName;
  $("#service-name").textContent = state.current.serviceName;
}

function animateMetric(element, target, suffix = "", decimals = 0) {
  if (!element || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    if (element) element.textContent = `${target.toFixed(decimals)}${suffix}`;
    return;
  }
  const previous = Number(element.dataset.metricValue || 0);
  if (previous === target) {
    element.textContent = `${target.toFixed(decimals)}${suffix}`;
    return;
  }
  const started = performance.now();
  const duration = 750;
  element.dataset.metricValue = target;
  const tick = (now) => {
    const progress = Math.min(1, (now - started) / duration);
    const eased = 1 - Math.pow(1 - progress, 3);
    const value = previous + (target - previous) * eased;
    element.textContent = `${value.toFixed(decimals)}${suffix}`;
    if (progress < 1 && Number(element.dataset.metricValue) === target) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

function fieldHtml(field) {
  const id = `field-${field.key}`;
  const value = field.value ?? "";
  let control = "";
  if (field.type === "select") {
    control = `<select id="${id}" data-key="${escapeHtml(field.key)}">${field.options.map((option) => `<option value="${escapeHtml(option)}" ${option === value ? "selected" : ""}>${escapeHtml(option)}</option>`).join("")}</select>`;
  } else if (field.type === "boolean") {
    control = `<div class="switch-line"><span>${value ? "已开启" : "已关闭"}</span><label class="switch"><input id="${id}" data-key="${escapeHtml(field.key)}" type="checkbox" ${value ? "checked" : ""}><i></i></label></div>`;
  } else if (field.type === "range") {
    control = `<div class="range-line"><input id="${id}" data-key="${escapeHtml(field.key)}" type="range" value="${escapeHtml(value)}" min="${field.min}" max="${field.max}" step="${field.step}"><output>${escapeHtml(value)}</output></div>`;
  } else {
    control = `<input id="${id}" data-key="${escapeHtml(field.key)}" type="${field.type}" value="${escapeHtml(value)}"
      ${field.type === "password" && field.hasValue ? 'placeholder="已设置；留空表示不修改"' : ""}
      ${field.min !== undefined ? `min="${field.min}"` : ""}
      ${field.max !== undefined ? `max="${field.max}"` : ""}
      ${field.step !== undefined ? `step="${field.step}"` : ""}>`;
  }
  return `<div class="field" data-search="${escapeHtml(`${field.label} ${field.key} ${field.description || ""}`.toLowerCase())}">
    <label class="field-label" for="${id}"><span>${escapeHtml(field.label)}</span><code>${escapeHtml(field.key)}</code></label>
    ${control}
    <p class="field-description">${escapeHtml(field.description || "使用游戏服务端的官方默认行为。")}</p>
  </div>`;
}

function renderConfig(query = "") {
  const normalized = query.trim().toLowerCase();
  const visibleFields = state.current.fields.filter((field) =>
    `${field.label} ${field.key} ${field.description || ""}`.toLowerCase().includes(normalized)
  );
  const groups = [...new Set(visibleFields.map((field) => field.group))];
  $("#parameter-count").textContent = normalized
    ? `${visibleFields.length} / ${state.current.fields.length} 个参数`
    : `${state.current.fields.length} 个参数`;
  $("#config-form").innerHTML = groups.length ? groups.map((group) => `
    <article class="config-group card ${normalized || state.expandedGroups.has(group) ? "expanded" : ""}">
      <button class="group-head" type="button" data-group="${escapeHtml(group)}" aria-expanded="${normalized || state.expandedGroups.has(group)}">
        <span class="group-title"><i>⌄</i><h3>${escapeHtml(group)}</h3></span>
        <span>${visibleFields.filter((field) => field.group === group).length} 项</span>
      </button>
      ${normalized || state.expandedGroups.has(group)
        ? `<div class="field-grid">${visibleFields.filter((field) => field.group === group).map(fieldHtml).join("")}</div>`
        : ""}
    </article>
  `).join("") : `<div class="config-empty">没有找到匹配的参数</div>`;
  $$("[data-group]").forEach((button) => button.addEventListener("click", () => {
    const group = button.dataset.group;
    if (state.expandedGroups.has(group)) state.expandedGroups.delete(group);
    else state.expandedGroups.add(group);
    renderConfig($("#config-search").value);
  }));
  $$('.range-line input').forEach((input) => input.addEventListener("input", () => {
    input.nextElementSibling.value = input.value;
    updateDraftValue(input);
  }));
  $$('.switch input').forEach((input) => input.addEventListener("change", () => {
    input.closest(".switch-line").firstElementChild.textContent = input.checked ? "已开启" : "已关闭";
    updateDraftValue(input);
  }));
  $$("[data-key]:not([type=range]):not([type=checkbox])").forEach((input) =>
    input.addEventListener("change", () => updateDraftValue(input))
  );
}

function updateDraftValue(input) {
  const field = state.current.fields.find((item) => item.key === input.dataset.key);
  if (!field) return;
  if (input.type === "checkbox") field.value = input.checked;
  else if (input.type === "number" || input.type === "range") field.value = Number(input.value);
  else field.value = input.value;
}

function collectConfig() {
  $$("[data-key]").forEach(updateDraftValue);
  return Object.fromEntries(state.current.fields.map((field) => [field.key, field.value]));
}

async function saveConfig() {
  try {
    const values = collectConfig();
    await api(`/api/games/${state.current.id}/config/form`, { method: "PUT", body: JSON.stringify(values) });
    Object.assign(state.originalValues, values);
    state.current.fields.forEach((field) => field.value = values[field.key]);
    renderOverview();
    showToast("参数已保存");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function loadRaw() {
  const data = await api(`/api/games/${state.current.id}/config/raw`);
  $("#raw-editor").value = data.content;
  $("#editor-filename").textContent = data.filename;
  state.rawModified = false;
  $("#modified-dot").classList.remove("visible");
  updateLineNumbers();
}

async function saveRaw() {
  try {
    await api(`/api/games/${state.current.id}/config/raw`, {
      method: "PUT",
      body: JSON.stringify({ content: $("#raw-editor").value }),
    });
    state.rawModified = false;
    $("#modified-dot").classList.remove("visible");
    showToast("原始配置已保存，旧文件已备份");
  } catch (error) {
    showToast(error.message, true);
  }
}

function switchTab(tab) {
  state.tab = tab;
  $$(".tab").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
  $$(".page-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `panel-${tab}`));
}

async function startAction(action) {
  const labels = { install: "安装服务端", update: "更新服务端", start: "启动服务", stop: "停止服务", restart: "重启服务" };
  try {
    let payload = {};
    if (state.current.id === "minecraft" && action === "install" && !state.current.state.installed) {
      const accepted = window.confirm(
        "安装 Minecraft Java Server 表示你已阅读并接受 Minecraft EULA（https://aka.ms/MinecraftEULA）。是否继续？"
      );
      if (!accepted) return;
      payload.acceptEula = true;
    }
    const job = await api(`/api/games/${state.current.id}/actions/${action}`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    $("#task-title").textContent = labels[action];
    $("#task-drawer").dataset.status = "running";
    $("#task-drawer").classList.add("visible");
    pollJob(job.id);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function pollJob(id) {
  const job = await api(`/api/jobs/${id}`);
  $("#task-message").textContent = job.message;
  $("#task-drawer").dataset.status = job.status;
  $("#task-progress").style.width = `${job.progress}%`;
  $("#task-logs").textContent = job.logs.join("\n") || "等待任务输出…";
  $("#task-logs").scrollTop = $("#task-logs").scrollHeight;
  if (job.status === "running") {
    setTimeout(() => pollJob(id), 550);
    return;
  }
  await refreshGames();
  showToast(job.status === "done" ? "操作已完成" : job.message, job.status !== "done");
}

async function refreshGames() {
  const id = state.current.id;
  state.games = await api("/api/games");
  state.current = state.games.find((game) => game.id === id);
  renderNav();
  renderHeader();
  renderOverview();
}

async function refreshCurrentStatus() {
  if (!state.current || document.hidden) return;
  try {
    const latest = await api(`/api/games/${state.current.id}/status`);
    Object.assign(state.current.state, latest);
    state.current.version = latest.version;
    state.current.latestVersion = latest.latestVersion;
    renderHeader();
    renderOverview();
    renderNav();
  } catch (_) {
    // Keep the last known status when a transient status probe fails.
  }
}

function updateLineNumbers() {
  const count = $("#raw-editor").value.split("\n").length;
  $("#line-numbers").textContent = Array.from({ length: count }, (_, index) => index + 1).join("\n");
}

let toastTimer;
function showToast(message, error = false) {
  const toast = $("#toast");
  toast.querySelector("span").textContent = error ? "!" : "✓";
  toast.querySelector("p").textContent = message;
  toast.style.borderColor = error ? "rgba(255,119,119,.25)" : "";
  toast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("visible"), 2600);
}

function bindEvents() {
  const themeToggle = $("#theme-toggle");
  if (themeToggle) {
    themeToggle.addEventListener("click", () => {
      applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
    });
  }
  $$(".tab").forEach((button) => button.addEventListener("click", () => switchTab(button.dataset.tab)));
  $$("[data-action]").forEach((button) => button.addEventListener("click", () => startAction(button.dataset.action)));
  $("#start-button").addEventListener("click", () => startAction("start"));
  $("#stop-button").addEventListener("click", () => startAction("stop"));
  $("#restart-button").addEventListener("click", () => startAction("restart"));
  $("#save-config").addEventListener("click", saveConfig);
  $("#reset-config").addEventListener("click", () => {
    state.current.fields.forEach((field) => field.value = state.originalValues[field.key]);
    $("#config-search").value = "";
    renderConfig();
  });
  $("#config-search").addEventListener("input", (event) => renderConfig(event.target.value));
  $("#save-raw").addEventListener("click", saveRaw);
  $("#close-task").addEventListener("click", () => $("#task-drawer").classList.remove("visible"));
  $("#raw-editor").addEventListener("input", () => {
    state.rawModified = true;
    $("#modified-dot").classList.add("visible");
    updateLineNumbers();
  });
  $("#raw-editor").addEventListener("scroll", () => {
    $("#line-numbers").scrollTop = $("#raw-editor").scrollTop;
  });
  $("#raw-editor").addEventListener("keydown", (event) => {
    if (event.key === "Tab") {
      event.preventDefault();
      const editor = event.target;
      const start = editor.selectionStart;
      editor.value = editor.value.slice(0, start) + "  " + editor.value.slice(editor.selectionEnd);
      editor.selectionStart = editor.selectionEnd = start + 2;
      editor.dispatchEvent(new Event("input"));
    }
  });
}

initializeTheme();
boot()
  .then(() => setInterval(refreshCurrentStatus, 10000))
  .catch((error) => showToast(`载入失败：${error.message}`, true));
