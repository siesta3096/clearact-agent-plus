# 2026-10-18 修复记录：计划后第二次模型请求失败

版本：0.2.2。上一版本：[v0.2.1](https://github.com/siesta3096/clearact-agent-plus/tree/v0.2.1)。

## 用户实际遇到的问题

在空工作目录中请求创建视觉小说游戏，第一次模型请求成功并生成计划，第二次请求立即失败，还没有执行游戏文件写入。界面仅提示检查接口类型、模型名称和 Base URL，无法据此判断真正原因。

在用户当前本地 OpenAI-compatible 服务重放失败请求，取得 HTTP 400，服务错误明确包含：

```text
Jinja Exception: System message must be at the beginning.
```

运行时在已有对话末尾加入“计划已存在”等系统提示，本地模型模板不允许系统提示出现在用户/工具消息之后。因此正常的第一轮请求，并不能证明后续工具循环的消息格式兼容。此问题早于 0.2.1 已存在，完成核对和续写等后续提示也会遇到同类风险。

## 修复

在两个 Provider 的发送边界统一整理系统消息：

1. 原始规则和后续系统提示按原有顺序合并，作为唯一的开头系统消息。
2. 用户、助手和工具消息保持原顺序、角色、工具关联及返回的 reasoning。
3. 不把网页、文件或其他工具结果提升为系统提示。
4. 不改写磁盘上的原始运行记录，也不修改用户模型配置。

对这一已知模板错误提供具体的消息顺序诊断，不直接向界面复制可能包含隐私的服务响应。其他错误仍使用原有分类。

同时明确首次请求仅开放规划工具、规划后才开放执行工具；计划成功后直接提示下一阶段标识，减少模型漏掉阶段声明的情况。提示保持简短，继续满足已有小上下文预算测试。

## 验证

- 新增 6 项回归，覆盖两个 Provider 的严格消息模板、规划后真实文件写入、续聊、工具信任边界、原始记录不变，以及中英文安全错误提示。
- 完整测试 **394 项通过**；Ruff、JavaScript 语法和差异检查通过。
- 原失败请求：未整理消息时 HTTP 400；使用修复后的实际 Provider 重放返回 HTTP 200。重放限制输出长度，没有执行模型提出的动作，不能据此声称游戏已生成。
- 使用当前本地模型，在隔离临时目录执行创建固定内容文本文件的小任务。初次探测文件内容正确，但模型漏掉阶段声明，因此为 `needs_review`；明确下一步提示后，完整走通规划、声明阶段、文件写入和最终答复，状态为 `completed`，内容与要求一致。

本次实际调用仅连接当前本地服务，不调用外部付费模型，不触碰真实游戏目录，不改写原失败任务记录。真实模型小任务是单次验证，不代表任意模型或复杂游戏任务的成功率。

回归入口：

```powershell
$env:PYTHONPATH = "$PWD/src"
python -m pytest tests/integration/test_provider_message_order.py -q -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check src tests --no-cache
node --check src/clearact/web/app.js
git -c core.autocrlf=false diff --check
```

## 更新后的使用方式与边界

已经启动的网关需要重新启动，才会加载新代码。随后可在原话题明确要求继续执行；原计划、用户要求和成功结果仍保留。修复不自动重放失败任务，也不把旧失败状态改成成功。

视觉小说的完整生成和浏览器玩法验收尚未执行。本轮解决的是计划后请求被拒绝的确定性兼容错误；阶段提示仍依赖模型遵守，最终验收继续如实呈现未满足条件。

后续系统提示会在发送时合并到开头，因此服务看到的是合并后的当前规则；本地记录保留每条提示的原位置，便于审查。此方案不保证适配所有第三方模板或接口扩展。

实现：[消息整理](../src/clearact/providers/messages.py)、[OpenAI-compatible](../src/clearact/providers/openai_compatible.py)、[Ollama](../src/clearact/providers/ollama.py)、[错误分类](../src/clearact/runtime/model_retry.py)、[回归场景](../tests/integration/test_provider_message_order.py)。
