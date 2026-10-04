import shutil
import subprocess

import pytest

from clearact import webapp


def test_first_run_ui_has_discoverable_guide_and_capability_examples():
    html = (webapp._ASSET_DIR / "index.html").read_text(encoding="utf-8")
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")

    assert 'id="guide-trigger"' in html
    assert 'id="guide-dialog"' in html
    assert "接入新的能力" in html
    assert "mcpServers" in html
    assert "registry.modelcontextprotocol.io" in html
    assert "github.com/modelcontextprotocol/servers" in html
    assert "smithery.ai" in html
    assert "quick-card" in html
    assert "welcome-guide" in script
    assert "guide-dialog" in script


def test_folder_picker_changes_workspace_without_rewriting_the_goal():
    html = (webapp._ASSET_DIR / "index.html").read_text(encoding="utf-8")
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")

    assert 'id="workspace-indicator"' in html
    assert 'id="workspace-path"' in html
    assert "setWorkspace(result.path)" in script
    assert "请处理这个文件夹中的文件" not in script
    assert "Please work with this folder" not in script
    assert "model.retrying" in script
    assert "connection-banner" in script
    assert "Ollama 原生（/api/chat）" in script
    assert "OpenAI 兼容（/v1/chat/completions）" in script
    assert 'id="attach-files"' in html
    assert 'id="file-input"' in html
    assert 'id="attachment-list"' in html
    assert 'json("/api/uploads"' in script


def test_web_assets_keep_untrusted_content_out_of_html_attributes():
    html = (webapp._ASSET_DIR / "index.html").read_text(encoding="utf-8")
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")

    assert 'id="approval-dialog"' in html
    assert "data-copy=\"${" not in script
    assert "button.dataset.copy=final.content" in script
    assert "safeHttpUrl" in script
    assert "replace(/[&<>\"']/g" in script


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_result_card_uses_only_the_latest_user_turn():
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")
    start = script.index("function latestTurnFinal(run) {")
    function = script[start:script.index("\n}", start) + 2]
    scenario = """
const run = {messages: [
  {role: 'user', content: 'first'},
  {role: 'assistant', content: 'old answer'},
  {role: 'user', content: 'new task'},
]};
if (latestTurnFinal(run) !== undefined) throw Error('Old answer leaked into new task');
run.messages.push({role: 'assistant', tool_calls: [{id: 'tool'}]});
if (latestTurnFinal(run) !== undefined) throw Error('Tool proposal is not a final answer');
run.messages.push({role: 'assistant', content: 'new answer'});
if (latestTurnFinal(run).content !== 'new answer') throw Error('Latest answer was not selected');
"""
    subprocess.run(["node", "--eval", function + scenario], check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_current_request_is_visible_and_earlier_requests_are_collapsed():
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")
    start = script.index("function requestContextHtml(run, previousOpen = false) {")
    function = script[start:script.index("\n}", start) + 2]
    scenario = """
const language = 'zh', t = () => '你', markdown = (value) => `<p>${value}</p>`;
const attachmentsHtml = () => '';
const run = {messages: [
  {role: 'user', content: 'first request'},
  {role: 'assistant', content: 'old result'},
  {role: 'user', content: 'latest request'},
]};
const rendered = requestContextHtml(run);
if (!rendered.includes('本轮请求') || !rendered.includes('此前请求（1）')) throw Error('Request labels missing');
if (rendered.indexOf('latest request') > rendered.indexOf('first request')) throw Error('Old request shown first');
if (!rendered.includes('<details class="prior-requests">')) throw Error('Earlier requests are not collapsed');
"""
    subprocess.run(["node", "--eval", function + scenario], check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_follow_up_collapses_previous_execution_phases():
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")
    start = script.index("function workflowStagesHtml(detail, steps, calls, previousOpen = false) {")
    function = script[start:script.index("\n}", start) + 2]
    scenario = """
const language = 'zh', stepCard = (_detail, step) => `<article>${step.title}</article>`;
const detail = {run: {messages: [
  {role: 'user'}, {role: 'assistant'}, {role: 'user'}, {role: 'assistant'},
]}};
const steps = [
  {title: 'old step', status: 'needs_review', start_message_index: 1},
  {title: 'new step', status: 'needs_review', start_message_index: 3},
];
const rendered = workflowStagesHtml(detail, steps, []);
if (!rendered.includes('<details class="prior-stages">')) throw Error('Old phases were not collapsed');
if (!rendered.includes('先前执行阶段（1）')) throw Error('Old phase count is missing');
if (!rendered.includes('1 项需核对')) throw Error('Hidden review warning was not surfaced');
if (rendered.includes('2 项需核对')) throw Error('Current review warning was counted as old');
if (rendered.indexOf('old step') > rendered.indexOf('</details>')) throw Error('Old phase escaped details');
if (rendered.indexOf('new step') < rendered.indexOf('</details>')) throw Error('New phase was hidden');
const expanded = workflowStagesHtml(detail, steps, [], true);
if (!expanded.includes('<details class="prior-stages" open>')) throw Error('Open state was lost');
detail.run.messages = [{role: 'user'}];
if (workflowStagesHtml(detail, steps, []).includes('prior-stages')) throw Error('First run should show all phases');
"""
    subprocess.run(["node", "--eval", function + scenario], check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_transient_status_banners_ignore_previous_user_turns():
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")
    start = script.index("function currentTurnEvents(detail) {")
    event_function = script[start:script.index("\n}", start) + 2]
    start = script.index("function connectionHtml(detail) {")
    connection_function = script[start:script.index("\n", start)]
    start = script.index("function completionReviewHtml(detail) {")
    review_function = script[start:script.index("\n}", start) + 2]
    scenario = """
const language = 'zh', escape = (value) => String(value);
const detail = {run: {status: 'running', workflow_steps: [], stage_notes: {}}, events: [
  {type: 'run.started'},
  {type: 'model.retrying', title: 'old retry'},
  {type: 'completion.review_requested', title: 'old review'},
  {type: 'run.started'},
]};
if (connectionHtml(detail) || completionReviewHtml(detail)) throw Error('Old banners leaked into follow-up');
detail.events.push({type: 'model.retrying', title: 'new retry'});
detail.events.push({type: 'completion.review_requested', title: 'new review'});
if (!connectionHtml(detail).includes('new retry')) throw Error('Current connection status was hidden');
if (!completionReviewHtml(detail).includes('new review')) throw Error('Current review was hidden');
detail.run.status = 'created';
if (currentTurnEvents(detail).length) throw Error('Pending new turn inherited old events');
detail.run.messages = [{role: 'user'}, {role: 'assistant'}, {role: 'user'}];
detail.run.workflow_steps = [{status: 'needs_review', title: 'old phase', start_message_index: 1}];
detail.run.stage_notes.completion_review = 'old warning';
if (completionReviewHtml(detail)) throw Error('Pending new turn inherited old completion warnings');
"""
    subprocess.run(
        ["node", "--eval", event_function + connection_function + review_function + scenario],
        check=True, capture_output=True, text=True,
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_search_usage_separates_successes_from_failed_attempts():
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")
    start = script.index("function runUsageHtml(detail) {")
    function = script[start:script.index("\n}", start) + 2]
    scenario = """
const language = 'zh', escape = (value) => String(value), formatDuration = () => '0 秒';
const detail = {run: {
  status: 'completed', created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:01Z',
  research_strategy: {mode: 'local_first', total_search_limit: 2},
  messages: [
    {role: 'user'},
    {role: 'tool', name: 'web_search', metadata: {status: 'failed'}},
    {role: 'tool', name: 'web_search', metadata: {status: 'succeeded'}},
  ],
}, events: []};
const rendered = runUsageHtml(detail);
if (!rendered.includes('成功检索 <strong>1/2</strong>')) throw Error('Failed search spent the success budget');
if (!rendered.includes('检索失败 <strong>1</strong>')) throw Error('Failed attempt was hidden');
if (!rendered.includes('工具调用 <strong>2</strong>')) throw Error('Executed call count changed');
"""
    subprocess.run(["node", "--eval", function + scenario], check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_revised_plan_shows_public_reason_without_raw_html():
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")
    start = script.index("function planHtml(run) {")
    function = script[start:script.index("\n}", start) + 2]
    scenario = """
const language = 'zh', escape = (value) => String(value).replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const run = {workflow_plan: [{id: 'verify', title: '核验来源', summary: '核对资料'}],
  workflow_steps: [], messages: [
    {role: 'tool', name: 'revise_workflow_plan', metadata: {status: 'succeeded',
      reason: '<old evidence>', replaced: ['旧阶段'], future: ['<new phase>']}},
    {role: 'tool', name: 'revise_workflow_plan', metadata: {status: 'succeeded',
      reason: '<new evidence>', replaced: ['<new phase>'], future: ['核验来源']}},
    {role: 'tool', name: 'revise_workflow_plan', metadata: {status: 'skipped', reason: 'no_new_result'}}
  ]};
const rendered = planHtml(run);
if (!rendered.includes('计划已调整 2 次')) throw Error('Plan update count missing');
if (!rendered.includes('查看调整历史')) throw Error('Revision history control missing');
if (!rendered.includes('第 1 次调整') || !rendered.includes('第 2 次调整')) throw Error('Revision history incomplete');
if (!rendered.includes('另有 1 次无新结果的重复调整已跳过')) throw Error('Skipped revisions are not transparent');
if (!rendered.includes('&lt;old evidence&gt;')) throw Error('Older reason missing');
if (!rendered.includes('&lt;new evidence&gt;')) throw Error('Reason was not safely displayed');
if (rendered.includes('<new evidence>') || rendered.includes('<new phase>'))
  throw Error('Untrusted revision entered HTML');
if (!rendered.includes('核验来源')) throw Error('Revised phase is missing');
"""
    subprocess.run(["node", "--eval", function + scenario], check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_failed_run_without_plan_does_not_show_endless_planning_spinner():
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")
    start = script.index("function planHtml(run) {")
    function = script[start:script.index("\n}", start) + 2]
    scenario = """
const language = 'zh';
const failed = planHtml({status: 'failed', workflow_plan: []});
if (!failed.includes('未能生成任务步骤')) throw Error('Failed plan is not explained');
if (failed.includes('正在生成') || failed.includes('<i>')) throw Error('Failed run still shows a spinner');
const running = planHtml({status: 'running', workflow_plan: []});
if (!running.includes('正在生成')) throw Error('Active planning lost its progress state');
"""
    subprocess.run(["node", "--eval", function + scenario], check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_reused_file_read_can_preview_original_result():
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")
    start = script.index("function actionLayout(detail, actions) {")
    function = script[start:script.index("\n}", start) + 2]
    scenario = """
const language = 'zh', escape = (value) => String(value).replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const actionStatus = () => '已复用已有结果';
const resultMap = () => new Map([['retry', {metadata: {status: 'skipped', reused_from_action_id: 'old-read'}}]]);
const rendered = actionLayout({}, [{id: 'retry', tool_name: 'read_file', arguments: {path: 'source.txt'}}]);
if (!rendered.includes('data-preview-action="old-read"')) throw Error('Original read is not previewable');
if (!rendered.includes('查看复用内容')) throw Error('Reuse preview is not labelled');
if (rendered.includes('data-preview-action="retry"')) throw Error('Skipped read was used as a preview source');
"""
    subprocess.run(["node", "--eval", function + scenario], check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_result_evidence_lists_only_successfully_opened_sources():
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")
    start = script.index("function evidenceHtml(detail) {")
    function = script[start:script.index("\n}", start) + 2]
    scenario = """
const language = 'zh', escape = (value) => String(value).replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const safeHttpUrl = (value) => value.startsWith('https://') ? value : null;
const workflowData = () => ({calls: [
  {id: 'read', tool_name: 'read_file', arguments: {path: 'rules.txt'}},
  {id: 'again', tool_name: 'read_file', arguments: {path: 'rules.txt'}},
  {id: 'page', tool_name: 'fetch_url', arguments: {url: 'https://example.test/source'}},
  {id: 'search', tool_name: 'web_search', arguments: {query: 'not evidence'}},
  {id: 'failed', tool_name: 'read_pdf', arguments: {path: 'failed.pdf'}},
]});
const resultMap = () => new Map([
  ['read', {metadata: {status: 'succeeded', path: 'C:/docs/rules.txt'}}],
  ['again', {metadata: {status: 'succeeded', path: 'C:/docs/rules.txt'}}],
  ['page', {metadata: {status: 'succeeded'}}],
  ['search', {metadata: {status: 'succeeded'}}],
  ['failed', {metadata: {status: 'failed'}}],
]);
const rendered = evidenceHtml({});
if (!rendered.includes('已读取资料 <span>2</span>')) throw Error('Only two opened sources should count');
if (!rendered.includes('data-preview-action="again"')) throw Error('Latest local read preview missing');
if (rendered.includes('data-preview-action="read"')) throw Error('Stale first read was selected');
if (!rendered.includes('https://example.test/source')) throw Error('Opened page missing');
if (rendered.includes('failed.pdf') || rendered.includes('not evidence')) throw Error('Unopened source was shown');
if (!rendered.includes('不等于每项结论都引用了它')) throw Error('Source/claim distinction missing');
"""
    subprocess.run(["node", "--eval", function + scenario], check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_failed_fetch_guard_is_not_labeled_as_reused_success():
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")
    start = script.index("function actionStatus(result) {")
    status_function = script[start:script.index("\n", start)]
    start = script.index("function guardNote(result) {")
    note_function = script[start:script.index("\n}", start) + 2]
    scenario = """
const language = 'zh', t = (key) => key, escape = (value) => String(value);
const skipped = {metadata: {status: 'skipped', reason: 'failed_fetch'}};
if (actionStatus(skipped) !== '此前打开失败') throw Error('Failed URL appeared reused');
if (!guardNote(skipped).includes('本轮不再重复请求')) throw Error('Skip reason was hidden');
"""
    subprocess.run(
        ["node", "--eval", status_function + note_function + scenario],
        check=True, capture_output=True, text=True,
    )
