/* ============================================================
   engine.js · 游戏引擎
   职责：渲染节点 / 处理选择 / 重新开始 / 本地存档与读取
   依赖：story.js 提供的全局 STORY；DOM 结构见 index.html
   存档：优先 localStorage；若浏览器禁用存储则退回内存存储，
        保证“存档 → 回标题 → 读档”在任何环境下可用。
   ============================================================ */

(function () {
  "use strict";

  var STORAGE_KEY = "cheese-case-save-v1";
  var memSave = null; // localStorage 不可用时的兜底存储

  /* ---------- DOM 引用 ---------- */
  var $ = function (id) { return document.getElementById(id); };
  var els = {
    title: $("screen-title"),
    story: $("screen-story"),
    dialog: $("dialog"),
    speaker: $("speaker"),
    text: $("story-text"),
    choices: $("choices"),
    sceneArt: $("scene-art"),
    sceneLabel: $("scene-label"),
    btnNew: $("btn-new"),
    btnLoad: $("btn-load"),
    btnSave: $("btn-save"),
    btnTitle: $("btn-title"),
    toast: $("toast"),
    titleMain: $("title-main"),
    titleSub: $("title-sub")
  };

  var state = { node: null };
  var toastTimer = null;

  /* ---------- 存储封装（带兜底） ---------- */
  function readSave() {
    try {
      var v = window.localStorage.getItem(STORAGE_KEY);
      if (v !== null) { return v; }
    } catch (e) { /* 存储被禁用 */ }
    return memSave;
  }

  function writeSave(value) {
    try {
      window.localStorage.setItem(STORAGE_KEY, value);
    } catch (e) {
      memSave = value;
    }
  }

  function hasSave() {
    return readSave() !== null && readSave() !== undefined;
  }

  /* ---------- 通用 UI ---------- */
  function toast(msg) {
    els.toast.textContent = msg;
    els.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { els.toast.hidden = true; }, 1700);
  }

  function showScreen(name) {
    els.title.hidden = name !== "title";
    els.story.hidden = name !== "story";
    var inStory = name === "story";
    els.btnSave.hidden = !inStory;
    els.btnTitle.hidden = !inStory;
  }

  function mkBtn(label, onClick, cls) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = cls || "btn choice-btn";
    b.textContent = label;
    b.addEventListener("click", onClick);
    return b;
  }

  function replayDialogAnimation() {
    els.dialog.classList.remove("enter");
    void els.dialog.offsetWidth; // 强制重排以重启动画
    els.dialog.classList.add("enter");
  }

  /* ---------- 画面渲染 ---------- */
  function renderTitle() {
    state.node = null;
    els.titleMain.textContent = STORY.meta.title;
    els.titleSub.textContent = STORY.meta.subtitle;
    els.btnLoad.disabled = !hasSave();
    els.btnLoad.textContent = hasSave() ? "📜 读取存档" : "📜 读取存档（暂无）";
    document.body.className = "scene-title";
    showScreen("title");
  }

  function renderNode(id) {
    var node = STORY.nodes[id];
    if (!node) {
      toast("⚠️ 找不到剧情节点：" + id);
      return;
    }
    var scene = STORY.scenes[node.scene] || {};
    state.node = id;

    document.body.className = "scene-" + (node.scene || "title");
    els.sceneLabel.textContent = scene.label || "";
    els.sceneArt.textContent = scene.emoji || "";
    els.speaker.textContent = node.speaker || "旁白";
    els.text.textContent = node.text || "";

    var box = els.choices;
    box.innerHTML = "";

    if (node.ending) {
      var badge = document.createElement("div");
      badge.className = "ending-badge";
      badge.textContent = "🏆 " + (node.endingTitle || "结局");
      box.appendChild(badge);

      var again = mkBtn("⚔️ 重新开始", startNew, "btn primary");
      var home = mkBtn("🏰 回到标题", renderTitle, "btn primary");
      box.appendChild(again);
      box.appendChild(home);
    } else {
      (node.choices || []).forEach(function (c) {
        box.appendChild(mkBtn("➤ " + c.text, function () { choose(c.to); }));
      });
    }

    replayDialogAnimation();
    showScreen("story");
  }

  /* ---------- 游戏流程 ---------- */
  function choose(to) {
    renderNode(to);
  }

  function startNew() {
    renderNode(STORY.meta.startNode || "start");
  }

  /* ---------- 存档 / 读档 / 重新开始 ---------- */
  function save() {
    if (!state.node) { return; }
    writeSave(JSON.stringify({
      node: state.node,
      version: STORY.meta.version || "1.0",
      ts: Date.now()
    }));
    renderTitleRefreshLoadBtn();
    toast("💾 已存档：" + currentEndingName());
  }

  function load() {
    var raw = readSave();
    if (raw === null || raw === undefined) {
      toast("📭 还没有存档");
      return;
    }
    var data;
    try {
      data = JSON.parse(raw);
    } catch (e) {
      toast("⚠️ 存档已损坏，无法读取");
      return;
    }
    if (!data || !data.node || !STORY.nodes[data.node]) {
      toast("⚠️ 存档已损坏，无法读取");
      return;
    }
    renderNode(data.node);
    toast("📜 已读取存档，回到：" + (STORY.nodes[data.node].endingTitle || "剧情中"));
  }

  function currentEndingName() {
    var n = STORY.nodes[state.node];
    return n && n.ending ? n.endingTitle : "剧情进行中";
  }

  function renderTitleRefreshLoadBtn() {
    els.btnLoad.disabled = !hasSave();
    els.btnLoad.textContent = hasSave() ? "📜 读取存档" : "📜 读取存档（暂无）";
  }

  /* ---------- 事件绑定 ---------- */
  els.btnNew.addEventListener("click", startNew);
  els.btnLoad.addEventListener("click", load);
  els.btnSave.addEventListener("click", save);
  els.btnTitle.addEventListener("click", renderTitle);

  /* ---------- 启动 ---------- */
  renderTitle();
})();
