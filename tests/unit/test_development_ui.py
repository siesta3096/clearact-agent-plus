import shutil
import subprocess

import pytest

from clearact import webapp


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_delivery_links_escape_names_and_history_survives_new_turn():
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")
    start = script.index("function deliveryHtml(")
    end = script.index("function evidenceHtml(", start)
    scenario = r'''
const language = 'en';
const escape = value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;')
  .replaceAll('>','&gt;').replaceAll('"','&quot;');
const readableSize = value => value + ' B';
const resultMap = () => new Map([['preview/"id',{name:'preview_project',metadata:{screenshot_path:'image.png'}}]]);
const report = {passed:false,turn_start:7,files:[{path:'<img>.js',action_id:'a/"b',size_bytes:20}],
  checks:[{title:'<script>',detail:'<img onerror=alert(1)>',passed:false}],
  preview_action_ids:['preview/"id']};
const detail = {run:{id:'run_1',status:'needs_review',delivery_report:report,delivery_history:[report]}};
const html = deliveryHtml(detail);
if(html.includes('<img>') || html.includes('<script>') || html.includes('onerror=alert(1)>'))
  throw Error('Unescaped delivery details');
if(!html.includes('/project-download?turn=7') || !html.includes('preview%2F%22id/screenshot'))
  throw Error('Incorrect evidence links');
detail.run.status = 'created';
if(deliveryHtml(detail) !== '') throw Error('Stale current delivery');
if(!deliveryHistoryHtml(detail).includes('&lt;img&gt;.js')) throw Error('Earlier delivery disappeared');
'''
    subprocess.run(["node", "--eval", script[start:end] + scenario], check=True, capture_output=True, text=True)
