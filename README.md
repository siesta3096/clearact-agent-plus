# ClearAct

ClearAct 是一个本地优先、可审查的轻量 Agent Runtime。模型只能**提出**工具调用；运行时负责将每一步的作用范围、风险、确认结果、快照与事件清楚地呈现和记录。它的设计原则是 **透明，而非保守**：用户明确选择本次工作目录、自动化程度与预算，Runtime 再在该授权范围内执行。

## 已实现能力

- OpenAI-compatible API 与 Ollama 的原生工具调用循环。
- 本地文件工具：列目录、读 UTF-8 文本、写 UTF-8 文本；作用范围为用户为**本次运行**选择的任意已有目录，默认是 `workspace/`。
- 联网工具：网页搜索、HTTP(S) URL 获取；`localhost` / `127.0.0.1` 默认可访问，可在根目录 `clearact.json` 关闭。
- 白 / 绿 / 黄 / 红风险分级；默认自动执行到 yellow。选择 red 时同样完整保留审计记录；风险提示是可见的决策信息，而非隐式限制。
- 写入前快照，可用 CLI 恢复；运行、事件和 checkpoint 均保存在 `data/`。
- 默认 80 次迭代 / 240 次工具调用 / 60 秒单工具超时，均可配置或按运行覆盖，适合较大的任务。
- **MCP 客户端接入**：支持本地 `stdio` 与远程 Streamable HTTP 服务；每次运行自动发现 MCP tools，并纳入同一套模型工具循环、超时、风险评估、审批、事件与审计记录。

## 安装

在全新的 Python 环境中安装项目（安装后才会注册 `clearact` 命令）：

```powershell
cd clearact-agent-plus
python -m pip install -e ".[dev]"
```

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

- `defaultProfile` 与 `profiles`：模型、服务商、`baseUrl`、`apiKey`、上下文窗口，以及可选的 `apiKeyEnv`；
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

也可以使用 `clearact web` 仅启动服务、不自动打开浏览器。Web 控制台包含新建话题、持久化的历史话题侧边栏和设置中心；设置中心可调整默认自动化等级、最大迭代/工具调用次数，以及各模型 Profile 的服务商、模型、接口地址和上下文窗口。API Key 不会传递到浏览器，仍应在 `.env` 或 `clearact.json` 管理。服务只绑定到 `127.0.0.1`，不会暴露给局域网。

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

- 启动一个任务时，ClearAct 连接启用的服务并调用 `tools/list`；发现的工具会以 `mcp__服务名__工具名` 注册，避免与内置工具冲突。
- 服务发现默认最多等待 15 秒；可在单个服务中设置 `connectTimeoutSeconds`（大于 0 且不超过 300）。超时或连接失败只会跳过该服务，不会阻塞整个任务。
- MCP 工具默认评估为 **yellow**：默认 yellow/red 可自动执行，green 需要用户审批；关闭“联网”策略会拒绝 MCP 调用。由于远程工具的真实副作用无法由客户端可靠推断，生产环境建议先用 green 审批模式验证新服务。
- 当前支持无交互的 stdio 与 Streamable HTTP 认证（通过静态 `env` / `headers`）；暂未实现 OAuth/DCR、浏览器登录、SSE 旧传输、MCP resources/prompts/sampling，以及跨运行的持久连接池。

## 审批与恢复

默认 autonomy 为 `yellow`：创建和覆盖文件可自动执行。可通过 `--autonomy white|green|yellow|red` 调整自动执行的最大等级。所有动作都会被记录并可在 `history` / `inspect` 或 Web 控制台中审查。

```powershell
clearact history --limit 20
clearact inspect run_xxxxxxxxxxxx
clearact restore snapshot_xxxxxxxxxxxx
```

快照记录的是一次写入前的状态和该次运行选择的实际目录：恢复已有文件会写回原内容；恢复原本不存在的文件会删除该次创建的文件。

## 验证

```powershell
$env:PYTHONPATH = "$PWD/src"
python -m pytest -q
python -m ruff check src tests
```

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
