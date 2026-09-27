const $ = (selector) => document.querySelector(selector);
let activeId = null;
let config = null;
let settings = null;
let language = "zh";
let lastRunSignature = null;
let selectedWorkdir = null;
let editingMcpName = null;
let pendingFiles = [];
let viewEpoch = 0;
let sending = false;
let gatewayConnected = true;
const runEtags = new Map();
const openStepDetails = new Set();
let appearance = localStorage.getItem("clearact-appearance") || "sun";
let scene = localStorage.getItem("clearact-scene") || "glass";
const clampSetting = (value, min, max, fallback) => Number.isFinite(value) ? Math.max(min, Math.min(max, value)) : fallback;
let themeDepth = clampSetting(Number(localStorage.getItem("clearact-theme-depth") ?? 8), 0, 100, 8);
let glassTint = clampSetting(Number(localStorage.getItem("clearact-glass-tint") ?? 70), 0, 100, 70);
let jellyStrength = clampSetting(Number(localStorage.getItem("clearact-jelly-strength") ?? 100), 25, 200, 100);
const storedJellyMotion = localStorage.getItem("clearact-jelly-motion");
let jellyMotionEnabled = storedJellyMotion === "on" || (storedJellyMotion == null && !window.matchMedia("(prefers-reduced-motion: reduce)").matches);
let cursorTrailEnabled = localStorage.getItem("clearact-cursor-trail") === "on";
let cursorTrailStyle = localStorage.getItem("clearact-cursor-trail-style") || "ribbon";
let customWallpaper = localStorage.getItem("clearact-wallpaper") || "";
let wallpaperUploadSequence = 0;

function renderAppearance() {
  const dark = appearance === "dark";
  const root = document.documentElement;
  root.dataset.theme = dark ? "dark" : "sun";
  root.dataset.scene = scene;
  root.dataset.jelly = jellyMotionEnabled ? "on" : "off";
  root.dataset.trail = cursorTrailEnabled ? cursorTrailStyle : "off";
  root.dataset.wallpaper = customWallpaper ? "custom" : "default";
  root.dataset.glassMode = glassTint < 40 ? "clear" : "tinted";
  root.style.setProperty("--theme-depth", `${themeDepth}%`);
  root.style.setProperty("--theme-text-shift", `${Math.max(0, Math.min(100, (themeDepth - 40) * 1.67))}%`);
  root.style.setProperty("--glass-opacity", `${14 + glassTint * .82}%`);
  root.style.setProperty("--glass-soft-opacity", `${8 + glassTint * .84}%`);
  root.style.setProperty("--glass-button-opacity", `${10 + glassTint * .84}%`);
  root.style.setProperty("--glass-button-text-shift", `${glassTint}%`);
  root.style.setProperty("--glass-edge-opacity", `${88 - glassTint * .58}%`);
  root.style.setProperty("--glass-blur", `${38 - glassTint * .18}px`);
  root.style.setProperty("--jelly-strength", String(jellyStrength / 100));
  root.style.setProperty("--scene-glow-opacity", String(appearance === "dark" ? .13 - themeDepth * .00035 : .23 - themeDepth * .00125));
  const wallpaper = $("#wallpaper-backdrop");
  if (wallpaper) {
    wallpaper.style.setProperty("--wallpaper-image", customWallpaper ? `url("${customWallpaper}")` : "none");
    const wash = scene === "forest" ? dark ? "rgb(11 28 18 / .58)" : "rgb(226 246 226 / .4)" : scene === "cosmos" ? dark ? "rgb(15 12 36 / .57)" : "rgb(241 235 255 / .4)" : dark ? "rgb(10 17 30 / .52)" : "rgb(250 252 255 / .36)";
    wallpaper.style.setProperty("--wallpaper-wash", wash);
  }
  document.querySelectorAll("[data-scene-choice]").forEach((choice) => {
    const selected = choice.dataset.sceneChoice === scene;
    choice.classList.toggle("active", selected);
    choice.setAttribute("aria-pressed", String(selected));
  });
  const trailToggle = $("#cursor-trail-toggle");
  if (trailToggle) trailToggle.checked = cursorTrailEnabled;
  const jellyToggle = $("#jelly-motion-toggle");
  if (jellyToggle) jellyToggle.checked = jellyMotionEnabled;
  const jellyStatus = $("#jelly-motion-status");
  if (jellyStatus) jellyStatus.textContent = jellyMotionEnabled ? "已开启" : "已关闭";
  const tuningControls = {"theme-depth-range": themeDepth, "glass-tint-range": glassTint, "jelly-strength-range": jellyStrength};
  Object.entries(tuningControls).forEach(([id, value]) => { const slider = $("#" + id); if (slider) slider.value = String(Math.round(value)); });
  const tuningValues = {"theme-depth": `${Math.round(themeDepth)}%`, "glass-tint": `${Math.round(glassTint)}%`, "jelly-strength": `${Math.round(jellyStrength)}%`};
  Object.entries(tuningValues).forEach(([name, value]) => { const output = $(`[data-range-value="${name}"]`); if (output) output.value = value; });
  const trailStyle = $("#cursor-trail-style");
  if (trailStyle) { trailStyle.value = cursorTrailStyle; trailStyle.disabled = !cursorTrailEnabled; }
  const wallpaperStatus = $("#wallpaper-status");
  if (wallpaperStatus) wallpaperStatus.textContent = customWallpaper ? "自定义图片（仅保存在本机浏览器）" : "使用主题默认背景";
  const wallpaperReset = $("#wallpaper-reset");
  if (wallpaperReset) wallpaperReset.disabled = !customWallpaper;
  const button = $("#theme-toggle");
  if (!button) return;
  button.querySelector(".theme-icon").textContent = dark ? "☾" : "☼";
  button.querySelector(".theme-label").textContent = dark ? (language === "zh" ? "黑暗模式" : "Dark mode") : (language === "zh" ? "阳光模式" : "Sun mode");
  button.querySelector(".theme-hint").textContent = dark ? (language === "zh" ? "切换至阳光模式" : "Switch to sun mode") : (language === "zh" ? "切换至黑暗模式" : "Switch to dark mode");
  button.setAttribute("aria-label", dark ? (language === "zh" ? "切换至阳光模式" : "Switch to sun mode") : (language === "zh" ? "切换至黑暗模式" : "Switch to dark mode"));
}

function toggleAppearance(event) {
  const changeTheme = () => {
    appearance = appearance === "dark" ? "sun" : "dark";
    localStorage.setItem("clearact-appearance", appearance);
    renderAppearance();
  };
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) { changeTheme(); return; }
  const rect = event.currentTarget.getBoundingClientRect();
  const x = event.detail ? event.clientX : rect.left + rect.width / 2;
  const y = event.detail ? event.clientY : rect.top + rect.height / 2;
  const radius = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
  const wave = document.createElement("div");
  wave.className = "theme-wave";
  wave.setAttribute("aria-hidden", "true");
  wave.style.setProperty("--wave-x", `${x}px`);
  wave.style.setProperty("--wave-y", `${y}px`);
  wave.style.setProperty("--wave-radius", `${radius}px`);
  document.body.appendChild(wave);
  changeTheme();
  wave.addEventListener("animationend", () => wave.remove(), {once:true});
  window.setTimeout(() => wave.remove(), 900);
}

function persistPersonalization(key, value) {
  try {
    if (value == null || value === "") localStorage.removeItem(key);
    else localStorage.setItem(key, value);
    $("#personalization-feedback").textContent = "";
    return true;
  } catch {
    $("#personalization-feedback").textContent = "无法保存此设置，请检查浏览器可用空间。";
    return false;
  }
}

let wallpaperDbPromise;
function openWallpaperDatabase() {
  if (!window.indexedDB) return Promise.reject(new Error("当前浏览器不支持本地图片存储。"));
  if (!wallpaperDbPromise) wallpaperDbPromise = new Promise((resolve, reject) => {
    const request = indexedDB.open("clearact-personalization", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("assets");
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("无法打开本地图片存储。"));
    request.onblocked = () => reject(new Error("本地图片存储正被其他页面占用，请关闭其他 ClearAct 页面后重试。"));
  });
  return wallpaperDbPromise;
}
async function readStoredWallpaper() {
  const db = await openWallpaperDatabase();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction("assets", "readonly");
    const request = transaction.objectStore("assets").get("custom-wallpaper");
    request.onsuccess = () => resolve(request.result || "");
    request.onerror = () => reject(request.error || new Error("读取壁纸失败。"));
  });
}
async function writeStoredWallpaper(data) {
  const db = await openWallpaperDatabase();
  await new Promise((resolve, reject) => {
    const transaction = db.transaction("assets", "readwrite");
    transaction.objectStore("assets").put(data, "custom-wallpaper");
    transaction.oncomplete = resolve;
    transaction.onerror = () => reject(transaction.error || new Error("保存壁纸失败。"));
    transaction.onabort = () => reject(transaction.error || new Error("保存壁纸被中断。"));
  });
  try { localStorage.removeItem("clearact-wallpaper"); } catch { /* IndexedDB already owns the saved image. */ }
}
async function removeStoredWallpaper() {
  const db = await openWallpaperDatabase();
  await new Promise((resolve, reject) => {
    const transaction = db.transaction("assets", "readwrite");
    transaction.objectStore("assets").delete("custom-wallpaper");
    transaction.oncomplete = resolve;
    transaction.onerror = () => reject(transaction.error || new Error("删除壁纸失败。"));
    transaction.onabort = () => reject(transaction.error || new Error("删除壁纸被中断。"));
  });
  try { localStorage.removeItem("clearact-wallpaper"); } catch { /* No legacy key remains. */ }
}
async function loadStoredWallpaper() {
  const loadSequence = wallpaperUploadSequence;
  try {
    const saved = await readStoredWallpaper();
    if (loadSequence !== wallpaperUploadSequence) return;
    if (saved) { customWallpaper = saved; try { localStorage.removeItem("clearact-wallpaper"); } catch { /* IndexedDB already owns the image. */ } }
    else if (customWallpaper) await writeStoredWallpaper(customWallpaper);
  } catch { /* Keep the legacy localStorage copy visible when IndexedDB is unavailable. */ }
  if (loadSequence === wallpaperUploadSequence) renderAppearance();
}

async function setWallpaper(file) {
  const status = $("#personalization-feedback");
  if (!file || !file.type.startsWith("image/")) return;
  const uploadId = ++wallpaperUploadSequence;
  if (file.size > 16 * 1024 * 1024) {
    status.textContent = "图片不能超过 16 MB。";
    return;
  }
  status.textContent = "正在优化壁纸…";
  try {
    const image = await createImageBitmap(file);
    const scale = Math.min(1, 1800 / image.width, 1400 / image.height);
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(image.width * scale));
    canvas.height = Math.max(1, Math.round(image.height * scale));
    const context = canvas.getContext("2d", {alpha:false});
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    image.close();
    let data = canvas.toDataURL("image/webp", .78);
    if (data.length > 4_500_000) data = canvas.toDataURL("image/jpeg", .58);
    if (data.length > 4_500_000) throw new Error("图片压缩后仍太大，请选择更小的图片。");
    if (uploadId !== wallpaperUploadSequence) return;
    await writeStoredWallpaper(data);
    if (uploadId !== wallpaperUploadSequence) return;
    customWallpaper = data;
    renderAppearance();
    status.textContent = "壁纸已应用，仅保存在本机浏览器。";
  } catch (error) {
    status.textContent = error.message || "无法读取这张图片，请换一张试试。";
  }
}

const text = {
  zh: {newTopic:"新建话题",history:"过往话题",settings:"设置",settingsHint:"权限、模型与扩展",guide:"使用指南",guideHint:"第一次使用先看这里",welcomeTitle:"把目标告诉我，剩下的交给 ClearAct",welcomeHint:"你不需要先选择工具。直接描述想完成的事情，我会根据任务自动使用文件、网页和已接入的 MCP 能力。",exampleFiles:"处理本地文件",exampleFilesHint:"选择文件夹后读取、分析或生成文件",exampleMcp:"接入新能力",exampleMcpHint:"选择连接方式，ClearAct 完成配置",openGuide:"查看完整使用指南",guideStep1:"先描述目标",guideStep1Text:"在底部输入框用自然语言说清楚要完成什么。",guideStep2:"观察与调整",guideStep2Text:"任务会先被拆成贴合目标的阶段，再逐步显现。展开阶段可检查过程，也可带着反馈从这里重新执行。",guideStep3:"接入新的能力",guideStep3Text:"在设置中选择远程 URL 或本地命令，填写服务商给出的信息，ClearAct 会保存并测试连接。",mcpRegistryHint:"官方服务注册表",mcpOfficialHint:"官方参考服务",mcpSmitheryHint:"社区 MCP 目录",mcpGlamaHint:"社区 MCP 目录",mcpSourceNote:"第三方服务并非天然可信，请核对发布者和所需权限。",guidePermissionTitle:"权限怎么选？",guidePermissionText:"默认“日常”会自动完成读取和工作区文件修改；外部写入、工作区外写入及破坏性操作仍会先询问。",gotIt:"知道了",connected:"本地网关已连接",placeholder:"告诉 ClearAct 你希望完成什么…",attach:"添加文件或图片",uploading:"正在上传附件…",attachmentGoal:"请处理我上传的附件",workspace:"工作区",workspaceChanged:"工作区已切换",send:"发送",stop:"停止运行",language:"界面语言",maxIterations:"最大迭代",maxTools:"最大工具调用",defaultProfile:"默认 Profile",save:"保存配置",cancel:"取消",confirm:"确定",rename:"重命名",delete:"删除",user:"你",running:"运行中",completed:"已完成",failed:"失败",cancelled:"已停止",created:"准备中",waiting_approval:"等待确认",reasoning:"模型公开说明",reasoningHint:"这里仅展示模型/API 明确返回的公开内容，不尝试还原隐藏思维链。",feedbackHint:"说明你希望怎样调整；将保留此前已完成阶段",restart:"从此阶段重新执行",searches:"检索记录",pages:"已访问网页",files:"文件操作",noSources:"尚无可打开的来源。",success:"成功",denied:"被策略阻止",failedAction:"操作失败",approvalTitle:"确认操作",approveAction:"允许",denyAction:"拒绝"},
  en: {newTopic:"New topic",history:"History",settings:"Settings",settingsHint:"Permissions, models & extensions",guide:"User guide",guideHint:"Start here",welcomeTitle:"Tell me the goal. ClearAct handles the rest.",welcomeHint:"Describe the outcome and ClearAct will use files, web research, and connected MCP capabilities as needed.",exampleFiles:"Work with files",exampleFilesHint:"Choose a folder, then read, analyze, or create files",exampleMcp:"Connect a capability",exampleMcpHint:"Choose a connection and ClearAct configures it",openGuide:"Open guide",guideStep1:"Describe the goal",guideStep1Text:"Use natural language in the composer.",guideStep2:"Observe and adjust",guideStep2Text:"The task is split into specific phases that appear as work begins. Inspect or restart any phase with feedback.",guideStep3:"Connect capabilities",guideStep3Text:"Choose a remote URL or local command in Settings; ClearAct saves and tests it.",mcpRegistryHint:"Official registry",mcpOfficialHint:"Official reference servers",mcpSmitheryHint:"Community MCP directory",mcpGlamaHint:"Community MCP directory",mcpSourceNote:"Verify third-party publishers and requested permissions.",guidePermissionTitle:"Which permissions?",guidePermissionText:"Balanced mode automates reads and workspace edits while asking before external writes and destructive work.",gotIt:"Got it",connected:"Local gateway connected",placeholder:"Tell ClearAct what to do…",attach:"Add files or images",uploading:"Uploading attachments…",attachmentGoal:"Please process the attached files",workspace:"Workspace",workspaceChanged:"Workspace changed",send:"Send",stop:"Stop",language:"Language",maxIterations:"Max iterations",maxTools:"Max tool calls",defaultProfile:"Default profile",save:"Save",cancel:"Cancel",confirm:"Confirm",rename:"Rename",delete:"Delete",user:"You",running:"Running",completed:"Completed",failed:"Failed",cancelled:"Stopped",created:"Ready",waiting_approval:"Awaiting approval",reasoning:"Model explanation",reasoningHint:"Only explicit model/API output is shown; hidden chain-of-thought is not reconstructed.",feedbackHint:"Describe the change; completed earlier phases will be reused",restart:"Restart from this phase",searches:"Searches",pages:"Visited pages",files:"File operations",noSources:"No sources yet.",success:"Succeeded",denied:"Blocked by policy",failedAction:"Failed",approvalTitle:"Approve action",approveAction:"Allow",denyAction:"Deny"}
};

const t = (key) => text[language]?.[key] || key;
const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
function safeHttpUrl(value) { try { const url = new URL(String(value)); return ["http:", "https:"].includes(url.protocol) ? url.href : ""; } catch { return ""; } }
function setGatewayConnected(connected) {
  gatewayConnected = connected;
  $(".live-status").classList.toggle("offline", !connected);
  $(".live-status > span").textContent = connected ? t("connected") : (language === "zh" ? "本地网关已断开" : "Local gateway disconnected");
}
async function fetchGateway(url, options) {
  try {
    const response = await fetch(url, options);
    setGatewayConnected(true);
    return response;
  } catch (error) {
    setGatewayConnected(false);
    const hint = language === "zh" ? "无法连接本地网关。请重新启动 ClearAct 并刷新页面。" : "Cannot reach the local gateway. Restart ClearAct and refresh this page.";
    throw new Error(`${hint} (${error.message})`, {cause:error});
  }
}
async function json(url, options) { const response = await fetchGateway(url, options); const body = response.status === 204 ? null : await response.json(); if (!response.ok) throw new Error(body?.detail || "Request failed"); return body; }
async function requestRunDetail(id, conditional = false) {
  const headers = {};
  if (conditional && runEtags.has(id)) headers["If-None-Match"] = runEtags.get(id);
  const response = await fetchGateway(`/api/runs/${encodeURIComponent(id)}`, {headers, cache:"no-store"});
  if (response.status === 304) return {unchanged:true};
  const body = await response.json();
  if (!response.ok) throw new Error(body?.detail || "Request failed");
  return {detail:body, etag:response.headers.get("ETag")};
}
function rememberRunEtag(id, etag) { if (etag) runEtags.set(id, etag); else runEtags.delete(id); }
function applyLanguage() { document.documentElement.lang = language === "zh" ? "zh-CN" : "en"; document.querySelectorAll("[data-i18n]").forEach((node) => node.textContent = t(node.dataset.i18n)); document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => node.placeholder = t(node.dataset.i18nPlaceholder)); document.querySelectorAll("[data-i18n-title]").forEach((node) => node.title = t(node.dataset.i18nTitle)); setGatewayConnected(gatewayConnected); renderAppearance(); }
function setWorkspace(path) { const effective = path || config?.default_workdir || ""; selectedWorkdir = effective || null; $("#workspace-path").textContent = effective; $("#workspace-indicator").title = effective ? `${t("workspace")}: ${effective}` : t("workspace"); }
function readableSize(bytes) { if (bytes < 1024) return `${bytes} B`; if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`; return `${(bytes / 1048576).toFixed(1)} MB`; }
function renderPendingFiles() { const root = $("#attachment-list"); root.replaceChildren(); pendingFiles.forEach((file,index) => { const chip = document.createElement("span"); chip.className = "attachment-chip"; const label = document.createElement("span"); label.textContent = `${file.type.startsWith("image/") ? "🖼" : "📄"} ${file.name} · ${readableSize(file.size)}`; const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "×"; remove.onclick = () => { pendingFiles.splice(index,1); renderPendingFiles(); }; chip.append(label,remove); root.appendChild(chip); }); root.classList.toggle("hidden", !pendingFiles.length); }
function readBase64(file) { return new Promise((resolve,reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(",",2)[1] || ""); reader.onerror = () => reject(reader.error || new Error("Could not read attachment")); reader.readAsDataURL(file); }); }
async function uploadPendingFiles(selectedFiles, workdir) { if (!selectedFiles.length) return []; $("#feedback").textContent = t("uploading"); const files = await Promise.all(selectedFiles.map(async (file) => ({name:file.name,media_type:file.type || "application/octet-stream",data_base64:await readBase64(file)}))); const result = await json("/api/uploads", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({workdir,files})}); return result.attachments || []; }
function profileOptions(selected) { return config.profiles.map((profile) => `<option value="${escape(profile)}" ${profile === selected ? "selected" : ""}>${escape(profile)}</option>`).join(""); }
async function loadConfig() { config = await json("/api/config"); language = config.interface_language || "zh"; applyLanguage(); setWorkspace(config.default_workdir); }

async function loadRuns() {
  const runs = await json("/api/runs");
  $("#run-count").textContent = runs.length;
  $("#runs").innerHTML = runs.length ? runs.map((run) => `<div class="topic-row"><button class="topic ${run.id === activeId ? "active" : ""}" data-id="${escape(run.id)}" title="${escape(run.title)}"><div class="topic-goal">${escape(run.title)}</div><div class="topic-meta"><span>${new Date(run.updated_at).toLocaleDateString()}</span><span class="badge status-${escape(run.status)}">${t(run.status)}</span></div></button><button class="topic-menu" data-id="${escape(run.id)}">⋯</button></div>`).join("") : `<p class="muted">${language === "zh" ? "尚无过往话题。" : "No topics yet."}</p>`;
  document.querySelectorAll(".topic").forEach((node) => node.onclick = () => showRun(node.dataset.id));
  document.querySelectorAll(".topic-menu").forEach((node) => node.onclick = () => { const run = runs.find((item) => item.id === node.dataset.id); openTopicMenu(run.id, run.title); });
}

function markdown(value) {
  let output = escape(value || "");
  output = output.replace(/^### (.+)$/gm,"<h4>$1</h4>").replace(/^## (.+)$/gm,"<h3>$1</h3>").replace(/^# (.+)$/gm,"<h2>$1</h2>").replace(/\*\*(.+?)\*\*/g,"<strong>$1</strong>").replace(/^[-*] (.+)$/gm,"<li>$1</li>").replace(/(?:<li>.*<\/li>\n?)+/g,(match) => `<ul>${match}</ul>`).replace(/\n\n/g,"</p><p>").replace(/\n/g,"<br>");
  return `<p>${output}</p>`;
}
function resultMap(detail) { return new Map(detail.run.messages.filter((message) => message.role === "tool" && message.tool_call_id).map((message) => [message.tool_call_id, message])); }
function workflowData(detail) {
  const calls = [];
  detail.run.messages.forEach((message, messageIndex) => message.tool_calls?.filter((action) => !["declare_workflow_plan", "declare_workflow_step"].includes(action.tool_name)).forEach((action) => calls.push({...action, messageIndex})));
  return {steps: detail.run.workflow_steps || [], calls};
}
function actionStatus(result) { const status = result?.metadata?.status; if (status === "succeeded") return t("success"); if (status === "skipped") { const labels = language === "zh" ? {search_streak:"先查看已有来源",search_budget:"已达检索预算",local_first:"先读本地资料",local_only:"按要求离线"} : {search_streak:"Open a source first",search_budget:"Search budget reached",local_first:"Read local files first",local_only:"Offline as requested"}; return labels[result.metadata.reason] || (language === "zh" ? "已复用已有结果" : "Reused earlier result"); } if (status === "denied") return t("denied"); if (["failed","cancelled"].includes(status)) return `${t("failedAction")}：${escape(result?.metadata?.error || result?.content || "")}`; return language === "zh" ? "进行中" : "In progress"; }
function guardNote(result) {
  if (result?.metadata?.status !== "skipped") return "";
  const notes = language === "zh"
    ? {search_streak:"已检索多次，请先核对已有网页来源。",search_budget:"这个任务的网页检索预算已用完，可基于现有资料完成或在反馈中要求扩大调查。",local_first:"请先读取附件或工作区文件。",local_only:"你要求仅使用本地资料，网页操作已跳过。"}
    : {search_streak:"Open a found source before searching again.",search_budget:"This task's search budget is spent. Continue from existing evidence or request broader research.",local_first:"Read the attachment or workspace files first.",local_only:"Web actions were skipped because you requested local-only work."};
  return notes[result.metadata.reason] || (language === "zh" ? "相同或相近的操作已完成，已复用原结果。" : "An equivalent action was already completed.");
}
function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  if (total < 60) return language === "zh" ? `${total} 秒` : `${total}s`;
  const minutes = Math.floor(total / 60), remainder = total % 60;
  if (minutes < 60) return language === "zh" ? `${minutes} 分 ${remainder} 秒` : `${minutes}m ${remainder}s`;
  const hours = Math.floor(minutes / 60), rest = minutes % 60;
  return language === "zh" ? `${hours} 小时 ${rest} 分` : `${hours}h ${rest}m`;
}
function runUsageHtml(detail) {
  const run = detail.run;
  const strategy = run.research_strategy;
  const modes = language === "zh" ? {local_only:"仅本地",local_first:"本地优先",mixed:"本地＋联网",research:"深入调查",balanced:"平衡"} : {local_only:"Local only",local_first:"Local first",mixed:"Local + web",research:"Research",balanced:"Balanced"};
  const lastUserIndex = run.messages.findLastIndex((message) => message.role === "user");
  const toolMessages = run.messages.slice(lastUserIndex + 1).filter((message) => message.role === "tool" && !["declare_workflow_plan","declare_workflow_step"].includes(message.name));
  const executed = toolMessages.filter((message) => ["succeeded","failed"].includes(message.metadata?.status));
  const skipped = toolMessages.filter((message) => message.metadata?.status === "skipped").length;
  const searches = executed.filter((message) => message.name === "web_search").length;
  const events = detail.events || [];
  const started = run.status === "created" ? run.updated_at : [...events].reverse().find((event) => event.type === "run.started")?.timestamp || run.created_at;
  const completed = [...events].reverse().find((event) => event.type === "run.completed")?.timestamp;
  const active = ["created","running","waiting_approval","paused"].includes(run.status);
  const startMs = Date.parse(started);
  const completedMs = Date.parse(completed);
  const endMs = active ? Date.now() : (Number.isFinite(completedMs) && completedMs >= startMs ? completedMs : Date.parse(run.updated_at));
  const duration = Number.isFinite(startMs) && Number.isFinite(endMs) ? formatDuration((endMs - startMs) / 1000) : "—";
  return `<aside class="run-usage" aria-label="${language === "zh" ? "任务用量" : "Task usage"}"><b>${language === "zh" ? "本次任务" : "This task"}</b>${strategy ? `<span>${language === "zh" ? "资料策略" : "Evidence"} <strong>${escape(modes[strategy.mode] || strategy.mode)}</strong></span>` : ""}<span>${language === "zh" ? "用时" : "Elapsed"} <strong id="run-elapsed" data-start="${Number.isFinite(startMs) ? startMs : ""}" data-active="${active ? "1" : "0"}">${duration}</strong></span><span>${language === "zh" ? "工具调用" : "Tool calls"} <strong>${executed.length}</strong></span><span>${language === "zh" ? "网页搜索" : "Web searches"} <strong>${searches}${strategy?.total_search_limit != null ? `/${strategy.total_search_limit}` : ""}</strong></span>${skipped ? `<span>${language === "zh" ? "已跳过" : "Skipped"} <strong>${skipped}</strong></span>` : ""}</aside>`;
}
function refreshRunElapsed() {
  const node = $("#run-elapsed"), startMs = Number(node?.dataset.start);
  if (node?.dataset.active === "1" && Number.isFinite(startMs) && startMs > 0) {
    node.textContent = formatDuration((Date.now() - startMs) / 1000);
  }
}
function feedbackShortcuts() {
  const choices = language === "zh"
    ? [["local", "优先本地资料"], ["finish", "基于现有资料完成"]]
    : [["local", "Use local sources"], ["finish", "Finish with current evidence"]];
  return `<div class="feedback-shortcuts"><span>${language === "zh" ? "快速建议" : "Quick suggestions"}</span>${choices.map(([key,label]) => `<button type="button" data-feedback-shortcut="${key}">${label}</button>`).join("")}</div>`;
}
function shortcutText(key) {
  if (key === "local") return language === "zh"
    ? "请优先使用已上传的附件和工作区文件，避免不必要的网页搜索；仅在确实缺少外部事实时再联网。"
    : "Prioritize the uploaded attachments and workspace files. Avoid unnecessary web searches unless external facts are truly missing.";
  return language === "zh"
    ? "请基于已经获取的资料尽快形成可用结果，不要继续重复搜索或阅读相同内容。"
    : "Finish a useful result from the evidence already collected. Do not repeat searches or reread the same content.";
}
function reasoningHtml(detail, step) { const ids = new Set(step.action_ids || []); const reasoning = detail.run.messages.filter((message) => message.role === "assistant" && message.reasoning_content?.trim() && message.tool_calls?.some((action) => ids.has(action.id))).map((message) => message.reasoning_content.trim()); return reasoning.length ? `<details class="reasoning-details"><summary>${t("reasoning")}<span>${language === "zh" ? "展开" : "Expand"}</span></summary><div class="reasoning-content"><p class="reasoning-hint">${t("reasoningHint")}</p>${reasoning.map(markdown).join("")}</div></details>` : ""; }
function researchLayout(detail, actions) {
  const results = resultMap(detail), searches = actions.filter((action) => action.tool_name === "web_search"), pages = actions.filter((action) => action.tool_name === "fetch_url");
  if (!searches.length && !pages.length) return "";
  const searchHtml = searches.map((action) => { const result = results.get(action.id); const urls = [...(result?.content || "").matchAll(/https?:\/\/[^\s)]+/g)].map((match) => match[0]); const note = guardNote(result); return `<section class="action-record"><div><b>${escape(action.arguments?.query || "")}</b><small>${actionStatus(result)}</small></div>${note ? `<p class="guard-note">${note}</p>` : `<div class="source-links">${urls.map((value) => { const url = safeHttpUrl(value); return url ? `<a href="${escape(url)}" target="_blank" rel="noopener">${escape(value)}</a>` : ""; }).join("") || `<span class="muted">${t("noSources")}</span>`}</div>`}</section>`; }).join("");
  const pageHtml = pages.map((action) => { const result = results.get(action.id), value = action.arguments?.url || "", url = safeHttpUrl(value), note = guardNote(result); return `<section class="action-record"><div>${url ? `<a href="${escape(url)}" target="_blank" rel="noopener">${escape(value)}</a>` : escape(value)}<small>${actionStatus(result)}</small></div>${note ? `<p class="guard-note">${note}</p>` : ""}</section>`; }).join("");
  return `<div class="research-layout">${searchHtml ? `<h4>${t("searches")}</h4>${searchHtml}` : ""}${pageHtml ? `<h4>${t("pages")}</h4>${pageHtml}` : ""}</div>`;
}
function actionLayout(detail, actions) {
  const results = resultMap(detail), ordinary = actions.filter((action) => !["web_search","fetch_url"].includes(action.tool_name));
  if (!ordinary.length) return "";
  return `<div class="action-layout"><h4>${t("files")}</h4>${ordinary.map((action) => { const result = results.get(action.id), target = result?.metadata?.path || action.arguments?.path || action.arguments?.url || ""; const preview = result?.metadata?.status === "succeeded" && ["read_file","read_pdf","write_file"].includes(action.tool_name); return `<section class="action-record"><div><b>${escape(action.tool_name.replace(/^mcp__/, ""))}</b><small>${actionStatus(result)}</small></div>${target ? `<p>${escape(target)}</p>` : ""}${preview ? `<button type="button" class="file-preview-trigger" data-preview-action="${escape(action.id)}">${language === "zh" ? (action.tool_name === "write_file" ? "查看改动" : "预览内容") : (action.tool_name === "write_file" ? "View changes" : "Preview")}</button>` : ""}</section>`; }).join("")}</div>`;
}
async function openFilePreview(actionId) {
  if (!activeId) return;
  const dialog = $("#file-preview-dialog");
  $("#file-preview-title").textContent = language === "zh" ? "正在读取…" : "Loading…";
  $("#file-preview-path").textContent = "";
  $("#file-preview-content").textContent = "";
  dialog.showModal();
  try {
    const result = await json(`/api/runs/${encodeURIComponent(activeId)}/actions/${encodeURIComponent(actionId)}/preview`);
    $("#file-preview-title").textContent = result.kind === "diff" ? (language === "zh" ? "文件改动" : "File changes") : (language === "zh" ? "文件预览" : "File preview");
    $("#file-preview-path").textContent = result.path + (result.truncated ? (language === "zh" ? " · 仅显示前 12 万字符" : " · first 120,000 characters") : "");
    $("#file-preview-content").textContent = result.content || (language === "zh" ? "（没有文本内容）" : "(No text content)");
    $("#file-preview-content").classList.toggle("diff-content", result.kind === "diff");
  } catch (error) { $("#file-preview-content").textContent = error.message; }
}
function planHtml(run) {
  if (!run.workflow_plan?.length) return `<div class="plan-loading"><i></i>${language === "zh" ? "正在生成与任务匹配的步骤…" : "Creating a task-specific plan…"}</div>`;
  const reached = new Set((run.workflow_steps || []).map((step) => step.plan_item_id));
  return `<ol class="workflow-plan">${run.workflow_plan.map((item) => `<li class="${reached.has(item.id) ? "reached" : ""}"><span></span><div><b>${escape(item.title)}</b><p>${escape(item.summary)}</p></div></li>`).join("")}</ol>`;
}
function stepCard(detail, step, calls, index) {
  const actions = (step.action_ids || []).map((id) => calls.find((call) => call.id === id)).filter(Boolean);
  const isUnderstand = step.id === "understand";
  const specialized = isUnderstand ? planHtml(detail.run) : researchLayout(detail, actions) + actionLayout(detail, actions);
  const canRestart = !["created","running","waiting_approval","paused"].includes(detail.run.status);
  const open = openStepDetails.has(step.id) ? " open" : "";
  return `<article class="message stage-message" data-step-id="${escape(step.id)}"><div class="stage-rail"><span class="stage-index">${String(index + 1).padStart(2,"0")}</span><i class="stage-dot ${escape(step.status || "completed")}"></i></div><section class="stage-card"><div class="stage-title"><h3>${escape(step.title)}</h3><span class="step-status">${t(step.status || "completed")}</span></div><details class="stage-details"${open}><summary>${escape(step.summary)}<span>${language === "zh" ? "查看详情" : "Details"}</span></summary><div class="stage-expanded">${specialized || `<p class="process-label">${escape(step.summary)}</p>`}${reasoningHtml(detail, step)}${canRestart ? feedbackShortcuts() : ""}<form class="stage-feedback"><textarea placeholder="${escape(t("feedbackHint"))}"></textarea><button class="secondary-button" type="submit" ${canRestart ? "" : "disabled"}>${t("restart")}</button></form></div></details></section></article>`;
}
function revisionHtml(run) { const revision = run.workflow_revisions?.at(-1); if (!revision) return ""; const count = revision.reused_step_ids?.length || 0, restored = revision.restored_snapshot_ids?.length || 0, warning = revision.rollback_warnings?.length ? (language === "zh" ? "；部分外部操作无法自动撤销" : "; some external effects could not be undone") : ""; return `<div class="revision-banner">↺ ${language === "zh" ? `已按反馈创建新分支，复用 ${count} 个前置阶段，恢复 ${restored} 个文件快照` : `New branch; reused ${count} phases and restored ${restored} file snapshots`}${warning}<small>${escape(revision.feedback || "")}</small></div>`; }
function attachmentsHtml(message) { const attachments = message?.metadata?.attachments || []; if (!attachments.length) return ""; return `<div class="sent-attachments">${attachments.map((item) => `<span>${item.kind === "image" ? "🖼" : "📄"} <b>${escape(item.name)}</b><small>${escape(item.path)} · ${readableSize(item.size || 0)}</small></span>`).join("")}</div>`; }
function connectionHtml(detail) { const event = [...(detail.events || [])].reverse().find((item) => ["model.retrying","model.recovered"].includes(item.type)); if (event?.type !== "model.retrying" || !["created","running"].includes(detail.run.status)) return ""; const attempt = event.data?.attempt || 2, total = event.data?.max_attempts || 3; return `<div class="connection-banner"><i></i><div><b>${escape(event.title)}</b><small>${escape(event.detail || "")} · ${language === "zh" ? `第 ${attempt}/${total} 次尝试` : `attempt ${attempt}/${total}`}</small></div></div>`; }
function deliverCard(detail) { const final = [...detail.run.messages].reverse().find((message) => message.role === "assistant" && !message.tool_calls?.length); if (!final && ["created","running","waiting_approval","paused"].includes(detail.run.status)) return ""; return `<article class="message stage-message result-card"><div class="stage-rail"><span class="stage-index">✓</span></div><section class="stage-card"><div class="stage-title"><h3>${language === "zh" ? "结果" : "Result"}</h3></div><div class="stage-direct">${final ? markdown(final.content) : `<p class="muted">${language === "zh" ? "任务没有生成最终答复。" : "No final response."}</p>`}</div>${final ? '<span class="copy-response-slot"></span>' : ""}</section></article>`; }
const animatedStepKeys = new Set();
const stageEntranceObserver = "IntersectionObserver" in window ? new IntersectionObserver((entries, observer) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    const node = entry.target;
    animatedStepKeys.add(node.dataset.motionKey);
    node.classList.remove("stage-awaiting");
    node.classList.add("stage-entering");
    observer.unobserve(node);
  }
}, {root: $("#conversation"), rootMargin: "0px 0px 60px 0px", threshold: .05}) : null;

async function restartAtStage(stepId, feedback) {
  const id = activeId, epoch = viewEpoch;
  const result = await json("/api/runs", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({goal:feedback, run_id:id, rewind_step_id:stepId, interface_language:language})});
  if (activeId !== id || viewEpoch !== epoch) return;
  lastRunSignature = null;
  await showRun(result.run_id);
}
function bindStageDetails(node) {
  const stepId = node.closest("[data-step-id]").dataset.stepId;
  node.ontoggle = () => node.open ? openStepDetails.add(stepId) : openStepDetails.delete(stepId);
  const panel = node.querySelector(".stage-expanded");
  if (!panel) return;
  node.classList.add("motion-controlled");
  node._motionOpen = node.open;
  node.querySelector("summary").addEventListener("click", (event) => {
    if (reducedMotion.matches) return;
    event.preventDefault();
    const opening = !node._motionOpen;
    node._motionOpen = opening;
    const startHeight = node.open ? panel.getBoundingClientRect().height : 0;
    window.clearTimeout(node._motionTimer);
    if (opening) node.open = true;
    opening ? openStepDetails.add(stepId) : openStepDetails.delete(stepId);
    panel.style.transition = "none";
    panel.style.overflow = "hidden";
    panel.style.height = `${startHeight}px`;
    panel.style.opacity = opening && !startHeight ? "0" : "1";
    panel.style.transform = opening && !startHeight ? "translateY(-10px)" : "none";
    void panel.offsetHeight;
    panel.style.transition = "";
    panel.style.height = `${opening ? panel.scrollHeight : 0}px`;
    panel.style.opacity = opening ? "1" : "0";
    panel.style.transform = opening ? "none" : "translateY(-10px)";
    node._motionTimer = window.setTimeout(() => {
      if (node._motionOpen !== opening) return;
      panel.style.overflow = "";
      panel.style.height = "";
      panel.style.opacity = "";
      panel.style.transform = "";
      if (!opening) node.open = false;
    }, 530);
  });
}
function renderRun(detail, {force = false} = {}) {
  const signature = JSON.stringify([detail.run.updated_at, detail.run.status, detail.run.messages.length, detail.events.length, detail.approval?.action_id, detail.run.workflow_steps, detail.run.workflow_plan]);
  $("#topic-title").textContent = detail.run.title || detail.run.goal.slice(0,48);
  $("#topic-title").title = detail.run.title || detail.run.goal;
  $("#stop-run").classList.toggle("hidden", !["created","running","waiting_approval","paused"].includes(detail.run.status));
  $("#empty-state").classList.add("hidden"); $("#conversation").classList.remove("hidden"); syncApproval(detail);
  if (!force && signature === lastRunSignature) return;
  document.querySelectorAll(".stage-details[open]").forEach((node) => openStepDetails.add(node.closest("[data-step-id]")?.dataset.stepId));
  const {steps, calls} = workflowData(detail); const firstUser = detail.run.messages.find((message) => message.role === "user");
  stageEntranceObserver?.disconnect();
  $("#detail").innerHTML = `${firstUser ? `<article class="message role-user"><b>${t("user")}</b><div class="markdown">${markdown(firstUser.content)}</div>${attachmentsHtml(firstUser)}</article>` : ""}${runUsageHtml(detail)}${revisionHtml(detail.run)}${connectionHtml(detail)}${steps.map((step,index) => stepCard(detail,step,calls,index)).join("")}${deliverCard(detail)}`;
  if (!reducedMotion.matches) document.querySelectorAll("#detail .stage-message").forEach((node, index) => {
    const key = `${detail.run.id}:${node.dataset.stepId || "result"}`;
    if (animatedStepKeys.has(key)) return;
    node.dataset.motionKey = key;
    node.style.setProperty("--entry-delay", `${Math.min(index, 5) * 65}ms`);
    if (stageEntranceObserver) { node.classList.add("stage-awaiting"); stageEntranceObserver.observe(node); }
    else { animatedStepKeys.add(key); node.classList.add("stage-entering"); }
  });
  document.querySelectorAll(".stage-details").forEach(bindStageDetails);
  document.querySelectorAll("[data-feedback-shortcut]").forEach((button) => button.onclick = () => { const field = button.closest(".stage-expanded").querySelector(".stage-feedback textarea"); field.value = shortcutText(button.dataset.feedbackShortcut); field.focus(); });
  document.querySelectorAll(".stage-feedback").forEach((form) => form.onsubmit = async (event) => { event.preventDefault(); const feedback = form.querySelector("textarea").value.trim(); const stepId = form.closest("[data-step-id]").dataset.stepId; if (!feedback) return; try { await restartAtStage(stepId, feedback); } catch (error) { $("#feedback").textContent = error.message; } });
  document.querySelectorAll("[data-preview-action]").forEach((button) => button.onclick = () => openFilePreview(button.dataset.previewAction));
  const final = [...detail.run.messages].reverse().find((message) => message.role === "assistant" && !message.tool_calls?.length), slot = $("#detail .copy-response-slot");
  if (slot && final?.content) { const button = document.createElement("button"); button.className = "copy-response secondary-button"; button.type = "button"; button.dataset.copy=final.content; button.textContent = language === "zh" ? "复制答复" : "Copy"; button.onclick = async () => { await navigator.clipboard.writeText(button.dataset.copy); button.textContent = language === "zh" ? "已复制" : "Copied"; }; slot.replaceWith(button); }
  lastRunSignature = signature;
}

function syncApproval(detail) { const dialog = $("#approval-dialog"), approval = detail.approval; if (!approval) { if (dialog.open) dialog.close(); return; } dialog.dataset.runId = detail.run.id; dialog.dataset.actionId = approval.action_id; $("#approval-summary").textContent = `${approval.tool_name} · ${approval.risk}\n${(approval.reasons || []).join("\n")}`; $("#approval-arguments").textContent = JSON.stringify(approval.arguments || {}, null, 2); if (!dialog.open) dialog.showModal(); }
async function submitApproval(approved) {
  const dialog = $("#approval-dialog"), id = dialog.dataset.runId, actionId = dialog.dataset.actionId, epoch = viewEpoch;
  await json(`/api/runs/${id}/approval`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({action_id:actionId, approved})});
  if (activeId !== id || viewEpoch !== epoch) return;
  dialog.close();
  await showRun(id);
}
async function showRun(id, {force = true} = {}) {
  if (activeId !== id && $("#approval-dialog").open) $("#approval-dialog").close();
  activeId = id;
  const epoch = ++viewEpoch;
  const result = await requestRunDetail(id);
  if (activeId !== id || viewEpoch !== epoch) return;
  rememberRunEtag(id, result.etag);
  const detail = result.detail;
  setWorkspace(detail.run.execution?.workdir);
  renderRun(detail, {force});
  await loadRuns();
}
function newTopic() { activeId = null; viewEpoch++; if ($("#approval-dialog").open) $("#approval-dialog").close(); lastRunSignature = null; pendingFiles = []; renderPendingFiles(); setWorkspace(config?.default_workdir); $("#stop-run").classList.add("hidden"); $("#topic-title").textContent = t("newTopic"); $("#topic-title").title = ""; $("#conversation").classList.add("hidden"); $("#empty-state").classList.remove("hidden"); $("#goal").focus(); loadRuns(); }

let topicAction = null;
function openTopicMenu(id,title) { topicAction = {id, mode:"rename"}; $("#topic-dialog-title").textContent = `${t("rename")} / ${t("delete")}`; $("#topic-input").value = title; $("#topic-input").classList.remove("hidden"); $("#topic-delete-warning").classList.add("hidden"); $("#topic-delete").classList.remove("hidden"); $("#topic-delete").onclick = () => { topicAction.mode = "delete"; $("#topic-delete-warning").classList.remove("hidden"); $("#topic-input").classList.add("hidden"); $("#topic-delete").classList.add("hidden"); }; $("#topic-dialog").showModal(); }

const capabilityMeta = [
  ["local_read","读取本地文件","查看已授权目录中的内容"],["web_read","联网查资料","搜索和读取公开网页"],["workspace_create","创建文件","在工作区生成新文件"],["workspace_modify","修改文件","更新工作区已有文件"],["mcp_read","MCP 只读","调用服务声明为只读的工具"],["mcp_write","MCP 外部操作","调用可能改变外部状态的工具"],["outside_write","工作区外写入","修改本次工作目录之外的文件"],["destructive","高影响操作","删除、发布或服务声明的破坏性动作"]
];
const presets = {
  cautious:{local_read:"allow",web_read:"allow",workspace_create:"ask",workspace_modify:"ask",mcp_read:"ask",mcp_write:"ask",outside_write:"ask",destructive:"ask",other:"ask"},
  balanced:{local_read:"allow",web_read:"allow",workspace_create:"allow",workspace_modify:"allow",mcp_read:"allow",mcp_write:"ask",outside_write:"ask",destructive:"ask",other:"ask"},
  trusted:{local_read:"allow",web_read:"allow",workspace_create:"allow",workspace_modify:"allow",mcp_read:"allow",mcp_write:"allow",outside_write:"allow",destructive:"ask",other:"allow"}
};
function renderCapabilityRules(rules) { $("#capability-rules").innerHTML = capabilityMeta.map(([key,title,hint]) => `<div class="capability-row"><div><b>${title}</b><small>${hint}</small></div><select data-capability="${key}">${key === "destructive" ? "" : `<option value="allow" ${rules[key] === "allow" ? "selected" : ""}>自动执行</option>`}<option value="ask" ${rules[key] === "ask" ? "selected" : ""}>先询问</option><option value="deny" ${rules[key] === "deny" ? "selected" : ""}>关闭</option></select></div>`).join(""); }
function currentCapabilityRules() { const rules = {}; document.querySelectorAll("[data-capability]").forEach((node) => rules[node.dataset.capability] = node.value); rules.other = settings?.capability_rules?.other || "ask"; return rules; }
function syncProfileProvider(card) { const provider = card.querySelector('[data-key="provider"]').value, hint = card.querySelector(".profile-hint"); hint.textContent = provider === "ollama" ? (language === "zh" ? "Ollama 原生接口：Base URL 通常为 http://localhost:11434，程序调用 /api/chat。" : "Native Ollama: the Base URL is usually http://localhost:11434; ClearAct calls /api/chat.") : (language === "zh" ? "OpenAI 兼容接口：Base URL 应包含 /v1，程序调用 /chat/completions。" : "OpenAI-compatible: include /v1 in the Base URL; ClearAct calls /chat/completions."); }
function renderProfileSettings() { const root = $("#profile-settings"); root.replaceChildren(); Object.entries(settings.profiles || {}).forEach(([name,profile]) => { const card = document.createElement("div"); card.className = "profile-card"; card.dataset.profile = name; card.innerHTML = `<strong>${escape(name)}</strong><label>接口类型<select data-key="provider"><option value="ollama">Ollama 原生（/api/chat）</option><option value="openai_compatible">OpenAI 兼容（/v1/chat/completions）</option></select></label><label>Model<input data-key="model"></label><small class="profile-hint span-two"></small><label>Base URL<input data-key="baseUrl"></label><label>Context<input data-key="contextWindow" type="number"></label><label class="span-two">API Key<input data-key="apiKey" type="password"></label>`; Object.entries({provider:"provider",model:"model",baseUrl:"baseUrl",contextWindow:"contextWindow"}).forEach(([key]) => card.querySelector(`[data-key="${key}"]`).value = profile[key] ?? ""); card.querySelector('[data-key="provider"]').onchange = () => syncProfileProvider(card); card.querySelector('[data-key="apiKey"]').placeholder = profile.hasApiKey ? "已设置，留空保持不变" : "留空表示不设置"; syncProfileProvider(card); root.appendChild(card); }); }
function renderMcpServers() { const entries = Object.entries(settings.mcp_servers || {}); $("#mcp-server-list").innerHTML = entries.length ? entries.map(([name,server]) => `<article class="mcp-server-card"><span class="connection-dot ${server.enabled === false ? "off" : ""}"></span><div><b>${escape(name)}</b><small>${escape(server.url || server.command || "")}</small></div><label class="mini-toggle"><input type="checkbox" data-mcp-toggle="${escape(name)}" ${server.enabled === false ? "" : "checked"}>启用</label><button type="button" data-mcp-test="${escape(name)}">测试</button><button type="button" data-mcp-edit="${escape(name)}">编辑</button><button type="button" data-mcp-delete="${escape(name)}">删除</button></article>`).join("") : `<div class="empty-extension">还没有 MCP 服务。点击“添加服务”即可开始。</div>`; bindMcpActions(); }
let mcpCatalog = [];
async function searchMcpCatalog() {
  const query = $("#mcp-catalog-query").value.trim(), root = $("#mcp-catalog-results");
  if (query.length < 2) { root.textContent = "请输入至少两个字符。"; return; }
  root.textContent = "正在搜索…";
  try {
    const result = await json(`/api/mcp/catalog?q=${encodeURIComponent(query)}`);
    mcpCatalog = result.servers || [];
    root.innerHTML = mcpCatalog.length ? mcpCatalog.map((item, index) => `<article class="mcp-catalog-card"><div><b>${escape(item.title || item.name)}</b><small>${escape(item.description || item.name)}</small><code>${escape(item.url)}</code>${item.needs_auth ? `<em>可能需要服务商凭据</em>` : ""}</div><button type="button" data-catalog-index="${index}" class="secondary-button">选择并配置</button></article>`).join("") : "没有找到可直接连接的远程服务。也可以手动添加 URL。";
    root.querySelectorAll("[data-catalog-index]").forEach((button) => button.onclick = () => chooseMcpCatalog(Number(button.dataset.catalogIndex)));
  } catch (error) { root.textContent = error.message; }
}
function chooseMcpCatalog(index) {
  const item = mcpCatalog[index]; if (!item) return;
  openMcpEditor();
  const baseName = (item.title || item.name).replace(/[\\/]/g, "-").slice(0, 75);
  let name = baseName, suffix = 2;
  while (settings.mcp_servers?.[name]) name = `${baseName} ${suffix++}`;
  $("#mcp-name").value = name;
  $("#mcp-transport").value = "streamable_http";
  $("#mcp-url").value = item.url;
  syncMcpTransport();
  $("#mcp-feedback").textContent = item.needs_auth ? "已填入服务地址；如需凭据，请先从服务商获取，再保存并测试。" : "已填入服务地址。核对后点击“保存并测试”。";
  $("#mcp-editor").scrollIntoView({behavior:"smooth", block:"center"});
}
function bindMcpActions() { document.querySelectorAll("[data-mcp-toggle]").forEach((node) => node.onchange = async () => { await json(`/api/mcp/servers/${encodeURIComponent(node.dataset.mcpToggle)}/enabled?enabled=${node.checked}`, {method:"PUT"}); await refreshSettings(); }); document.querySelectorAll("[data-mcp-test]").forEach((node) => node.onclick = () => testMcp(node.dataset.mcpTest)); document.querySelectorAll("[data-mcp-edit]").forEach((node) => node.onclick = () => openMcpEditor(node.dataset.mcpEdit)); document.querySelectorAll("[data-mcp-delete]").forEach((node) => node.onclick = async () => { if (!confirm(`删除 MCP 服务 ${node.dataset.mcpDelete}？`)) return; await json(`/api/mcp/servers/${encodeURIComponent(node.dataset.mcpDelete)}`, {method:"DELETE"}); await refreshSettings(); }); }
async function refreshSettings() { settings = await json("/api/settings"); renderMcpServers(); }
function openMcpEditor(name = null) { editingMcpName = name; const server = name ? settings.mcp_servers[name] : {}; $("#mcp-name").value = name || ""; $("#mcp-name").disabled = Boolean(name); $("#mcp-transport").value = server.transport || (server.url ? "streamable_http" : "streamable_http"); $("#mcp-url").value = server.url || ""; $("#mcp-command").value = server.command || ""; $("#mcp-args").value = (server.args || []).join(" "); $("#mcp-secret-name").value = server.headerKeys?.[0] || server.envKeys?.[0] || ""; $("#mcp-secret-kind").value = server.envKeys?.length ? "env" : "header"; $("#mcp-secret-value").value = ""; $("#mcp-editor").classList.remove("hidden"); syncMcpTransport(); }
function syncMcpTransport() { const local = $("#mcp-transport").value === "stdio"; document.querySelectorAll(".mcp-stdio-field").forEach((node) => node.classList.toggle("hidden", !local)); document.querySelectorAll(".mcp-http-field").forEach((node) => node.classList.toggle("hidden", local)); }
function splitArgs(value) { return [...value.matchAll(/"([^"]*)"|'([^']*)'|([^\s]+)/g)].map((match) => match[1] ?? match[2] ?? match[3]); }
async function testMcp(name) { const note = $("#mcp-feedback"); note.textContent = `正在测试 ${name}…`; try { const result = await json(`/api/mcp/servers/${encodeURIComponent(name)}/test`, {method:"POST"}); note.textContent = `连接成功，发现 ${result.tools.length} 个工具`; return true; } catch (error) { note.textContent = `连接失败：${error.message}`; return false; } }
async function saveMcp() { const body = {name:editingMcpName || $("#mcp-name").value.trim(), transport:$("#mcp-transport").value, url:$("#mcp-url").value.trim() || null, command:$("#mcp-command").value.trim() || null, args:splitArgs($("#mcp-args").value), secret_kind:$("#mcp-secret-kind").value, secret_name:$("#mcp-secret-name").value.trim() || null, secret_value:$("#mcp-secret-value").value || null, enabled:false}; const result = await json("/api/mcp/servers", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)}); $("#mcp-editor").classList.add("hidden"); const connected = await testMcp(body.name); if (connected) { await json(`/api/mcp/servers/${encodeURIComponent(body.name)}/enabled?enabled=true`, {method:"PUT"}); } else { $("#mcp-feedback").textContent += "；服务暂未启用，不会拖慢任务。"; } await refreshSettings(); return result; }

async function openSettings() { settings = await json("/api/settings"); $("#interface-language").value = settings.interface_language || "zh"; $("#default-autonomy").value = settings.default_autonomy || "yellow"; $("#default-iterations").value = settings.max_iterations; $("#default-tool-calls").value = settings.max_tool_calls; $("#default-profile").innerHTML = profileOptions(settings.default_profile); renderCapabilityRules({...presets.balanced,...settings.capability_rules}); renderProfileSettings(); renderMcpServers(); $("#settings-dialog").showModal(); }

$("#grant-approval").onclick = () => submitApproval(true); $("#deny-approval").onclick = () => submitApproval(false);
$("#stop-run").onclick = async () => { const id = activeId, epoch = viewEpoch; if (id) { await json(`/api/runs/${id}/stop`, {method:"POST"}); if (activeId === id && viewEpoch === epoch) await showRun(id); } };
$("#guide-trigger").onclick = () => $("#guide-dialog").showModal(); $("#welcome-guide").onclick = () => $("#guide-dialog").showModal();
function chooseWorkdir() {
  $("#workspace-input").value = selectedWorkdir || "";
  $("#workspace-feedback").textContent = "";
  $("#workspace-browser").classList.add("hidden");
  $("#browse-workspace").setAttribute("aria-expanded", "false");
  $("#workspace-dialog").showModal();
  $("#workspace-input").focus();
}
let browsePath = null;
let browseParent = null;
let browseRequest = 0;
let browseSearchTimer = null;
async function loadWorkspaceFolders(path, query = "") {
  const request = ++browseRequest;
  const list = $("#workspace-browser-list");
  list.textContent = language === "zh" ? "正在读取文件夹…" : "Loading folders…";
  $("#workspace-browser-hint").textContent = "";
  try {
    const params = new URLSearchParams();
    if (path) params.set("path", path);
    if (query) params.set("query", query);
    const result = await json(`/api/workspaces/browse?${params}`);
    if (request !== browseRequest || !$("#workspace-dialog").open) return;
    browsePath = result.path;
    browseParent = result.parent;
    $("#workspace-parent").disabled = !browseParent;
    $("#workspace-browser-path").textContent = result.path || (language === "zh" ? "此电脑" : "This computer");
    $("#workspace-browser-search").disabled = !result.path;
    list.replaceChildren();
    for (const folder of result.directories) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "workspace-folder";
      button.textContent = `📁 ${folder.name}`;
      button.title = folder.path;
      button.onclick = () => { $("#workspace-input").value = folder.path; navigateWorkspace(folder.path); };
      list.appendChild(button);
    }
    if (!result.directories.length) list.textContent = language === "zh" ? "没有子文件夹，可直接使用当前文件夹。" : "No subfolders. You can use this folder.";
    if (result.truncated) $("#workspace-browser-hint").textContent = language === "zh" ? "子文件夹较多，请用上方搜索框查找。" : "Many folders; use search above.";
    $("#workspace-feedback").textContent = "";
  } catch (error) {
    if (request !== browseRequest) return;
    list.textContent = "";
    $("#workspace-feedback").textContent = error.message;
  }
}
function navigateWorkspace(path) {
  clearTimeout(browseSearchTimer);
  $("#workspace-browser-search").value = "";
  loadWorkspaceFolders(path);
}
$("#browse-workspace").onclick = () => {
  const browser = $("#workspace-browser");
  const opening = browser.classList.contains("hidden");
  browser.classList.toggle("hidden", !opening);
  $("#browse-workspace").setAttribute("aria-expanded", String(opening));
  if (opening) navigateWorkspace($("#workspace-input").value.trim() || null);
  else browseRequest++;
};
$("#workspace-roots").onclick = () => navigateWorkspace(null);
$("#workspace-parent").onclick = () => { if (browseParent) { $("#workspace-input").value = browseParent; navigateWorkspace(browseParent); } };
$("#workspace-browser-search").oninput = () => {
  clearTimeout(browseSearchTimer);
  const query = $("#workspace-browser-search").value.trim();
  browseSearchTimer = setTimeout(() => loadWorkspaceFolders(browsePath, query), 180);
};
$("#workspace-dialog").addEventListener("close", () => { browseRequest++; clearTimeout(browseSearchTimer); });
$("#workspace-form").onsubmit = async (event) => {
  event.preventDefault();
  try {
    const result = await json("/api/workspaces/validate", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({path:$("#workspace-input").value.trim()})});
    setWorkspace(result.path);
    $("#workspace-dialog").close();
    $("#feedback").textContent = t("workspaceChanged");
    $("#goal").focus();
  } catch (error) { $("#workspace-feedback").textContent = error.message; }
};
$("#choose-workdir").onclick = chooseWorkdir;
$("#workspace-indicator").onclick = chooseWorkdir;
$("#theme-toggle").onclick = toggleAppearance;
renderAppearance();
void loadStoredWallpaper();
const feedbackNode = $("#feedback");
new MutationObserver(() => $("#copy-feedback").classList.toggle("hidden", !feedbackNode.textContent.trim())).observe(feedbackNode, {childList:true, characterData:true, subtree:true});
$("#copy-feedback").onclick = async () => {
  const button = $("#copy-feedback");
  try {
    await navigator.clipboard.writeText(feedbackNode.textContent);
    button.textContent = language === "zh" ? "已复制" : "Copied";
  } catch {
    const selection = window.getSelection(), range = document.createRange();
    range.selectNodeContents(feedbackNode);
    selection.removeAllRanges();
    selection.addRange(range);
    button.textContent = document.execCommand("copy")
      ? (language === "zh" ? "已复制" : "Copied")
      : (language === "zh" ? "已选中，请按 Ctrl+C" : "Selected; press Ctrl+C");
  }
};
$("#attach-files").onclick = () => $("#file-input").click();
$("#file-input").onchange = () => { const input = $("#file-input"), additions = [...input.files]; input.value = ""; const combined = [...pendingFiles,...additions]; if (combined.length > 8) { $("#feedback").textContent = language === "zh" ? "一次最多上传 8 个附件" : "Up to 8 attachments per message"; return; } if (combined.some((file) => file.size > 12 * 1024 * 1024) || combined.reduce((sum,file) => sum + file.size,0) > 32 * 1024 * 1024) { $("#feedback").textContent = language === "zh" ? "单个附件不能超过 12 MB，总计不能超过 32 MB" : "Each attachment must be under 12 MB and the total under 32 MB"; return; } pendingFiles = combined; $("#feedback").textContent = ""; renderPendingFiles(); };
$("#open-mcp-quick").onclick = async () => { await openSettings(); openMcpEditor(); $("#mcp-settings").scrollIntoView({behavior:"smooth"}); };
$("#new-topic").onclick = newTopic; $("#settings-trigger").onclick = openSettings; $("#save-settings-button").onclick = () => $("#settings-form").requestSubmit();
$("#add-mcp-button").onclick = () => openMcpEditor(); $("#cancel-mcp-button").onclick = () => $("#mcp-editor").classList.add("hidden"); $("#mcp-transport").onchange = syncMcpTransport; $("#save-mcp-button").onclick = async () => { try { await saveMcp(); } catch (error) { $("#mcp-feedback").textContent = error.message; } };
$("#mcp-catalog-search").onclick = searchMcpCatalog;
$("#mcp-catalog-query").onkeydown = (event) => { if (event.key === "Enter") { event.preventDefault(); searchMcpCatalog(); } };
document.querySelectorAll("[data-preset]").forEach((button) => button.onclick = () => { renderCapabilityRules(presets[button.dataset.preset]); document.querySelectorAll("[data-preset]").forEach((item) => item.classList.toggle("active", item === button)); });
document.querySelectorAll("[data-close]").forEach((button) => button.onclick = () => $("#" + button.dataset.close).close());
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const finePointer = window.matchMedia("(hover: hover) and (pointer: fine)");
// Pick once per encounter; crossing text/icon children must not restart the effect.
const motionSelector = ".quick-card, .stage-card, .topic, .settings-button, .composer, .capability-row, .mcp-catalog-card, .mcp-server-card";
let glowFrame = 0;
let glowTarget = null;
let glowBounds = null;
let glowX = 0;
let glowY = 0;
let squishTimer = 0;
function chooseSurfaceMotion(target) {
  if (!jellyMotionEnabled) return;
  target.dataset.motion = "jelly";
}
function releaseSurfaceMotion() {
  if (glowFrame) cancelAnimationFrame(glowFrame);
  glowFrame = 0;
  if (glowTarget) {
    glowTarget.classList.remove("motion-active");
    ["--tilt-x", "--tilt-y", "--drift-x", "--drift-y", "--squish-x", "--squish-y"].forEach((key) => glowTarget.style.removeProperty(key));
  }
  window.clearTimeout(squishTimer);
  glowTarget = null;
  glowBounds = null;
}
function enterSurfaceMotion(target) {
  if (glowTarget === target) return;
  releaseSurfaceMotion();
  if (!target || target.matches(":disabled")) return;
  // Measure the resting surface, so its own animation never feeds back into tracking.
  glowBounds = target.getBoundingClientRect();
  glowTarget = target;
  if (!target.contains(document.activeElement) || !target.dataset.motion) chooseSurfaceMotion(target);
  target.classList.add("motion-active");
}
document.addEventListener("pointerover", (event) => {
  if (!jellyMotionEnabled || !finePointer.matches || reducedMotion.matches || event.pointerType === "touch") return;
  const target = event.target instanceof Element ? event.target.closest(motionSelector) : null;
  if (target && !target.contains(event.relatedTarget)) enterSurfaceMotion(target);
});
document.addEventListener("pointermove", (event) => {
  if (!jellyMotionEnabled || !finePointer.matches || reducedMotion.matches || event.pointerType === "touch") return;
  const element = event.target instanceof Element ? event.target : null;
  glowX = event.clientX;
  glowY = event.clientY;
  const target = element?.closest(motionSelector);
  if (target && target !== glowTarget) enterSurfaceMotion(target);
  if (glowTarget) {
    const isReading = glowTarget.matches(":focus-within") || Boolean(glowTarget.querySelector(".stage-details[open]"));
    const power = jellyStrength / 100;
    const energyX = isReading ? 0 : Math.min(.17 * power, Math.abs(event.movementX || 0) / 20 * .17 * power);
    const energyY = isReading ? 0 : Math.min(.17 * power, Math.abs(event.movementY || 0) / 20 * .17 * power);
    glowTarget.style.setProperty("--squish-x", `${(1 + energyY - energyX * .8).toFixed(3)}`);
    glowTarget.style.setProperty("--squish-y", `${(1 + energyX - energyY * .8).toFixed(3)}`);
    window.clearTimeout(squishTimer);
    squishTimer = window.setTimeout(() => {
      if (glowTarget?.isConnected) {
        glowTarget.style.setProperty("--squish-x", "1");
        glowTarget.style.setProperty("--squish-y", "1");
      }
    }, 90);
  }
  // A small resting-area margin keeps lifted edges from repeatedly entering/leaving.
  if (!target && glowBounds && (glowX < glowBounds.left - 8 || glowX > glowBounds.right + 8 || glowY < glowBounds.top - 8 || glowY > glowBounds.bottom + 8)) releaseSurfaceMotion();
  if (!glowTarget || glowFrame) return;
  glowFrame = requestAnimationFrame(() => {
    glowFrame = 0;
    if (!glowTarget?.isConnected || !glowBounds) { releaseSurfaceMotion(); return; }
    const rect = glowBounds;
    const x = Math.max(-.5, Math.min(.5, (glowX - rect.left) / Math.max(1, rect.width) - .5));
    const y = Math.max(-.5, Math.min(.5, (glowY - rect.top) / Math.max(1, rect.height) - .5));
    const quick = glowTarget.matches(".quick-card");
    const resting = glowTarget.matches(":focus-within") || Boolean(glowTarget.querySelector(".stage-details[open]"));
    const strength = resting ? 0 : (quick ? 22 : glowTarget.matches(".stage-card") ? 12 : 8) * jellyStrength / 100;
    glowTarget.style.setProperty("--glow-x", `${((x + .5) * 100).toFixed(1)}%`);
    glowTarget.style.setProperty("--glow-y", `${((y + .5) * 100).toFixed(1)}%`);
    glowTarget.style.setProperty("--tilt-x", `${(-y * strength).toFixed(2)}deg`);
    glowTarget.style.setProperty("--tilt-y", `${(x * strength).toFixed(2)}deg`);
    glowTarget.style.setProperty("--drift-x", `${(x * (quick ? 8 : 3)).toFixed(1)}px`);
    glowTarget.style.setProperty("--drift-y", `${(y * (quick ? 5 : 2)).toFixed(1)}px`);
  });
});
document.addEventListener("focusin", (event) => {
  if (!jellyMotionEnabled || reducedMotion.matches) return;
  const target = event.target instanceof Element ? event.target.closest(motionSelector) : null;
  if (target && target !== glowTarget) enterSurfaceMotion(target);
});
document.addEventListener("focusout", (event) => {
  const target = event.target instanceof Element ? event.target.closest(motionSelector) : null;
  if (target && target === glowTarget && !target.matches(":hover") && !target.contains(event.relatedTarget)) releaseSurfaceMotion();
});
document.addEventListener("pointerleave", releaseSurfaceMotion);
document.addEventListener("pointercancel", releaseSurfaceMotion);
document.addEventListener("scroll", releaseSurfaceMotion, {capture:true, passive:true});
document.addEventListener("visibilitychange", () => { if (document.hidden) releaseSurfaceMotion(); });
window.addEventListener("blur", releaseSurfaceMotion);
window.addEventListener("resize", releaseSurfaceMotion);
reducedMotion.addEventListener("change", releaseSurfaceMotion);
finePointer.addEventListener("change", releaseSurfaceMotion);

const trailCanvas = $("#cursor-trail");
const trailContext = trailCanvas.getContext("2d");
let trailPoints = [];
let trailFrame = 0;
let lastTrailPoint = 0;
function sizeTrailCanvas() {
  const scale = Math.min(2, window.devicePixelRatio || 1);
  trailCanvas.width = Math.round(innerWidth * scale);
  trailCanvas.height = Math.round(innerHeight * scale);
  trailContext.setTransform(scale, 0, 0, scale, 0, 0);
}
function drawCursorTrail(now) {
  trailFrame = 0;
  trailContext.clearRect(0, 0, innerWidth, innerHeight);
  trailPoints = trailPoints.filter((point) => now - point.time < 850);
  if (!cursorTrailEnabled || !trailPoints.length) return;
  const age = (point) => Math.max(0, 1 - (now - point.time) / 850);
  const palette = scene === "forest" ? ["#8af3c0", "#c8ff89", "#54bda0"] : scene === "cosmos" ? ["#83d6ff", "#e8a0ff", "#8d8cff"] : ["#92e2ff", "#b8a1ff", "#74d6ec"];
  trailContext.lineCap = "round";
  trailContext.lineJoin = "round";
  if (cursorTrailStyle === "ribbon") {
    trailContext.beginPath();
    trailPoints.forEach((point, index) => index ? trailContext.lineTo(point.x, point.y) : trailContext.moveTo(point.x, point.y));
    const head = trailPoints.at(-1);
    if (trailPoints.length > 1) {
      const gradient = trailContext.createLinearGradient(trailPoints[0].x, trailPoints[0].y, head.x, head.y);
      gradient.addColorStop(0, `${palette[0]}00`);
      gradient.addColorStop(.52, `${palette[1]}bb`);
      gradient.addColorStop(1, `${palette[2]}ee`);
      trailContext.globalAlpha = age(head);
      trailContext.strokeStyle = gradient;
      trailContext.lineWidth = 11;
      trailContext.shadowColor = palette[1];
      trailContext.shadowBlur = 20;
      trailContext.stroke();
      trailContext.shadowBlur = 0;
      trailContext.globalAlpha = .45 * age(head);
      trailContext.strokeStyle = "#ffffff";
      trailContext.lineWidth = 2;
      trailContext.stroke();
    }
  } else if (cursorTrailStyle === "comet") {
    for (let index = 1; index < trailPoints.length; index++) {
      const start = trailPoints[index - 1], end = trailPoints[index];
      trailContext.globalAlpha = age(end);
      trailContext.strokeStyle = palette[index % palette.length];
      trailContext.lineWidth = 1.5 + age(end) * 7;
      trailContext.shadowColor = palette[0];
      trailContext.shadowBlur = 13;
      trailContext.beginPath(); trailContext.moveTo(start.x, start.y); trailContext.lineTo(end.x, end.y); trailContext.stroke();
    }
    trailContext.shadowBlur = 0;
  } else {
    trailPoints.forEach((point, index) => {
      const alpha = age(point);
      const bob = (now - point.time) / 24;
      trailContext.globalAlpha = alpha;
      trailContext.fillStyle = palette[index % palette.length];
      trailContext.shadowColor = trailContext.fillStyle;
      trailContext.shadowBlur = 9;
      trailContext.beginPath();
      trailContext.arc(point.x + Math.sin(bob + index) * 3, point.y - bob * 1.8, 1 + alpha * 3, 0, Math.PI * 2);
      trailContext.fill();
    });
    trailContext.shadowBlur = 0;
  }
  trailContext.globalAlpha = 1;
  if (trailPoints.length) trailFrame = requestAnimationFrame(drawCursorTrail);
}
document.addEventListener("pointermove", (event) => {
  if (!cursorTrailEnabled || event.pointerType === "touch") return;
  const now = performance.now();
  if (now - lastTrailPoint >= 16) {
    trailPoints.push({x:event.clientX,y:event.clientY,time:now});
    if (trailPoints.length > 48) trailPoints.splice(0, trailPoints.length - 48);
    lastTrailPoint = now;
  }
  if (!trailFrame) trailFrame = requestAnimationFrame(drawCursorTrail);
});
function clearCursorTrail() {
  trailPoints = [];
  if (trailFrame) cancelAnimationFrame(trailFrame);
  trailFrame = 0;
  trailContext.clearRect(0, 0, innerWidth, innerHeight);
}
window.addEventListener("resize", sizeTrailCanvas);
window.addEventListener("blur", clearCursorTrail);
reducedMotion.addEventListener("change", clearCursorTrail);
sizeTrailCanvas();

$("#jelly-motion-toggle").addEventListener("change", (event) => {
  const enabled = event.currentTarget.checked;
  if (!persistPersonalization("clearact-jelly-motion", enabled ? "on" : "off")) { event.currentTarget.checked = jellyMotionEnabled; return; }
  jellyMotionEnabled = enabled;
  renderAppearance();
  releaseSurfaceMotion();
});
document.querySelectorAll("[data-scene-choice]").forEach((button) => button.addEventListener("click", () => {
  const nextScene = button.dataset.sceneChoice;
  if (!persistPersonalization("clearact-scene", nextScene)) return;
  scene = nextScene;
  renderAppearance();
}));
$("#cursor-trail-toggle").addEventListener("change", (event) => {
  const enabled = event.currentTarget.checked;
  if (!persistPersonalization("clearact-cursor-trail", enabled ? "on" : "off")) { event.currentTarget.checked = cursorTrailEnabled; return; }
  cursorTrailEnabled = enabled;
  renderAppearance();
  if (!enabled) clearCursorTrail();
});
$("#cursor-trail-style").addEventListener("change", (event) => {
  const style = event.currentTarget.value;
  if (!persistPersonalization("clearact-cursor-trail-style", style)) return;
  cursorTrailStyle = style;
  renderAppearance();
  clearCursorTrail();
});
$("#wallpaper-input").addEventListener("change", async (event) => {
  await setWallpaper(event.currentTarget.files?.[0]);
  event.currentTarget.value = "";
});
$("#wallpaper-reset").addEventListener("click", async () => {
  const status = $("#personalization-feedback");
  wallpaperUploadSequence++;
  status.textContent = "正在恢复主题背景…";
  try {
    await removeStoredWallpaper();
    customWallpaper = "";
    renderAppearance();
  } catch (error) { status.textContent = error.message || "恢复默认壁纸失败。"; }
});
const rangeSettings = [
  {id:"theme-depth-range", key:"clearact-theme-depth", get:() => themeDepth, set:(value) => { themeDepth = value; }},
  {id:"glass-tint-range", key:"clearact-glass-tint", get:() => glassTint, set:(value) => { glassTint = value; }},
  {id:"jelly-strength-range", key:"clearact-jelly-strength", get:() => jellyStrength, set:(value) => { jellyStrength = value; }}
];
rangeSettings.forEach(({id, key, get, set}) => {
  const slider = $("#" + id);
  slider.addEventListener("input", () => { set(Number(slider.value)); renderAppearance(); });
  slider.addEventListener("change", () => {
    if (!persistPersonalization(key, String(get()))) { slider.value = String(get()); renderAppearance(); }
  });
});
document.addEventListener("pointerdown", (event) => {
  if (reducedMotion.matches || event.button !== 0 || !event.isPrimary) return;
  const target = event.target instanceof Element ? event.target.closest(".new-topic, .send-button, .save-button, .quick-card, .topic, .settings-button, .attach-button, .workspace-indicator, .secondary-button") : null;
  if (!target || target.disabled) return;
  const rect = target.getBoundingClientRect();
  const diameter = Math.max(rect.width, rect.height) * 2;
  const ripple = document.createElement("span");
  ripple.className = "tap-ripple";
  ripple.setAttribute("aria-hidden", "true");
  ripple.style.width = `${diameter}px`;
  ripple.style.height = `${diameter}px`;
  ripple.style.left = `${event.clientX - rect.left}px`;
  ripple.style.top = `${event.clientY - rect.top}px`;
  target.appendChild(ripple);
  ripple.addEventListener("animationend", () => ripple.remove(), {once:true});
  window.setTimeout(() => ripple.remove(), 800);
  if (target.matches(".new-topic, .send-button, .save-button, .quick-card")) {
    const distance = Math.min(48, Math.max(22, Math.min(rect.width, rect.height) * .48));
    for (let index = 0; index < 6; index++) {
      const angle = index * Math.PI / 3 + Math.PI / 6;
      const spark = document.createElement("span");
      spark.className = "tap-spark";
      spark.setAttribute("aria-hidden", "true");
      spark.style.left = `${event.clientX - rect.left}px`;
      spark.style.top = `${event.clientY - rect.top}px`;
      spark.style.setProperty("--spark-x", `${(Math.cos(angle) * distance).toFixed(1)}px`);
      spark.style.setProperty("--spark-y", `${(Math.sin(angle) * distance).toFixed(1)}px`);
      target.appendChild(spark);
      window.setTimeout(() => spark.remove(), 750);
    }
  }
});
document.addEventListener("click", (event) => {
  if (reducedMotion.matches) return;
  const target = event.target instanceof Element ? event.target.closest(".new-topic, .send-button, .save-button, .quick-card, .secondary-button") : null;
  if (!target || target.disabled || !target.isConnected) return;
  target.classList.remove("spring-pop");
  void target.offsetWidth;
  target.classList.add("spring-pop");
  window.setTimeout(() => target.classList.remove("spring-pop"), 550);
});
document.addEventListener("keydown", (event) => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); newTopic(); } });
$("#goal").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("#run-form").requestSubmit(); } });
$("#topic-form").onsubmit = async (event) => { event.preventDefault(); const action = topicAction; if (action.mode === "delete") { await json(`/api/runs/${action.id}`, {method:"DELETE"}); runEtags.delete(action.id); $("#topic-dialog").close(); if (activeId === action.id) newTopic(); else await loadRuns(); return; } await json(`/api/runs/${action.id}`, {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({title:$("#topic-input").value.trim()})}); $("#topic-dialog").close(); activeId === action.id ? await showRun(action.id) : await loadRuns(); };
$("#run-form").onsubmit = async (event) => { event.preventDefault(); if (sending) return; const input = $("#goal"); const draft = input.value; const submission = {goal:draft.trim() || (pendingFiles.length ? t("attachmentGoal") : ""), files:[...pendingFiles], workdir:selectedWorkdir, runId:activeId, language}; if (!submission.goal) return; const epoch = viewEpoch; const send = $(".send-button"); sending = true; send.disabled = true; try { const attachments = await uploadPendingFiles(submission.files, submission.workdir); const result = await json("/api/runs", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({goal:submission.goal,run_id:submission.runId,interface_language:submission.language,workdir:submission.workdir,attachments})}); if (viewEpoch !== epoch || activeId !== submission.runId) return; if (input.value === draft) input.value = ""; pendingFiles = pendingFiles.filter((file) => !submission.files.includes(file)); renderPendingFiles(); $("#feedback").textContent = ""; await showRun(result.run_id); } catch (error) { if (viewEpoch === epoch && activeId === submission.runId) $("#feedback").textContent = error.message; } finally { sending = false; send.disabled = false; } };
$("#settings-form").onsubmit = async (event) => { event.preventDefault(); const profiles = {}; document.querySelectorAll(".profile-card").forEach((card) => { const profile = {}; card.querySelectorAll("[data-key]").forEach((input) => { if (input.dataset.key === "apiKey" && !input.value.trim()) return; profile[input.dataset.key] = input.type === "number" ? Number(input.value) : input.value.trim(); }); profiles[card.dataset.profile] = profile; }); try { await json("/api/settings", {method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({interface_language:$("#interface-language").value,default_autonomy:$("#default-autonomy").value,max_iterations:Number($("#default-iterations").value),max_tool_calls:Number($("#default-tool-calls").value),default_profile:$("#default-profile").value,profiles,capability_rules:currentCapabilityRules()})}); $("#settings-feedback").textContent = language === "zh" ? "已保存" : "Saved"; } catch (error) { $("#settings-feedback").textContent = error.message; } };
$("#mcp-import-button").onclick = async () => { try { const result = await json("/api/mcp/import", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({config:JSON.parse($("#mcp-import").value)})}); $("#mcp-feedback").textContent = `已导入：${result.imported.join(", ")}`; $("#mcp-import").value = ""; await refreshSettings(); } catch (error) { $("#mcp-feedback").textContent = error.message; } };

let pollBusy = false;
(async () => {
  await loadConfig();
  await loadRuns();
  setInterval(async () => {
    if (!activeId || pollBusy) return;
    const id = activeId, epoch = viewEpoch;
    pollBusy = true;
    try { const result = await requestRunDetail(id, true); if (activeId === id && viewEpoch === epoch) { if (result.unchanged) refreshRunElapsed(); else { rememberRunEtag(id, result.etag); renderRun(result.detail); } } } catch (error) { if (activeId === id && viewEpoch === epoch) $("#feedback").textContent = error.message; } finally { pollBusy = false; }
  }, 1000);
  setInterval(() => loadRuns().catch(() => {}), 8000);
})().catch((error) => $("#feedback").textContent = error.message);
