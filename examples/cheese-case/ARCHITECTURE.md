# 架构文档 · 地下室奶酪失踪案

## 总览

一个零依赖、纯前端的视觉小说（Visual Novel）。四个文件各司其职：

```
project/
├── index.html      页面结构（静态骨架，不含逻辑）
├── styles.css      全部样式与动画
├── js/
│   ├── story.js    剧情数据（纯数据，零逻辑）
│   └── engine.js   游戏引擎（渲染 / 流程 / 存档）
├── README.md       运行与玩法说明
└── ARCHITECTURE.md 本文档
```

## 模块职责

### 1. `index.html`（表现层骨架）
- 定义三个区域：
  - `#screen-title` 标题画面：游戏标题、副标题、「开始新游戏 / 读取存档」按钮；
  - `#screen-story` 剧情画面：`#scene-art` 场景表情画、`#speaker` 说话人、
    `#story-text` 正文、`#choices` 选项容器；
  - `#toast` 底部状态提示（存档成功、读档失败等）。
- 顶部 HUD：`#scene-label` 场景名 + 「💾 存档 / 🏰 回到标题」按钮（仅剧情中显示）。
- 以 `<script>` 顺序引入 `story.js` → `engine.js`，保证数据先于引擎加载。
- 不含任何内联脚本或远程资源，完全离线。

### 2. `styles.css`（视觉层）
- 设计语言：中世纪羊皮纸 + 城堡暗色场景底色，衬线字体栈，零外部字体。
- 场景切换：引擎改写 `body` 的 class（`scene-title / scene-basement / scene-hall /
  scene-kitchen / scene-cellar`），对应不同的径向渐变背景。
- 组件样式：对话框（双线边框 + 入场动画 `rise`）、选项按钮（悬停位移）、
  结局徽章 `ending-badge`、通用按钮 `.btn.primary / .btn.ghost`、Toast 动画。
- 响应式：`max-width: 780px` 居中布局，小屏自动缩小场景画。

### 3. `js/story.js`（领域数据层）
- 暴露全局常量 `STORY`：
  - `meta`：标题、副标题、`startNode`、版本号；
  - `scenes`：场景 key → `{ label, emoji }`，决定 HUD 场景名与场景画；
  - `nodes`：剧情节点图。每个节点含 `scene / speaker / text`，
    普通节点带 `choices: [{ text, to }]`，结局节点带 `ending: true` 与 `endingTitle`。
- **无逻辑、无 DOM、无副作用**：改剧情只改这个文件；引擎对节点字段做容错
  （缺字段时显示占位），因此数据格式错误不会白屏。

### 4. `js/engine.js`（逻辑层）
- 立即执行函数（IIFE），不污染全局，唯一对外依赖是 `STORY`。
- 核心函数：
  - `renderTitle()`：显示标题画面，并按“是否有存档”启用/禁用读档按钮；
  - `renderNode(id)`：按 `STORY.nodes[id]` 渲染场景、说话人、正文，
    动态生成选项按钮或“结局徽章 + 重新开始/回到标题”按钮；
  - `choose(to) → renderNode`：推进剧情；
  - `startNew()`：从 `meta.startNode` 重新开局；
  - `save()`：把 `{ node, version, ts }` 序列化为 JSON 写入存储，Toast 反馈；
  - `load()`：读回 JSON，校验节点存在后 `renderNode`，损坏时给出提示；
  - `toast(msg)` / `showScreen(name)`：状态提示与画面切换。
- 存储策略：优先 `localStorage`（键 `cheese-case-save-v1`）；
  读写抛异常时自动退回内存变量 `memSave`，保证沙箱/禁用存储环境下
  “存档 → 回标题 → 读档”仍可用。

## 数据流

```
        ┌──────────────────────────────────────────────┐
        │                 用户点击 (DOM 事件)            │
        └───────────────────────┬──────────────────────┘
                                ▼
  选择选项 ───────────► engine.choose(to) ─► engine.renderNode(id)
  存档按钮 ───────► engine.save()   ─► 存储 (localStorage / 内存兜底)
  读档按钮 ───────► engine.load()   ─► 存储 → 校验 → renderNode(id)
  重新开始 ───────► engine.startNew() ─► renderNode(meta.startNode)
                                │
                                ▼
                    读 STORY.nodes[id] / STORY.scenes[scene]
                    （story.js 提供的纯数据）
                                │
                                ▼
        更新 DOM：body 场景 class、场景画、说话人、正文、选项按钮
                                │
                                ▼
                    styles.css 依据 body class / 组件类渲染外观
```

## 依赖关系

| 模块 | 依赖 |
| --- | --- |
| `index.html` | `styles.css`、`js/story.js`、`js/engine.js` |
| `styles.css` | 无 |
| `js/story.js` | 无 |
| `js/engine.js` | `js/story.js`（全局 `STORY`）、浏览器 DOM 与存储 API |

无任何第三方库、无网络请求、无构建步骤。

## 扩展点

- **加剧情**：在 `story.js` 的 `nodes` 里新增节点并在某处 `choices` 里指向它即可，
  引擎与样式零改动；新场景只需在 `scenes` 加一条并在 `styles.css` 加一个
  `scene-xxx` 背景类。
- **改存档**：`save()/load()` 中只需调整 JSON 结构并递增 `meta.version`。
- **加功能**（打字机效果、背景音乐等）：都在 `engine.js` 内实现，
  保持 `story.js` 纯数据、`styles.css` 纯样式的边界。
