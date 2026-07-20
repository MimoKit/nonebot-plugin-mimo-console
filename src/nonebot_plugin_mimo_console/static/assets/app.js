"use strict";

const rootPath = location.pathname.replace(/\/$/, "");
const storageKey = "mimo-console-token";
const state = {
  token: localStorage.getItem(storageKey) || "",
  username: "",
  configured: true,
  page: "dashboard",
  dashboard: null,
  plugins: [],
  pluginTab: "installed",
  storePlugins: [],
  storePage: 1,
  storePages: 1,
  storeTotal: 0,
  packageManagement: true,
  storeSearchTimer: null,
  configItems: [],
  configOriginal: new Map(),
  configChanges: new Map(),
  logs: [],
  logAfter: 0,
  logFollow: true,
  timers: [],
  detailPlugin: null,
  detailSource: null,
};

const $ = (selector, context = document) => context.querySelector(selector);
const $$ = (selector, context = document) => [...context.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

function safeUrl(value) {
  try {
    const url = new URL(String(value || ""));
    return ["http:", "https:"].includes(url.protocol) ? escapeHtml(url.href) : "";
  } catch (_) {
    return "";
  }
}

function safeImageUrl(value) {
  const text = String(value || "").trim();
  if (/^data:image\/(?:gif|jpeg|png|webp);base64,[a-z0-9+/=]+$/i.test(text)) {
    return escapeHtml(text);
  }
  return safeUrl(text);
}

function pluginAvatarHtml(item, large = false) {
  const label = item.title || item.name || item.module_name || "P";
  const letter = String(label).slice(0, 1).toUpperCase();
  const icon = safeImageUrl(item.icon);
  return `<div class="plugin-avatar${large ? " lg" : ""}">
    <span aria-hidden="true">${escapeHtml(letter)}</span>
    ${icon ? `<img class="plugin-avatar-image" src="${icon}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ""}
  </div>`;
}

function bindAvatarFallbacks(context = document) {
  $$(".plugin-avatar-image:not([data-avatar-bound])", context).forEach((image) => {
    image.dataset.avatarBound = "true";
    image.addEventListener("error", () => image.remove(), { once: true });
  });
}

function setDetailAvatar(item) {
  const avatar = $("#detail-avatar");
  const label = item.title || item.name || item.module_name || "P";
  const letter = String(label).slice(0, 1).toUpperCase();
  const icon = safeImageUrl(item.icon);
  avatar.innerHTML = `<span aria-hidden="true">${escapeHtml(letter)}</span>${icon
    ? `<img class="plugin-avatar-image" src="${icon}" alt="" referrerpolicy="no-referrer">`
    : ""}`;
  bindAvatarFallbacks(avatar);
}

function toast(message, type = "success") {
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.textContent = message;
  $("#toast-stack").append(item);
  setTimeout(() => item.remove(), 3800);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (options.body && !(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(`${rootPath}/api${path}`, { ...options, headers });
  let data = {};
  try { data = await response.json(); } catch (_) { data = {}; }
  if (response.status === 401 && !path.startsWith("/auth/login")) {
    signOut(false);
    throw new Error(data.detail || "登录已失效");
  }
  if (!response.ok) throw new Error(data.detail || `请求失败（${response.status}）`);
  return data;
}

function setAuthMode(configured) {
  state.configured = configured;
  $("#setup-token-field").classList.toggle("hidden", configured);
  $("#password-hint").classList.toggle("hidden", configured);
  $("#auth-step-label").textContent = configured ? "登录" : "首次设置";
  $("#auth-title").textContent = configured ? "欢迎回来" : "创建管理员";
  $("#auth-description").textContent = configured
    ? "使用管理员账号继续"
    : "输入启动日志中的初始化令牌";
  $("#auth-submit span").textContent = configured ? "进入控制台" : "完成初始化";
  $("#password").autocomplete = configured ? "current-password" : "new-password";
}

async function bootstrap() {
  bindEvents();
  updateClock();
  setInterval(updateClock, 1000);
  try {
    const authStatus = await api("/auth/status");
    setAuthMode(authStatus.configured);
    if (state.token && authStatus.configured) {
      try {
        const me = await api("/auth/me");
        enterApp(me.username);
        return;
      } catch (_) { /* stay on auth */ }
    }
  } catch (error) {
    $("#auth-error").textContent = `无法连接控制台：${error.message}`;
  }
  $("#auth-screen").classList.remove("hidden");
}

function bindEvents() {
  $("#auth-form").addEventListener("submit", handleAuth);
  $("#toggle-password").addEventListener("click", () => {
    const input = $("#password");
    input.type = input.type === "password" ? "text" : "password";
  });
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.page)));
  $$("[data-goto]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.goto)));
  $("#logout-button").addEventListener("click", () => signOut(true));
  $("#refresh-button").addEventListener("click", refreshCurrent);
  $("#menu-button").addEventListener("click", () => toggleSidebar(true));
  $("#sidebar-close").addEventListener("click", () => toggleSidebar(false));
  $("#sidebar-backdrop").addEventListener("click", () => toggleSidebar(false));
  $("#plugin-search").addEventListener("input", onPluginSearch);
  $$(".tab[data-plugin-tab]").forEach((button) => button.addEventListener("click", () => switchPluginTab(button.dataset.pluginTab)));
  $("#official-only").addEventListener("change", () => { state.storePage = 1; loadStorePlugins(); });
  $("#store-prev").addEventListener("click", () => changeStorePage(-1));
  $("#store-next").addEventListener("click", () => changeStorePage(1));
  $("#config-search").addEventListener("input", renderConfig);
  $("#log-search").addEventListener("input", renderLogs);
  $("#log-level").addEventListener("change", renderLogs);
  $("#log-follow").addEventListener("click", () => {
    state.logFollow = !state.logFollow;
    $("#log-follow").classList.toggle("active", state.logFollow);
  });
  $("#clear-logs").addEventListener("click", clearLogs);
  $$(".view-toggle button").forEach((button) => button.addEventListener("click", () => {
    $$(".view-toggle button").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    $("#plugin-grid").classList.toggle("list", button.dataset.view === "list");
  }));
  $("#save-config").addEventListener("click", saveConfig);
  $("#discard-config").addEventListener("click", discardConfig);
  $("#add-config").addEventListener("click", () => showModal(true));
  $("#modal-close").addEventListener("click", () => showModal(false));
  $("#modal-cancel").addEventListener("click", () => showModal(false));
  $("#modal-confirm").addEventListener("click", addConfig);
  $("#modal").addEventListener("click", (event) => { if (event.target.id === "modal") showModal(false); });
  $("#detail-close").addEventListener("click", closeDetail);
  $("#detail-backdrop").addEventListener("click", closeDetail);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (!$("#detail-drawer").classList.contains("hidden")) closeDetail();
      else if (!$("#modal").classList.contains("hidden")) showModal(false);
    }
  });
}

async function handleAuth(event) {
  event.preventDefault();
  const submit = $("#auth-submit");
  const error = $("#auth-error");
  submit.disabled = true;
  error.textContent = "";
  try {
    const body = {
      username: $("#username").value.trim(),
      password: $("#password").value,
    };
    const endpoint = state.configured ? "/auth/login" : "/auth/setup";
    if (!state.configured) body.setup_token = $("#setup-token").value.trim();
    const result = await api(endpoint, { method: "POST", body: JSON.stringify(body) });
    state.token = result.token;
    localStorage.setItem(storageKey, state.token);
    enterApp(result.username);
  } catch (err) {
    error.textContent = err.message;
  } finally {
    submit.disabled = false;
  }
}

function enterApp(username) {
  state.username = username;
  $("#auth-screen").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#profile-name").textContent = username;
  $("#welcome-name").textContent = username;
  $("#avatar").textContent = username.slice(0, 1).toUpperCase();
  clearTimers();
  state.timers.push(setInterval(() => loadDashboard(false), 5000));
  state.timers.push(setInterval(loadLogs, 2000));
  Promise.allSettled([loadDashboard(), loadPlugins(), loadLogs()]);
}

async function signOut(requestLogout) {
  if (requestLogout && state.token) {
    try { await api("/auth/logout", { method: "POST" }); } catch (_) { /* local logout */ }
  }
  clearTimers();
  closeDetail();
  state.token = "";
  localStorage.removeItem(storageKey);
  $("#app").classList.add("hidden");
  $("#auth-screen").classList.remove("hidden");
  $("#password").value = "";
  setAuthMode(true);
}

function clearTimers() {
  state.timers.forEach(clearInterval);
  state.timers = [];
}

const pages = { dashboard: "概览", plugins: "插件", config: "配置", logs: "日志" };
async function navigate(page) {
  if (!pages[page]) return;
  state.page = page;
  $$(".page").forEach((item) => item.classList.toggle("active", item.id === `page-${page}`));
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.page === page));
  $("#page-crumb").textContent = pages[page];
  toggleSidebar(false);
  if (page === "plugins") await refreshPluginsPage();
  if (page === "config") await loadConfig();
  if (page === "logs") { await loadLogs(); scrollLogs(); }
}

function toggleSidebar(show) {
  $("#sidebar").classList.toggle("open", show);
  $("#sidebar-backdrop").classList.toggle("show", show);
}

function updateClock() {
  const now = new Date();
  $("#clock").textContent = now.toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
  const welcomeDate = $("#welcome-date");
  if (welcomeDate) {
    welcomeDate.textContent = now.toLocaleDateString("zh-CN", { month: "long", day: "numeric", weekday: "long" });
  }
  const hour = now.getHours();
  $("#greeting").textContent = hour < 6 ? "夜深了" : hour < 11 ? "早上好" : hour < 14 ? "中午好" : hour < 18 ? "下午好" : "晚上好";
}

async function refreshCurrent() {
  const button = $("#refresh-button");
  button.classList.add("loading");
  try {
    if (state.page === "dashboard") await loadDashboard();
    if (state.page === "plugins") await refreshPluginsPage();
    if (state.page === "config") await loadConfig();
    if (state.page === "logs") await loadLogs();
    toast("已刷新");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setTimeout(() => button.classList.remove("loading"), 350);
  }
}

function formatBytes(value) {
  const number = Number(value || 0);
  if (!number) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  const index = Math.min(Math.floor(Math.log(number) / Math.log(1024)), units.length - 1);
  return `${(number / 1024 ** index).toFixed(index > 2 ? 1 : 2)} ${units[index]}`;
}

function formatUptime(seconds) {
  const value = Number(seconds || 0);
  const days = Math.floor(value / 86400);
  const hours = Math.floor((value % 86400) / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  return days ? `${days} 天 ${hours} 小时` : hours ? `${hours} 小时 ${minutes} 分钟` : `${minutes} 分钟`;
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

function setRing(id, value) {
  const safe = Math.max(0, Math.min(100, Number(value || 0)));
  $(id).style.setProperty("--value", safe.toFixed(1));
}

function setBar(id, value) {
  const safe = Math.max(0, Math.min(100, Number(value || 0)));
  $(id).style.width = `${safe}%`;
}

async function loadDashboard(showErrors = true) {
  try {
    const data = await api("/dashboard");
    state.dashboard = data;
    const system = data.system;
    const cpu = Number(system.cpu_percent || 0).toFixed(1);
    const memory = Number(system.memory_percent || 0).toFixed(1);
    const disk = Number(system.disk_percent || 0).toFixed(1);
    $("#cpu-value").textContent = `${cpu}%`;
    $("#cpu-sub").textContent = `${system.cpu_count} 个逻辑核心`;
    $("#memory-value").textContent = `${memory}%`;
    $("#memory-sub").textContent = `${formatBytes(system.memory_used)} / ${formatBytes(system.memory_total)}`;
    $("#disk-value").textContent = `${disk}%`;
    $("#disk-sub").textContent = `${formatBytes(system.disk_used)} / ${formatBytes(system.disk_total)}`;
    $("#bot-value").textContent = data.counts.bots;
    $("#bot-sub").textContent = `${new Set(data.bots.map((bot) => bot.adapter)).size} 个适配器`;
    setBar("#cpu-bar", cpu);
    setBar("#memory-bar", memory);
    setBar("#disk-bar", disk);
    setBar("#bot-bar", Math.min(100, data.counts.bots * 20));
    $("#cpu-ring-value").textContent = `${cpu}%`;
    $("#memory-ring-value").textContent = `${memory}%`;
    $("#disk-ring-value").textContent = `${disk}%`;
    setRing("#cpu-ring", cpu);
    setRing("#memory-ring", memory);
    setRing("#disk-ring", disk);
    $("#cpu-ring-sub").textContent = `${system.cpu_count} 核心`;
    $("#process-memory").textContent = `进程 ${formatBytes(system.process_memory)}`;
    $("#disk-free").textContent = `剩余 ${formatBytes(system.disk_total - system.disk_used)}`;
    $("#network-sent").textContent = formatBytes(system.network_sent);
    $("#network-recv").textContent = formatBytes(system.network_recv);
    $("#hostname").textContent = system.hostname;
    $("#nonebot-version").textContent = `v${system.nonebot}`;
    $("#python-version").textContent = `v${system.python}`;
    $("#platform-value").textContent = system.platform;
    $("#plugin-count").textContent = data.counts.plugins;
    $("#matcher-count").textContent = data.counts.matchers;
    $("#nav-plugin-count").textContent = data.counts.plugins;
    $("#sidebar-uptime").textContent = `已运行 ${formatUptime(system.uptime)}`;
    $("#bot-list").innerHTML = data.bots.length
      ? data.bots.map((bot) => `<div class="bot-chip"><strong>${escapeHtml(bot.id)}</strong><span>${escapeHtml(bot.adapter)}</span></div>`).join("")
      : '<div class="empty-mini">暂无机器人连接</div>';
  } catch (error) {
    if (showErrors) toast(error.message, "error");
  }
}

async function loadPlugins() {
  try {
    const data = await api("/plugins");
    state.plugins = data.items || [];
    $("#plugin-total").textContent = state.plugins.length;
    $("#nav-plugin-count").textContent = state.plugins.length;
    $("#loaded-tab-count").textContent = state.plugins.length;
    if (state.pluginTab === "installed") renderPlugins();
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderPlugins() {
  const query = $("#plugin-search").value.trim().toLowerCase();
  const items = state.plugins.filter((item) =>
    [item.title, item.name, item.module, item.description].join(" ").toLowerCase().includes(query),
  );
  $("#plugin-result-meta").textContent = query
    ? `找到 ${items.length} 个已加载插件`
    : `当前进程已加载 ${items.length} 个插件 · 点击卡片查看详情`;
  $("#plugin-grid").innerHTML = items.length
    ? items.map((item, index) => loadedPluginHtml(item, index)).join("")
    : '<div class="empty-state">没有找到匹配的插件</div>';
  bindAvatarFallbacks($("#plugin-grid"));
  bindPluginCardEvents();
}

function loadedPluginHtml(item, index) {
  return `<article class="plugin-card" data-detail-source="loaded" data-detail-index="${index}" tabindex="0" role="button">
    <div class="plugin-card-head">
      ${pluginAvatarHtml(item)}
      <div class="plugin-title">
        <h3>${escapeHtml(item.title)}</h3>
        <div class="module">${escapeHtml(item.module)}</div>
      </div>
      <span class="badge loaded">运行中</span>
    </div>
    <p class="desc">${escapeHtml(item.description)}</p>
    <div class="plugin-meta">
      <span>${escapeHtml(item.type)}</span>
      <span>${item.matchers} 个响应器</span>
      <span class="detail-hint">详情 →</span>
    </div>
  </article>`;
}

function onPluginSearch() {
  if (state.pluginTab === "installed") {
    renderPlugins();
    return;
  }
  clearTimeout(state.storeSearchTimer);
  state.storeSearchTimer = setTimeout(() => {
    state.storePage = 1;
    loadStorePlugins();
  }, 320);
}

async function switchPluginTab(tabName) {
  if (!["installed", "store"].includes(tabName) || state.pluginTab === tabName) return;
  state.pluginTab = tabName;
  $$(".tab[data-plugin-tab]").forEach((button) => {
    const active = button.dataset.pluginTab === tabName;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  $("#plugin-search").value = "";
  $("#plugin-search").placeholder = tabName === "store" ? "搜索插件、作者、标签或包名" : "搜索已加载插件";
  $("#official-filter").classList.toggle("hidden", tabName !== "store");
  $("#store-pagination").classList.toggle("hidden", tabName !== "store");
  if (tabName === "store") await loadStorePlugins();
  else renderPlugins();
}

async function refreshPluginsPage() {
  await loadPlugins();
  if (state.pluginTab === "store") await loadStorePlugins();
}

async function loadStorePlugins() {
  const grid = $("#plugin-grid");
  grid.innerHTML = '<div class="store-loading"><i></i><span>正在加载插件商店…</span></div>';
  $("#plugin-result-meta").textContent = "";
  const params = new URLSearchParams({
    query: $("#plugin-search").value.trim(),
    page: String(state.storePage),
    page_size: "18",
    official_only: String($("#official-only").checked),
  });
  try {
    const data = await api(`/store/plugins?${params}`);
    state.storePlugins = data.items || [];
    state.storePage = data.page || 1;
    state.storePages = data.pages || 1;
    state.storeTotal = data.total || 0;
    state.packageManagement = data.package_management !== false;
    $("#store-tab-count").textContent = state.storeTotal;
    renderStorePlugins();
  } catch (error) {
    grid.innerHTML = `<div class="empty-state store-error"><strong>商店连接失败</strong><span>${escapeHtml(error.message)}</span><br><a href="https://nonebot.dev/store/plugins" target="_blank" rel="noreferrer">打开官方商店 ↗</a></div>`;
    toast(error.message, "error");
  }
}

function renderStorePlugins() {
  const query = $("#plugin-search").value.trim();
  $("#plugin-result-meta").textContent = query
    ? `在官方商店找到 ${state.storeTotal} 个结果 · 点击卡片查看详情`
    : `NoneBot 官方商店 · ${state.storeTotal} 个可用插件 · 点击卡片查看详情`;
  $("#store-page-label").textContent = `第 ${state.storePage} / ${state.storePages} 页`;
  $("#store-prev").disabled = state.storePage <= 1;
  $("#store-next").disabled = state.storePage >= state.storePages;
  $("#store-pagination").classList.toggle("hidden", state.storePages <= 1);
  $("#plugin-grid").innerHTML = state.storePlugins.length
    ? state.storePlugins.map((item, index) => storePluginHtml(item, index)).join("")
    : '<div class="empty-state">没有找到符合条件的插件</div>';
  bindAvatarFallbacks($("#plugin-grid"));
  bindPluginCardEvents();
}

function tagLabels(item) {
  if (Array.isArray(item.tags) && item.tags.length) {
    if (typeof item.tags[0] === "string") return item.tags;
    return item.tags.map((tag) => tag.label || tag).filter(Boolean);
  }
  return item.tag_labels || [];
}

function storePluginHtml(item, index) {
  const tags = tagLabels(item).slice(0, 4);
  const tagHtml = tags.length
    ? tags.map((tag) => `<span class="store-tag">${escapeHtml(tag)}</span>`).join("")
    : '<span class="store-tag muted">暂无标签</span>';
  const official = item.official ? '<span class="badge official">官方</span>' : "";
  const installed = item.installed ? '<span class="badge installed">已安装</span>' : "";
  const version = item.installed
    ? `已安装 ${escapeHtml(item.installed_version || "")}`
    : `最新 ${escapeHtml(item.version || "未知")}`;
  return `<article class="plugin-card store-card" data-detail-source="store" data-detail-index="${index}" tabindex="0" role="button">
    <div class="plugin-card-head">
      ${pluginAvatarHtml(item)}
      <div class="plugin-title">
        <h3>${escapeHtml(item.name)}</h3>
        <div class="module">${escapeHtml(item.project_link)}</div>
      </div>
      ${official}${installed}
    </div>
    <p class="desc">${escapeHtml(item.description)}</p>
    <div class="store-tags">${tagHtml}</div>
    <div class="store-card-footer">
      <div>
        <strong>${escapeHtml(item.author)}</strong>
        <span>${version}</span>
      </div>
      <span class="detail-hint">详情 →</span>
    </div>
  </article>`;
}

function bindPluginCardEvents() {
  $$(".plugin-card[data-detail-source]").forEach((card) => {
    const open = () => {
      const source = card.dataset.detailSource;
      const index = Number(card.dataset.detailIndex);
      if (source === "loaded") openLoadedDetail(state.plugins[index]);
      else openStoreDetail(state.storePlugins[index]);
    };
    card.addEventListener("click", (event) => {
      if (event.target.closest("button, a")) return;
      open();
    });
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    });
  });
}

function openLoadedDetail(item) {
  if (!item) return;
  state.detailSource = "loaded";
  state.detailPlugin = item;
  setDetailAvatar(item);
  $("#detail-title").textContent = item.title || item.name;
  $("#detail-module").textContent = item.module || "";
  $("#detail-badges").innerHTML = '<span class="badge loaded">运行中</span>';
  const homepage = safeUrl(item.homepage);
  $("#detail-body").innerHTML = `
    <div class="detail-section">
      <h3>简介</h3>
      <p class="detail-desc">${escapeHtml(item.description || "暂无插件介绍")}</p>
    </div>
    ${item.usage ? `<div class="detail-section"><h3>用法</h3><p class="detail-desc mono">${escapeHtml(item.usage)}</p></div>` : ""}
    <div class="detail-section">
      <h3>基本信息</h3>
      <div class="detail-grid">
        <div class="detail-cell"><span>模块名</span><strong class="mono">${escapeHtml(item.module)}</strong></div>
        <div class="detail-cell"><span>类型</span><strong>${escapeHtml(item.type || "plugin")}</strong></div>
        <div class="detail-cell"><span>响应器</span><strong>${escapeHtml(item.matchers ?? 0)}</strong></div>
        <div class="detail-cell"><span>插件名</span><strong>${escapeHtml(item.name)}</strong></div>
      </div>
    </div>
    ${item.path ? `<div class="detail-section"><h3>路径</h3><p class="detail-desc mono">${escapeHtml(item.path)}</p></div>` : ""}
    ${homepage ? `<div class="detail-section"><h3>链接</h3><div class="detail-links"><a href="${homepage}" target="_blank" rel="noreferrer"><span>主页 / 文档</span><span>↗</span></a></div></div>` : ""}
  `;
  $("#detail-actions").innerHTML = homepage
    ? `<a class="btn btn-primary" href="${homepage}" target="_blank" rel="noreferrer">打开主页</a>`
    : `<button class="btn btn-ghost" type="button" disabled>无主页</button>`;
  showDetail(true);
}

async function openStoreDetail(item) {
  if (!item) return;
  state.detailSource = "store";
  state.detailPlugin = item;
  setDetailAvatar(item);
  $("#detail-title").textContent = item.name || item.module_name;
  $("#detail-module").textContent = item.module_name || item.project_link || "";
  $("#detail-badges").innerHTML = [
    item.official ? '<span class="badge official">官方</span>' : "",
    item.installed ? '<span class="badge installed">已安装</span>' : "",
  ].join("");
  $("#detail-body").innerHTML = '<div class="empty-state compact">正在加载详情…</div>';
  $("#detail-actions").innerHTML = "";
  showDetail(true);

  let detail = item;
  try {
    const data = await api(`/store/plugins/${encodeURIComponent(item.module_name)}`);
    detail = data.item || item;
    if (data.package_management !== undefined) state.packageManagement = data.package_management;
  } catch (_) {
    /* use list data */
  }
  state.detailPlugin = detail;
  renderStoreDetail(detail);
}

function renderStoreDetail(item) {
  const homepage = safeUrl(item.homepage);
  const storeUrl = safeUrl(item.store_url) || safeUrl(`https://nonebot.dev/store/plugins?q=${item.project_link || ""}`);
  const tags = tagLabels(item);
  const adapters = item.supported_adapters || [];
  const tagHtml = tags.length
    ? tags.map((tag, i) => {
        const color = Array.isArray(item.tags) && item.tags[i] && item.tags[i].color
          ? item.tags[i].color
          : "";
        const style = color ? `style="--tag-c:${escapeHtml(color)};background:color-mix(in srgb, ${escapeHtml(color)} 18%, transparent);color:${escapeHtml(color)}"` : "";
        return `<span class="detail-tag" ${style}>${escapeHtml(tag)}</span>`;
      }).join("")
    : '<span class="store-tag muted">暂无标签</span>';
  const adapterHtml = adapters.length
    ? adapters.map((a) => `<span>${escapeHtml(a)}</span>`).join("")
    : "<span>全部适配器 / 未声明</span>";

  setDetailAvatar(item);
  $("#detail-title").textContent = item.name || item.module_name;
  $("#detail-module").textContent = item.module_name || "";
  $("#detail-badges").innerHTML = [
    item.official ? '<span class="badge official">官方</span>' : "",
    item.installed ? '<span class="badge installed">已安装</span>' : "",
    item.valid === false ? '<span class="badge" style="color:var(--yellow);background:rgba(232,197,106,.12)">未通过检查</span>' : "",
  ].join("");

  $("#detail-body").innerHTML = `
    <div class="detail-section">
      <h3>简介</h3>
      <p class="detail-desc">${escapeHtml(item.description || "暂无插件介绍")}</p>
    </div>
    <div class="detail-section">
      <h3>基本信息</h3>
      <div class="detail-grid">
        <div class="detail-cell"><span>作者</span><strong>${escapeHtml(item.author || "unknown")}</strong></div>
        <div class="detail-cell"><span>类型</span><strong>${escapeHtml(item.type || "application")}</strong></div>
        <div class="detail-cell"><span>最新版本</span><strong>${escapeHtml(item.version || "未知")}</strong></div>
        <div class="detail-cell"><span>已装版本</span><strong>${escapeHtml(item.installed_version || "未安装")}</strong></div>
        <div class="detail-cell"><span>包名</span><strong class="mono">${escapeHtml(item.project_link || "")}</strong></div>
        <div class="detail-cell"><span>更新时间</span><strong>${escapeHtml(formatDate(item.updated_at))}</strong></div>
      </div>
    </div>
    <div class="detail-section">
      <h3>标签</h3>
      <div class="detail-tags">${tagHtml}</div>
    </div>
    <div class="detail-section">
      <h3>支持适配器</h3>
      <div class="detail-adapters">${adapterHtml}</div>
    </div>
    <div class="detail-section">
      <h3>链接</h3>
      <div class="detail-links">
        ${homepage ? `<a href="${homepage}" target="_blank" rel="noreferrer"><span>项目主页</span><span>↗</span></a>` : ""}
        ${storeUrl ? `<a href="${storeUrl}" target="_blank" rel="noreferrer"><span>NoneBot 商店</span><span>↗</span></a>` : ""}
        <a href="https://pypi.org/project/${encodeURIComponent(item.project_link || "")}/" target="_blank" rel="noreferrer"><span>PyPI</span><span>↗</span></a>
      </div>
    </div>
  `;

  const moduleName = item.module_name;
  const name = item.name || moduleName;
  let actions = "";
  if (!state.packageManagement) {
    actions = '<button class="btn btn-ghost" type="button" disabled>安装已关闭</button>';
  } else if (item.installed) {
    actions = `
      <button class="btn btn-primary" type="button" data-plugin-action="update" data-plugin-module="${escapeHtml(moduleName)}" data-plugin-name="${escapeHtml(name)}">更新</button>
      <button class="btn btn-danger" type="button" data-plugin-action="uninstall" data-plugin-module="${escapeHtml(moduleName)}" data-plugin-name="${escapeHtml(name)}">卸载</button>
    `;
  } else {
    actions = `<button class="btn btn-primary" type="button" data-plugin-action="install" data-plugin-module="${escapeHtml(moduleName)}" data-plugin-name="${escapeHtml(name)}">安装插件</button>`;
  }
  if (homepage) {
    actions += `<a class="btn btn-ghost" href="${homepage}" target="_blank" rel="noreferrer">主页</a>`;
  }
  $("#detail-actions").innerHTML = actions;
  $$("[data-plugin-action]", $("#detail-actions")).forEach((button) => {
    button.addEventListener("click", () => manageStorePlugin(button));
  });
}

function showDetail(show) {
  $("#detail-drawer").classList.toggle("hidden", !show);
  document.body.style.overflow = show ? "hidden" : "";
}

function closeDetail() {
  showDetail(false);
  state.detailPlugin = null;
  state.detailSource = null;
}

async function manageStorePlugin(button) {
  const action = button.dataset.pluginAction;
  const moduleName = button.dataset.pluginModule;
  const pluginName = button.dataset.pluginName;
  const labels = { install: "安装", update: "更新", uninstall: "卸载" };
  if (!window.confirm(`确定${labels[action]}「${pluginName}」吗？完成后需要重启 NoneBot。`)) return;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = `${labels[action]}中…`;
  const drawer = $("#detail-drawer");
  drawer.classList.add("busy");
  try {
    await api(`/store/plugins/${encodeURIComponent(moduleName)}/action`, {
      method: "POST",
      body: JSON.stringify({ action }),
    });
    toast(`${pluginName} 已${labels[action]}，重启 NoneBot 后生效`, "warning");
    await loadStorePlugins();
    if (state.detailSource === "store" && state.detailPlugin?.module_name === moduleName) {
      const next = state.storePlugins.find((item) => item.module_name === moduleName);
      if (next) await openStoreDetail(next);
      else closeDetail();
    }
  } catch (error) {
    toast(error.message, "error");
    button.disabled = false;
    button.textContent = original;
  } finally {
    drawer.classList.remove("busy");
  }
}

function changeStorePage(offset) {
  const next = Math.min(state.storePages, Math.max(1, state.storePage + offset));
  if (next === state.storePage) return;
  state.storePage = next;
  loadStorePlugins();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function configGroup(key) {
  if (key.startsWith("MIMO_CONSOLE_")) return "Mimo Console";
  if (["DRIVER", "HOST", "PORT", "ENVIRONMENT", "LOG_LEVEL", "SUPERUSERS", "COMMAND_START", "COMMAND_SEP"].includes(key)) {
    return "NoneBot 核心";
  }
  const namespace = key.match(/^([A-Z][A-Z0-9]*)_/u)?.[1];
  return namespace ? `${namespace} 配置` : "其他配置";
}

async function loadConfig() {
  try {
    const data = await api("/config");
    state.configItems = data.items || [];
    state.configOriginal = new Map(state.configItems.map((item) => [item.key, item.value]));
    state.configChanges.clear();
    $("#config-path").textContent = data.path;
    updateSaveBar();
    renderConfig();
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderConfig() {
  const query = $("#config-search").value.trim().toLowerCase();
  const groups = new Map();
  state.configItems
    .filter((item) => item.key.toLowerCase().includes(query))
    .forEach((item) => {
      const name = configGroup(item.key);
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(item);
    });
  $("#config-groups").innerHTML = groups.size
    ? [...groups].map(([name, items]) =>
      `<section class="config-group"><header class="config-group-head"><h2>${escapeHtml(name)}</h2><span>${items.length} 项配置</span></header>${items.map(configItemHtml).join("")}</section>`,
    ).join("")
    : '<div class="empty-state">没有找到匹配的配置</div>';
  $$(".config-input").forEach((input) => input.addEventListener("input", () => changeConfig(input.dataset.key, input.value)));
  $$(".secret-toggle").forEach((button) => button.addEventListener("click", () => {
    const input = $(`.config-input[data-key="${CSS.escape(button.dataset.key)}"]`);
    input.type = input.type === "password" ? "text" : "password";
    button.textContent = input.type === "password" ? "显示" : "隐藏";
  }));
}

function configItemHtml(item) {
  const value = state.configChanges.has(item.key) ? state.configChanges.get(item.key) : item.value;
  return `<div class="config-item"><div class="config-key"><strong>${escapeHtml(item.key)}</strong><span>${item.secret ? "敏感配置 · 留空可清除" : "环境变量"}</span></div><div class="config-input-wrap"><input class="config-input" data-key="${escapeHtml(item.key)}" type="${item.secret ? "password" : "text"}" value="${escapeHtml(value)}">${item.secret ? `<button class="secret-toggle" data-key="${escapeHtml(item.key)}" type="button">显示</button>` : ""}</div></div>`;
}

function changeConfig(key, value) {
  if (value === state.configOriginal.get(key)) state.configChanges.delete(key);
  else state.configChanges.set(key, value);
  updateSaveBar();
}

function updateSaveBar() {
  $("#save-bar").classList.toggle("show", state.configChanges.size > 0);
}

async function saveConfig() {
  if (!state.configChanges.size) return;
  const button = $("#save-config");
  button.disabled = true;
  try {
    await api("/config", { method: "PUT", body: JSON.stringify({ values: Object.fromEntries(state.configChanges) }) });
    toast("配置已保存，重启 NoneBot 后生效", "warning");
    await loadConfig();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function discardConfig() {
  state.configChanges.clear();
  renderConfig();
  updateSaveBar();
}

function showModal(show) {
  $("#modal").classList.toggle("hidden", !show);
  $("#modal-error").textContent = "";
  if (show) {
    $("#new-config-key").value = "";
    $("#new-config-value").value = "";
    $("#new-config-key").focus();
  }
}

function addConfig() {
  const key = $("#new-config-key").value.trim().toUpperCase();
  const value = $("#new-config-value").value;
  if (!/^[A-Z_][A-Z0-9_]*$/.test(key)) {
    $("#modal-error").textContent = "配置键只能使用大写字母、数字和下划线";
    return;
  }
  if (state.configItems.some((item) => item.key === key)) {
    $("#modal-error").textContent = "这个配置已经存在";
    return;
  }
  state.configItems.push({
    key,
    value: "",
    secret: /TOKEN|SECRET|PASSWORD|COOKIE|API_KEY/.test(key),
  });
  state.configOriginal.set(key, "");
  state.configChanges.set(key, value);
  showModal(false);
  renderConfig();
  updateSaveBar();
}

async function loadLogs() {
  if (!state.token) return;
  try {
    const data = await api(`/logs?after=${state.logAfter}&limit=500`);
    if (data.items?.length) {
      state.logs.push(...data.items);
      if (state.logs.length > 1000) state.logs.splice(0, state.logs.length - 1000);
      state.logAfter = Math.max(state.logAfter, ...data.items.map((item) => item.id));
      renderRecentLogs();
      if (state.page === "logs") renderLogs();
    }
  } catch (_) { /* quiet poll errors */ }
}

function renderRecentLogs() {
  const items = state.logs.slice(-5).reverse();
  $("#recent-logs").innerHTML = items.length
    ? items.map((item) =>
      `<div class="recent-line"><time>${new Date(item.time).toLocaleTimeString("zh-CN", { hour12: false })}</time><span class="level ${escapeHtml(item.level)}">${escapeHtml(item.level)}</span><p>${escapeHtml(item.message)}</p></div>`,
    ).join("")
    : '<div class="empty-state compact">正在等待日志…</div>';
}

function renderLogs() {
  const query = $("#log-search").value.trim().toLowerCase();
  const level = $("#log-level").value;
  const items = state.logs.filter((item) =>
    (level === "ALL" || item.level === level)
    && [item.message, item.module].join(" ").toLowerCase().includes(query),
  );
  $("#log-count").textContent = `${items.length} 条`;
  $("#log-lines").innerHTML = items.length
    ? items.map((item) =>
      `<div class="log-line ${escapeHtml(item.level)}"><time>${new Date(item.time).toLocaleTimeString("zh-CN", { hour12: false })}</time><span class="level ${escapeHtml(item.level)}">${escapeHtml(item.level)}</span><span class="module">${escapeHtml(item.module || "nonebot")}</span><p>${escapeHtml(item.message)}</p></div>`,
    ).join("")
    : '<div class="empty-state">暂时没有日志</div>';
  if (state.logFollow) requestAnimationFrame(scrollLogs);
}

function scrollLogs() {
  const box = $("#log-lines");
  box.scrollTop = box.scrollHeight;
}

async function clearLogs() {
  try {
    await api("/logs", { method: "DELETE" });
    state.logs = [];
    state.logAfter = 0;
    renderLogs();
    renderRecentLogs();
    toast("日志视图已清空");
  } catch (error) {
    toast(error.message, "error");
  }
}

bootstrap();
