# ClearAct 0.2.0 本轮维护说明

日期：2026-10-13。本轮基于先前审查的具体问题继续维护，并增加可复用的命令和 Office 工具。这里记录实现和验证，发布状态以仓库提交和版本标签为准，不把固定模型测试当成真实模型成功率。

本地最终验证：**348 项测试全部通过（18.69 秒）**，Ruff 检查、前端 JavaScript 语法检查和差异检查通过；真实 Edge 浏览器检查通过。相较修改前 203 项测试，本轮补充了 145 项参数化及场景验证。已移除旧 FastAPI 生命周期写法，本次整套运行没有此前的弃用警告。

## 本轮解决的问题

| 原问题 | 当前行为 | 固定验证 |
| --- | --- | --- |
| Agent 滚动、按键、后退、刷新重复传入 action | 正确转发动作和参数，测试实际调用会话方法 | `tests/unit/test_computer_use.py` |
| 常见“别联网”“不要浏览网页”被误判 | 识别直接禁止联网指令，保持中途禁网要求；引用和条件句单独处理 | `tests/unit/test_research_strategy.py` |
| 压缩丢掉中途用户要求 | 保留所有用户请求、系统指令和最新完整工具事务；必要内容超限明确报错 | `tests/unit/test_context_budget.py` |
| 重启后仍显示运行/待审批，实际没有任务 | 启动扫描遗留记录，保留已有成功结果，补齐未结束工具结果，标为 interrupted；原审批不重放；系统锁阻止误处理另一个活动进程 | `tests/unit/test_web_recovery.py`、`test_data_lease.py` |
| 新生成文件缺少原生格式和恢复链路 | 生成 DOCX/XLSX，保存原始字节、原子替换、核对哈希后下载和回溯 | `tests/unit/test_office.py`、`test_artifact_web.py` |
| 文档渲染期间用户可能修改同名文件 | 渲染前记录版本，提交前重查路径与版本，变化时拒绝覆盖 | `tests/unit/test_office.py`、`test_recovery_artifact_integration.py` |
| 通用命令的确认、超时和停止没有统一链路 | 逐次审批、有效时间预算、输出限额、退出码、子进程清理；拒绝或离线时不执行 | `tests/unit/test_command.py`、`test_command_policy.py` |

## 命令执行

工具名：`execute_command`。

```json
{
  "command": ["python", "--version"],
  "cwd": ".",
  "timeout_seconds": 30,
  "max_output_chars": 2000,
  "encoding": "utf-8"
}
```

命令是可执行程序及参数数组，参数不会隐式拼接成 shell。复杂 shell 语法需要明确指定 PowerShell、cmd 或 bash。输出包含 stdout、stderr、退出码、超时与截断状态；非零退出或超时是失败，不建立成功检查点。

默认每个输出流只保留 2000 字符，避免日志吞掉较小模型的上下文。更多内容可保存到文件后分块读取，或在较大上下文配置下显式提高限额。

工作目录必须位于本次工作区；执行必须逐次确认，信任模式和 command_execute=allow 都不能跳过。相关能力被关闭或用户明确要求离线时，不提供和执行这个工具，因为通用命令无法保证只读或不联网。

命令以本机用户权限运行并继承环境，**工作目录不是文件/网络沙箱**。Windows 工作进程先加入 Job Object 再启动目标程序；正常退出、超时或取消时清理普通子进程。POSIX 使用独立会话和进程组，不能保证终止主动脱组的守护进程。本版不提供后台服务托管。

全局工具超时与请求超时取较小值，工具内部处理终止并返回超时结果，外层保留清理余量。命令产生的文件、网络或其他副作用不能通过阶段回溯自动撤销，失败和中断时同样提示核查。

实现依据：[Python subprocess](https://docs.python.org/3/library/asyncio-subprocess.html)、[Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。

## Word 生成

工具名：`create_document`。输出为真实 `.docx`，支持 A4、中文字体、标题/副标题、分节标题、段落、列表、表格、重复表头和页码。

```json
{
  "path": "output/report.docx",
  "title": "项目维护报告",
  "subtitle": "2026-10-13",
  "sections": [
    {"heading": "完成事项", "paragraphs": ["已验证本地工具执行流程。"], "bullets": ["保留运行记录", "保护用户修改"]},
    {"heading": "验证记录", "table": {"headers": ["场景", "结果"], "rows": [["文件生成", "完成"]]}}
  ]
}
```

这是结构化内容生成器，不是任意既有 Word 文件的保真编辑器。当前验证包括生成后重新读取内容和版式属性；没有把 Office GUI 打开或页面渲染作为已完成验证。

## Excel 生成

工具名：`create_spreadsheet`。输出为真实 `.xlsx`，支持多工作表、表格样式、筛选、冻结表头、列宽、数字/日期格式、公式及柱状/折线图。

```json
{
  "path": "output/budget.xlsx",
  "sheets": [{
    "name": "费用",
    "headers": ["项目", "数量", "单价", "金额"],
    "rows": [["材料", 2, 15, "=B2*C2"], ["合计", null, null, "=SUM(D2:D2)"]],
    "column_formats": ["text", "integer", "currency", "currency"],
    "column_widths": [22, 12, 16, 16],
    "charts": [{"type": "bar", "title": "金额", "category_column": 1, "value_columns": [4], "anchor": "F2"}]
  }]
}
```

公式保留为原生单元格公式，设置打开时重新计算，并记录 `formulas_calculated=false`。openpyxl 本身不计算公式，不能把生成后读取的空缓存值或输入数字冒充计算结果。[官方说明](https://openpyxl.readthedocs.io/en/stable/simple_formulae.html)

纯文本列会将类似公式的字符串保留为文本。日期格式列可接收 ISO 日期字符串。输入完整验证后才生成文件；文档总文字与单元格数量有限额，产物和被覆盖的旧文件限 32 MB。

## 文件保护、下载和中断

Office 工具沿用创建/修改/工作区外写入的既有权限。渲染在线程中只生成内存字节；保存时核对渲染前的目标版本，再同步完成快照与原子替换，避免取消后后台落盘。渲染期间路径或内容变化时，要求重新核对，不覆盖新内容。

这是渲染窗口的冲突检测，不是与任意外部编辑器共享的原子 compare-and-swap 锁。极短的最终提交竞态和回溯中途崩溃仍需后续研究。

结果区提供 Word/Excel 下载链接。接口只接受本任务记录的成功产物，核对路径、工作区和内容哈希；文件改变、缺失或与动作不匹配时拒绝下载。回溯按倒序恢复二进制快照，用户后来改过的内容保持不变。

重启不会精确重放任务。系统修复记录中的未闭合工具事务，并调整阶段的消息边界；可能已经生效但没有结果的命令、MCP、浏览器或文件写入，统一提示核查。用户明确说明后再继续。

网关在对账前取得数据目录的系统文件锁并持有到关闭；独立 CLI 执行与快照恢复也要取得同一把锁。其他进程占用时立即拒绝，不能误标其活动任务。进程内多任务通过引用计数共用锁；进程退出或崩溃由系统释放。只读历史和检查仍可运行。Windows 已用真实子进程测试占用、释放和崩溃；POSIX 分支待 Linux CI 运行。

## 固定场景与验证入口

```powershell
$env:PYTHONPATH = "$PWD/src"
python -m pytest -q -p no:cacheprovider
python -m ruff check src tests --no-cache
node --check src/clearact/web/app.js
```

单独运行可维护的完整任务场景：

```powershell
python -m pytest tests/integration/test_task_scenarios.py -q -p no:cacheprovider
```

固定场景使用真实 AgentRunner、工具、权限、存储和快照，以及固定的模型响应：

1. 读取本地资料，生成 Word 和包含 SUM 公式的 Excel，检查实际文件、审计与检查点。
2. 拒绝命令，检查文件副作用不存在、拒绝被记录。
3. 允许命令，检查 stdout/stderr 和退出码。
4. 命令返回非零，检查失败记录和没有成功检查点。

额外在临时配置与临时工作区启动本地网关，用真实 Edge 浏览器检查两个下载链接、命令确认开关、中断提示和不存在无限规划动画；没有浏览器脚本错误。示例任务没有调用真实模型，也不代表真实用户使用记录。

真实模型的评测应另建固定输入，记录模型/配置、耗时、工具次数、人工干预、成功与失败。当前新增测试验证执行系统，不能换算为真实模型成功率。

## 后续维护的范围

- 完成条件目前仍主要是软核对；继续研究可验证的产物与必需动作验收。
- MCP OAuth、流式模型输出、自动断点恢复，以及复杂 Office 文件的读取/保真编辑未在本轮实现。
- 持续观察约束保留的预算成本，未来提炼结构化约束时必须验证不会丢失用户要求。
- 更完整的故障注入、回溯事务日志和外部副作用对账可以沿现有固定场景继续扩展。

保留 [原始审查报告](project-review-2026-10-13.md) 作为本轮修改前的事实记录；它的 203 项测试和缺陷描述属于当时的基线。
