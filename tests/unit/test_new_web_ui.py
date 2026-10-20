import json
import shutil
import subprocess

import pytest

from clearact import webapp


def function_source(script, name):
    start = script.index(f"function {name}(")
    if script[max(0, start - 6):start] == "async ":
        start -= 6
    return script[start:script.index("\n}", start) + 2]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_context_notice_uses_only_current_turn_events_and_valid_counts():
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")
    scenario = r'''
let language = 'zh';
const escape = (value) => String(value).replaceAll('<', '&lt;');
const currentTurnEvents = (detail) => detail.events.filter((event) => event.turn === 2);
const detail = {events:[{turn:1,type:'context.compacted',data:{reasoning_characters_omitted:74119}}]};
if (contextNoticeHtml(detail)) throw Error('Previous turn notice leaked');
detail.events.push({turn:2,type:'context.compacted',title:'<img>',data:{reasoning_characters_omitted:74119}});
let html = contextNoticeHtml(detail);
if (!html.includes('上下文已自动整理') || !html.includes('记录中') || html.includes('<img>'))
  throw Error('Missing truthful notice or untrusted title rendered');
language = 'en';
if (!contextNoticeHtml(detail).includes('Original messages')) throw Error('English notice missing');
for (const count of [null, -1, 0, '<img>', Infinity, NaN]) {
  detail.events[1].data.reasoning_characters_omitted = count;
  if (contextNoticeHtml(detail)) throw Error('Invalid count rendered');
}
detail.events[1].data.history_messages_omitted = 10;
if (!contextNoticeHtml(detail).includes('10 old process messages')) throw Error('History-only compaction is hidden');
detail.events[1].data.history_messages_omitted = '<img>';
if (contextNoticeHtml(detail)) throw Error('Invalid history count rendered');
detail.events[1].data.tool_results_excerpted = 3;
if (!contextNoticeHtml(detail).includes('3 old read excerpts')) throw Error('Read excerpt notice missing');
detail.events[1].data.tool_results_excerpted = '<img>';
if (contextNoticeHtml(detail)) throw Error('Invalid excerpt count rendered');
'''
    subprocess.run(["node", "--eval", function_source(script, "contextNoticeHtml") + scenario],
                   check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_generated_file_links_are_escaped_deduplicated_and_only_successful():
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")
    functions = function_source(script, "artifactsHtml")
    scenario = r'''
const language = 'en';
const escape = (value) => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const messages = [
  {role:'tool',name:'create_document',tool_call_id:'old',metadata:{status:'succeeded',path:'C:/work/report.docx'}},
  {role:'tool',name:'create_document',tool_call_id:'latest',metadata:{status:'succeeded',path:'C:/work/report.docx'}},
  {role:'tool',name:'create_spreadsheet',tool_call_id:'a/"b',metadata:{status:'succeeded',path:'C:/work/<img>.xlsx'}},
  {role:'tool',name:'create_document',tool_call_id:'failed',metadata:{status:'failed',path:'failed.docx'}},
  {role:'tool',name:'write_file',tool_call_id:'text',metadata:{status:'succeeded',path:'text.txt'}}
];
const html = artifactsHtml({run:{id:'run_test',messages}});
if (html.includes('/old/download') || !html.includes('/latest/download'))
  throw Error('Duplicate path was not reduced to latest');
if (html.includes('<img>') || !html.includes('&lt;img&gt;.xlsx')) throw Error('File name was not escaped');
if (!html.includes('a%2F%22b/download')) throw Error('Action ID was not URL encoded');
if (html.includes('failed.docx') || html.includes('text.txt')) throw Error('Non-artifacts exposed');
'''
    subprocess.run(["node", "--eval", functions + scenario], check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_interruption_banner_disappears_after_manual_continuation():
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")
    scenario = '''
const language = 'en';
if (!interruptionHtml({run:{status:'interrupted'}}).includes('Check command or external effects'))
  throw Error('No recovery guidance');
if (interruptionHtml({run:{status:'running'}}) !== '') throw Error('Stale interruption banner');
'''
    subprocess.run(["node", "--eval", function_source(script, "interruptionHtml") + scenario],
                   check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_interrupted_task_without_plan_has_no_endless_spinner():
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")
    scenario = '''
const language = 'en';
const html = planHtml({status:'interrupted',workflow_plan:[]});
if (html.includes('plan-loading') || !html.includes('inspect preserved results'))
  throw Error('Interrupted task still spins');
'''
    subprocess.run(["node", "--eval", function_source(script, "planHtml") + scenario],
                   check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_acceptance_report_escapes_details_counts_results_and_hides_stale_report():
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")
    scenario = '''
let language = 'en';
const escape = (value) => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const run = {status:'needs_review', completion_checks:[
  {status:'passed',phase_title:'Document',detail:'report.docx'},
  {status:'failed',phase_title:'<img>',detail:'<script>bad()</script>'},
  {status:'unverified',phase_title:'Read',detail:'Access denied'}
]};
const html = acceptanceHtml({run});
if (!html.includes('1 / 3 passed') || !html.includes('Unverified')) throw Error('Missing outcomes');
if (html.includes('<script>') || html.includes('<img>')) throw Error('Unsafe report HTML');
if (!html.includes('&lt;script&gt;')) throw Error('Details missing');
language = 'zh';
if (!acceptanceHtml({run}).includes('通过 1 / 3 项')) throw Error('Chinese label missing');
run.status = 'created';
if (acceptanceHtml({run}) !== '') throw Error('Stale report on followup');
if (acceptanceHtml({run:{status:'completed'}}) !== '') throw Error('Legacy run compatibility');
'''
    subprocess.run(["node", "--eval", function_source(script, "acceptanceHtml") + scenario],
                   check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_needs_review_without_plan_has_no_endless_spinner():
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")
    scenario = '''
const language = 'en';
const html = planHtml({status:'needs_review',workflow_plan:[]});
if (html.includes('plan-loading') || !html.includes('still need review'))
  throw Error('Finished task still spins');
'''
    subprocess.run(["node", "--eval", function_source(script, "planHtml") + scenario],
                   check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
@pytest.mark.parametrize("lang,feedback,confirm", [
    ("zh", "", True), ("en", "   ", True), ("zh", "", False), ("en", "  Keep <img> as text  ", True),
])
def test_stage_restart_previews_optional_feedback_and_only_posts_after_confirmation(lang, feedback, confirm):
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")
    config = json.dumps({"language": lang, "feedback": feedback, "confirm": confirm})
    scenario = r'''
const language = config.language;
let activeId = 'run_test', viewEpoch = 1, lastRunSignature = 'old';
const escape = (value) => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
const body = {}, title = {}, button = {};
let shown = 0, displayed = 0;
class Dialog extends EventTarget {
  showModal() { shown++; queueMicrotask(() => config.confirm ? button.onclick() : this.close()); }
  close() { this.dispatchEvent(new Event('close')); }
}
const dialog = new Dialog();
const $ = (selector) => ({'#rewind-dialog':dialog, '#rewind-dialog-title':title,
  '#rewind-preview-body':body, '#confirm-rewind':button})[selector];
const calls = [];
const json = async (url, options) => {
  calls.push({url,options});
  if (options) return {run_id:'run_test'};
  return {from_step_title:'Start',reused_steps:[],discarded_steps:[{title:'Start'}],restore_paths:[],
    warnings:[],base_updated_at:'version',preview_token:'token'};
};
const showRun = async () => { displayed++; };
(async () => {
  await restartAtStage('understand', config.feedback);
  if (shown !== 1) throw Error('No preview for blank feedback');
  if (calls.length !== (config.confirm ? 2 : 1)) throw Error('Execution without confirmation');
  if (body.innerHTML.includes('<img>')) throw Error('Feedback not escaped');
  if (config.confirm) {
    const data = JSON.parse(calls[1].options.body);
    if (!data.goal.trim()) throw Error('No default retry instruction');
    if (config.feedback.trim() && data.goal !== config.feedback.trim()) throw Error('Custom feedback lost');
    if (!body.innerHTML.includes(escape(data.goal))) throw Error('Submitted instruction missing from preview');
    if (data.rewind_step_id !== 'understand' || data.rewind_base_updated_at !== 'version'
      || data.rewind_preview_token !== 'token') throw Error('Rewind safeguards lost');
    if (displayed !== 1) throw Error('Updated task not displayed');
  } else if (displayed !== 0) throw Error('Cancelled restart changed task');
})().catch((error) => { console.error(error); process.exitCode = 1; });
'''
    subprocess.run(["node", "--eval", "const config = " + config + ";\n"
                    + function_source(script, "restartAtStage") + scenario],
                   check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_restart_submit_reports_errors_and_blocks_duplicate_clicks():
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")
    scenario = r'''
const language = 'en';
const button = {disabled:false}, status = {textContent:''}, globalStatus = {};
const $ = () => globalStatus;
const form = {dataset:{}, querySelector: (selector) => selector.startsWith('button') ? button
  : selector === 'textarea' ? {value:''} : status,
  closest: () => ({dataset:{stepId:'understand'}})};
const event = {currentTarget:form, preventDefault() {}};
let calls = 0, finish;
let restartAtStage = async () => { calls++; await new Promise((resolve) => { finish = resolve; }); };
(async () => {
  const first = submitStageRestart(event);
  await submitStageRestart(event);
  if (calls !== 1 || !button.disabled || !status.textContent.includes('Checking'))
    throw Error('Duplicate restart or missing immediate feedback');
  finish(); await first;
  if (button.disabled || status.textContent) throw Error('Button not restored after cancellation/success');
  restartAtStage = async () => { throw Error('Preview unavailable'); };
  await submitStageRestart(event);
  if (status.textContent !== 'Preview unavailable' || globalStatus.textContent !== 'Preview unavailable')
    throw Error('Failure not shown next to the clicked button');
  if (button.disabled || form.dataset.submitting) throw Error('Cannot retry after failure');
})().catch((error) => { console.error(error); process.exitCode = 1; });
'''
    subprocess.run(["node", "--eval", function_source(script, "submitStageRestart") + scenario],
                   check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_model_wait_banner_shows_budget_and_clears_on_response_or_stop():
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")
    scenario = r'''
const language = 'en', escape = (value) => String(value).replaceAll('<','&lt;');
const currentTurnEvents = (detail) => detail.events;
const detail = {run:{status:'running'},events:[
  {type:'model.retrying',title:'Old retry'},
  {type:'model.waiting',title:'Waiting for model response',data:{timeout_seconds:1800}}
]};
let html = connectionHtml(detail);
if (!html.includes('1800s') || !html.includes('Waiting for the complete response'))
  throw Error('Missing model wait limit or waiting explanation');
if (html.includes('Old retry')) throw Error('Waiting misrepresented as repeated generation');
detail.events.push({type:'model.responded'});
if (connectionHtml(detail)) throw Error('Waiting banner remained after receiving the response');
detail.events.push({type:'model.waiting',title:'Waiting',data:{timeout_seconds:600}});
detail.run.status = 'cancelled';
if (connectionHtml(detail)) throw Error('Stopped task still shows waiting');
'''
    subprocess.run(["node", "--eval", function_source(script, "connectionHtml") + scenario],
                   check=True, capture_output=True, text=True)
