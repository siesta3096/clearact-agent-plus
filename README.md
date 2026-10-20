# ClearAct

ClearAct 是一个本地优先、可审查的轻量 Agent Runtime。模型只能**提出**工具调用；运行时负责将每一步的作用范围、风险、确认结果、快照与事件清楚地呈现和记录。它的设计原则是 **透明，而非保守**：用户明确选择本次工作目录、自动化程度与预算，Runtime 再在该授权范围内执行。

**简洁、轻量、上下文友好**是产品的基本要求：小任务直接执行，规划按需使用；完整执行记录留在本地，模型请求使用精简的工作上下文。任务的文件结构、文档与检查规模随需求决定。完整原则见 [设计原则](docs/DESIGN.md)。

项目的完整迭代过程、问题修复、验证结果和当前边界见 [完整改进记录](docs/IMPROVEMENTS.md)。

## 已实现能力

- **开发与交付模式**：从第一轮检查目录并实际实现项目，支持代码搜索、精确编辑、只读语法检查和本地浏览器交互验证。
  文件结构与文档按需求决定；没有真实文件或修改后检查不能显示完成。结果提供文件清单、变化预览、
  对应轮次 ZIP、验证截图与先前交付记录。实现结构和验证边界见 [v0.3.0 开发能力升级](docs/development-agent-2026-10-18.md)。
- OpenAI-compatible API 与 Ollama 的原生工具调用循环。
- 原生 Word / Excel 生成：Word 支持标题、段落、列表、表格和页码；Excel 支持多工作表、原生表格、筛选、冻结首行、数字/日期格式、公式和柱状/折线图。生成文件可在结果卡片下载；覆盖前保存原始字节，回溯时保护用户后续修改。当前是结构化生成器，不是现有复杂 Office 文件的保真编辑器；Excel 公式由 Excel 打开时重新计算。
- 通用命令执行：传入可执行程序和参数，在本次工作目录运行，记录输出、错误、退出码与超时。每次执行必须确认；命令无法自动回溯，也不具备文件/网络沙箱隔离。关闭相关能力或明确要求离线时，通用命令不会执行。
- 本地文件工具：列目录、读 UTF-8 文本、写 UTF-8 文本；作用范围为用户为**本次运行**选择的任意已有目录，默认是 `workspace/`。
- Web 输入框支持一次附加最多 8 个文件或图片；附件安全复制到当前工作区并记入运行记录，图片会按 OpenAI-compatible 或 Ollama 的多模态格式交给支持视觉的模型。
- 联网工具：网页搜索、HTTP(S) URL 获取；`localhost` / `127.0.0.1` 默认可访问，可在根目录 `clearact.json` 关闭。
- 支持贴合目标的动态阶段计划；开发任务可直接操作文件，计划用于说明进度。阶段详情可检查资料、文件与工具结果。
- 新证据改变任务方向时，Agent 可说明原因并重排尚未开始的阶段；已执行阶段保持原样，界面会显示改计划次数和最近一次原因。
- 执行中会复用已读取的文件片段与相近检索；若同一轮重复读取、读取已覆盖的文件区间、近似搜索，或旧结果已被上下文压缩移出，会把先前的实际结果重新交给模型，而不是只提示“已跳过”。超出文件末尾的区间不会被误判为已覆盖。按任务选择“仅本地、本地优先、混合、调查”等资料策略，限制无关搜索并先消化指定附件。Web 任务页显示采用的策略、用时和检索次数。
- 阶段详情可预览当时的文件读取结果，并查看每次写入相对于执行前快照的差异；预览仅开放给该任务已成功执行、且位于其授权工作区内的文件操作。
- 最终结果旁可展开“已读取资料”，快速回看成功打开的文件和网页；该清单只代表读取记录，不冒充逐条结论的引用。
- 计划可附带文件、工具成功次数和实际打开网页数量三类验收条件。运行时依据本轮真实记录核对，展示逐项结果；未满足的条件保留为“需核对”，避免仅凭模型答复标记完成。Word/Excel 核对包含核心文件结构，不代表排版或公式结果已经验收。
- 任意已执行阶段都可附带反馈重新开始：确认前会预览保留/重做的阶段、可恢复的文件和风险提示；此前阶段的消息和结果直接复用，旧分支记录仍可查看。若保留阶段读取过的本地文件后来发生变化，或旧记录缺少版本信息，预览会提示，重跑时也会要求模型重新读取。只有文件内容仍与 Agent 上次写入一致时才按快照恢复，避免覆盖用户之后的修改；无法撤销的外部副作用会明确提示。
- 权限按“本地读取、联网读取、工作区创建/修改、MCP 只读/外部操作、工作区外写入、高影响操作”分别设为自动执行、先询问或关闭；颜色阈值仅为旧配置兼容层。
- 写入前快照，可用 CLI 恢复；运行、事件和 checkpoint 均保存在 `data/`。
- 默认 80 次迭代 / 240 次工具调用 / 60 秒单工具超时，均可配置或按运行覆盖，适合较大的任务。
- **MCP 客户端接入**：支持本地 `stdio` 与远程 Streamable HTTP 服务；设置页可搜索官方 MCP Registry 中可直接连接的远程服务，选择后自动填写地址，再确认保存并测试；也支持手动添加和 JSON 导入。Registry 收录不等于安全审核，需要登录或密钥的服务仍须用户授权。

## 最近更新（2026-10-20）

2026-10-20（0.3.2）：重新明确简洁、轻量和上下文友好的设计原则。取消普通任务的强制规划往返，默认不重发历史思考，提前整理旧过程消息，缩短通用提示词；新项目不再一律要求多个源文件、架构文档和浏览器交互。保留实际文件、修改后检查和用户明确要求的验收。对照请求体积及验证边界见 [轻量化维护记录](docs/maintenance-2026-10-20-lightweight.md)。

2026-10-20（0.3.1）：修复长思考返回后，组装下一轮请求时因上下文预算超限而失败的问题。超限时只在请求副本中精简已返回思考，完整原始记录仍保留；必要内容仍放不下时明确标记“需核对”，保留已完成操作。状态追问使用只读核查，避免重新开始规划。原失败记录回放、真实文件及浏览器交互回归见 [故障与修复记录](docs/maintenance-2026-10-20-context.md)。

2026-10-18（0.2.4）：修复长时间生成时 Agent 提前超时、后台却继续输出的问题。模型读取等待默认由 90 秒改为 600 秒，可在设置中按模型调整；读取超时后不再自动重发同一生成请求。界面展示等待状态，收到答复后继续执行。超过旧上限的实际 HTTP 验证见 [等待超时修复记录](docs/maintenance-2026-10-18-timeout.md)。

2026-10-18（0.2.3）：修复“从此阶段重新执行”在反馈留空时点击无响应。反馈改为可选，留空按原要求重试，仍先展示范围预览并等待确认；增加检查状态、按钮旁错误提示与防重复提交。刷新后的真实浏览器流程验证见 [重跑按钮修复记录](docs/maintenance-2026-10-18-restart.md)。

2026-10-18（0.2.2）：修复本地模型在计划生成后因系统提示位置不兼容而返回 HTTP 400 的问题。OpenAI-compatible 和 Ollama 请求在发送前将系统提示合并到开头，保留用户/工具顺序和原始记录；补充具体错误提示，并明确规划后应开始哪个阶段。问题复现及实际本地模型验证见 [本轮修复记录](docs/maintenance-2026-10-18.md)。

2026-10-16（0.2.1）：加入可执行的产物验收和独立的“需核对”任务状态。模型声称完成但缺少文件、Office 格式不符、命令失败或实际来源不足时，会留下明确的验收记录；有预算时允许一次补做，满足后才标为完成。单纯的失败/拒绝记录不会额外触发补做请求。续聊按新一轮独立核对。实现、验证和限制见 [本轮维护说明](docs/maintenance-2026-10-16.md)。

2026-10-13（0.2.0）：加入通用命令与原生 Word/Excel 生成器，补齐二进制快照、产物下载和安全回溯。修复浏览器四类动作、常见禁止联网表达与长会话中间要求丢失。网关重启会把遗留任务标为“已中断”，保留成功结果，提示核查命令或外部副作用后手工继续；不会自动重放未完成操作。详情与固定验证入口见 [本轮维护说明](docs/maintenance-2026-10-13.md)。

2026-10-02：加入执行中的动态改计划能力。模型可依据新证据替换后续阶段，不丢失已经执行的步骤；改动原因和被替换的阶段会保存在任务记录中。初次规划工具在计划生成后不再占用模型输入预算，改计划工具也只在仍有待执行阶段时提供。

2026-10-01：阶段计划新增可见的完成条件；模型过早结束或刚写出的文件缺失时，会触发一次有界的完成核对，并在仍未满足时保留提示。阶段详情展示操作结果、权限依据和工具返回；跳过、策略拒绝及用户拒绝也会留下带时间与风险的事件。若一个阶段的工具操作全部失败或被阻止，阶段及历史话题会标为“需核对”，不再误写“已完成”。多轮对话会突出显示本轮请求，旧请求可展开查看；结果卡片只展示最新请求的答复，不再误显示上一轮结果。回溯前可预览影响，回溯后可查看旧分支完整记录，且不会覆盖用户在运行结束后自行修改的文件。

- **任务执行更有针对性**：根据任务选择本地、优先读取本地资料或联网调查等策略；复用已有文件读取和检索结果，并拦截重复搜索、重复读取与无关网页查询。任务详情可查看采用的策略、执行耗时和检索次数。
- **阶段检查与回溯更完整**：阶段详情增加文件内容预览和写入差异；从阶段附带反馈重新执行时，继续复用此前未修改的阶段结果。
- **外观个性化**：设置内可切换阳光/黑暗模式以及晶透玻璃、森林和深空主题；主题颜色深浅会联动整套界面。玻璃效果提供从清透叠层到浓郁染色的调节，果冻跟手可关闭并调整强度，光标拖尾可单独开关和选择样式。
- **壁纸更换与保存**：支持上传自定义背景并保存在本机浏览器；图片使用独立的浏览器存储，避免 `localStorage` 容量限制影响重复更换，并兼容迁移旧版已保存的壁纸。
- **MCP 服务发现**：设置页可搜索官方 MCP Registry 的远程服务并自动填入连接地址；连接凭据和授权仍由用户确认。
- **共享 Agent 浏览器**：对话页可单独启动隔离的浏览器侧栏；用户和 Agent 共用同一页面，侧栏支持网址导航、点击、滚动和键盘输入。只读浏览沿用联网读取权限，点击和输入默认先询问；密码框不可由 Agent 填写，下载默认关闭。

## 安装

在全新的 Python 环境中安装项目（安装后才会注册 `clearact` 命令）：

```powershell
cd clearact-agent-plus
python -m pip install -e ".[dev]"
```

Windows 使用已安装的 Microsoft Edge 运行隔离浏览器；macOS / Linux 需要额外安装 Playwright Chromium：`python -m playwright install chromium`。

如果当前终端仍然找不到 `clearact`，可以直接使用项目自带的启动脚本：

```powershell
.\\clearact.cmd gateway
```

或者双击项目根目录中的 `启动ClearAct网关.bat`。它会自动设置 `PYTHONPATH`，不依赖 PATH 中是否存在命令行入口。

首次使用请复制配置模板：`Copy-Item clearact.json.example clearact.json`。然后编辑根目录的 `clearact.json` 选择 `defaultProfile`。云端 API Key 推荐写入根目录 `.env`，不要写入 JSON；每个 profile 都可独立设置模型、接口地址和上下文窗口；本地 Ollama 无需 Key。

`apiKeyEnv` 指定环境变量名；系统环境变量或 `.env` 文件变量优先于 JSON 中的旧式 `apiKey` 字段，适合 Docker、CI，也避免把密钥写入仓库。

```powershell
# 本地 Ollama 示例（先确认 ollama serve 已运行且模型已下载）
clearact run "在 output/hello.txt 创建一段问候语" --profile ollama-local

# 将本次文件授权范围改为你选择的已有目录，并提高任务预算
clearact run "分析项目并生成改造报告" --workdir E:\\projects\\demo --max-iterations 200 --max-tool-calls 1000 --autonomy red

# 选择云端 OpenAI-compatible profile
clearact run "总结 workspace/inbox 中的文件" --profile deepseek
```

也可以不安装入口脚本，开发时使用：

```powershell
$env:PYTHONPATH = "$PWD/src"
python -m clearact.cli run "列出当前工作区文件"
```

## 配置、启动与控制台

运行配置集中在根目录唯一的 `clearact.json`，无需在多个文件间跳转：

- `defaultProfile` 与 `profiles`：模型、服务商、`baseUrl`、`apiKey`、上下文窗口，以及可选的 `apiKeyEnv`、`requestTimeoutSeconds`；后者为模型无数据读取等待上限，默认 600 秒，可设 1—7200 秒，也可在设置页按模型修改。它与单次工具超时独立；
- `workspace.defaultRoot`：未指定 `--workdir` 时的默认目录；
- `network.allowLocalhost`：是否允许 Agent 访问 `localhost`、`127.0.0.1` 和 `::1`；默认 `true`；
- `agent.maxIterations`、`agent.maxToolCallsPerRun`、`agent.toolTimeoutSeconds`：全局任务预算；
- `policy.defaultAutonomy`：默认自动化阈值；
- `web.host`、`web.port`：本地控制台监听地址与端口。

`config/tools.yaml` 和 `config/risk_rules.yaml` 仍保留为 Runtime 内部的工具展示/风险规则，不需要为了连接模型而编辑它们。

启动本地浏览器控制台（类似 `nanobot gateway`，会自动打开浏览器）：

```powershell
clearact gateway
# 自动打开 http://127.0.0.1:8787
```

也可以使用 `clearact web` 仅启动服务、不自动打开浏览器。Web 控制台包含新建话题、持久化的历史话题侧边栏和设置中心。常用界面优先展示按能力划分的权限与 MCP 连接管理；模型 Profile 和运行预算收在高级设置中。API Key 不会在设置 API 中回显。服务只绑定到 `127.0.0.1`，不会暴露给局域网。

对话顶部的 **浏览器** 按钮可随时启动或关闭一个独立的浏览器侧栏。启动后，用户可以在侧栏中直接浏览；Agent 也能在同一会话中查看页面、导航、点击和输入。日常权限下，改变网页状态的点击/输入会先弹出确认；“信任”权限可按设置自动执行。浏览器页面内容仍视为不可信来源，Agent 不会代填密码，也不会下载文件；付款、发送、删除等可识别的高影响按钮始终需要明确确认。是否允许访问本机地址仍由 `network.allowLocalhost` 控制。

## MCP 服务接入

ClearAct 兼容常见客户端的 `mcpServers` JSON 结构。可在 Web **设置 → MCP Servers** 粘贴导入；服务配置会写入本机 `clearact.json`，其中 `env` 和 HTTP `headers` 的值只保存在本地，设置页和 API 响应仅显示键名，不回显密钥。

也可以手工在 `clearact.json` 使用规范化结构：

```json
{
  "mcp": {
    "servers": {
      "filesystem": {
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "E:\\\\your-workspace"],
        "env": { "OPTIONAL_TOKEN": "${MCP_TOKEN}" }
      },
      "remote-service": {
        "transport": "streamable_http",
        "url": "https://example.com/mcp",
        "headers": { "Authorization": "Bearer ${REMOTE_MCP_TOKEN}" }
      }
    }
  }
}
```

- 启动一个任务时，ClearAct 并行连接所有启用的服务并调用 `tools/list`；发现的工具会以 `mcp__服务名__工具名` 注册，避免与内置工具冲突。
- 服务发现默认最多等待 15 秒；可在单个服务中设置 `connectTimeoutSeconds`（大于 0 且不超过 300）。超时或连接失败只会跳过该服务，不会阻塞整个任务。
- MCP 的 `readOnlyHint` / `destructiveHint` 会进入权限判断：只读工具可单独自动放行，可能修改外部状态的工具默认先询问，破坏性工具始终保持显式控制。未提供注解的服务按外部修改处理。
- 当前支持无交互的 stdio 与 Streamable HTTP 认证（通过静态 `env` / `headers`）；暂未实现 OAuth/DCR、浏览器登录、SSE 旧传输、MCP resources/prompts/sampling，以及跨运行的持久连接池。

## 审批与恢复

Web 默认“日常”策略会自动执行只读操作及工作区内的创建/修改；MCP 外部修改、工作区外写入和高影响动作先询问。CLI 仍可通过 `--autonomy white|green|yellow|red` 使用兼容的阈值模式。所有动作都会被记录并可在 `history` / `inspect` 或 Web 控制台中审查。

```powershell
clearact history --limit 20
clearact inspect run_xxxxxxxxxxxx
clearact restore snapshot_xxxxxxxxxxxx
```

快照记录的是一次写入前的状态和该次运行选择的实际目录：恢复已有文件会写回原内容；恢复原本不存在的文件会删除该次创建的文件。

## 验证

浏览器回归在 Windows 使用已安装的 Edge；其他系统先运行 `python -m playwright install --with-deps chromium`。
JavaScript 语法检查与样例测试需要安装 Node.js。CI 会准备这些依赖。

```powershell
$env:PYTHONPATH = "$PWD/src"
python -m pytest -q
python -m ruff check src tests scripts
```

固定的任务执行场景可独立运行，覆盖真实文件生成、命令审批和工具失败记录；模型响应固定，不调用付费模型：

```powershell
$env:PYTHONPATH = "$PWD/src"
python -m pytest tests/integration/test_task_scenarios.py tests/unit/test_artifact_web.py tests/unit/test_command_policy.py -q
```

真实模型生成的可试玩项目与脱敏评估记录见 [开发样例](examples/README.md)。
独立本地模型评估方法、模块结构、交付标准与已知限制见 [v0.3.0 开发模式记录](docs/development-agent-2026-10-18.md)。

## 命令与 Office 文件

在 Web 控制台直接描述目标，例如：

- “根据附件生成带标题和表格的 Word 报告，保存为 output/report.docx。”
- “生成费用 Excel，保留合计公式，并添加柱状图，保存为 output/budget.xlsx。”
- “运行项目测试，说明失败原因。”命令执行前会展示参数并等待确认。

Office 工具沿用工作区创建/修改权限。结果区提供下载链接，文件已被修改时会拒绝下载旧版本记录，避免冒充原始产物。生成文件及被覆盖文件限 32 MB。

命令工具接收参数数组，不隐式拼接 shell。需要 shell 语法时显式调用 PowerShell、cmd 或 bash。
工作目录限制只决定起始位置，命令仍以本机用户身份访问文件、环境和网络；它不是隔离容器。
普通子进程在超时、停止和命令结束时会清理；不提供后台服务托管。POSIX 主动脱离进程组的守护进程不在此保证内。
全局单工具超时与命令请求的超时取较小值，并留出清理进程的时间。权限页可关闭命令，信任模式仍会先确认。

同一数据目录由一个进程管理：网关、CLI 执行和快照恢复共用系统文件锁，另一个进程占用时会明确拒绝，避免重启对账误伤仍在运行的任务。网关内部的多任务与续接可以共用锁；不支持多进程同时写入或自动断点重放。

## 项目结构

- `src/clearact/runtime/`：Agent 循环、风险、策略、审批、状态与 checkpoint。
- `src/clearact/tools/`：受控工具实现。
- `src/clearact/providers/`：模型适配器。
- `src/clearact/storage/`：运行记录、事件、快照和产物存储。
- `clearact.json`：唯一的用户运行配置（模型/API、工作目录、预算、策略和 Web 地址）。
- `config/`：Runtime 内部的工具展示信息与风险规则。
- `workspace/`：默认唯一可读写的业务工作区。

## 安全提示

请勿提交 `.env`、真实 API Key、运行记录或快照。`red` 模式会扩大文件操作范围，仅应对可信任务使用；远程 MCP 服务也应先在审批模式下验证。

## 架构概览

```text
用户目标
   │
   ▼
CLI / Web Console
   │
   ▼
AgentRunner ── ContextBuilder ── LLM Provider
   │
   ├── PolicyEngine + RiskEvaluator + ApprovalGate
   ├── ToolExecutor ── Filesystem / Web / MCP
   └── RunStore + EventBus + Checkpoints + Snapshots
```

模型只负责提出结构化工具调用；权限判断、工具执行、超时、事件记录和快照由 Runtime 完成。网页内容和 MCP 返回值始终作为不可信的参考数据处理。

## 环境诊断

安装依赖后可以运行：

```powershell
clearact doctor
```

它会检查 Python 版本、配置文件、默认 workspace、数据目录、默认 Profile，以及云端 Profile 的 Key 是否可用；不会主动调用模型或联网。

## Roadmap

- [x] 本地优先的可审查 Agent Runtime
- [x] 文件、网页和 MCP 工具接入
- [x] 风险分级、审批、事件记录和写入前快照
- [x] CLI 与本地 Web Console
- [ ] 更完整的 MCP 连接状态与 OAuth 支持
- [ ] 可恢复的后台任务与跨重启任务管理
- [ ] 更丰富的 Provider 能力检测和流式输出
- [ ] 英文文档与发布包

## 贡献

欢迎提交 Issue 和 Pull Request。涉及工具权限、SSRF、密钥处理、MCP 隔离或审计记录的改动，请同时补充测试，并说明威胁模型和兼容性影响。
