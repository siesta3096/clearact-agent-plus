import shutil
import subprocess

import pytest

from clearact import webapp


def function_source(script, name):
    start = script.index(f"function {name}(")
    return script[start:script.index("\n}", start) + 2]


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
