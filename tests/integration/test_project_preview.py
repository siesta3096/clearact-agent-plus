"""Real browser interactions; requires the configured Chromium/Edge installation."""

import asyncio
from pathlib import Path

from clearact.tools.base import ToolContext
from clearact.tools.project_preview import PreviewProjectTool


def test_browser_loads_modules_and_exercises_both_branches(workspace):
    (workspace / "index.html").write_text(
        '<html><meta charset="utf-8"><h1>Castle</h1><button id="a">Tea</button>'
        '<button id="b">Cheese</button><p id="ending">Choose</p><script type="module" src="app.js"></script></html>',
        encoding="utf-8",
    )
    (workspace / "app.js").write_text(
        "import {endings} from './story.js';\n"
        "for(const key of ['a','b']) document.getElementById(key).onclick=()=>"
        "document.querySelector('#ending').textContent=endings[key];", encoding="utf-8",
    )
    (workspace / "story.js").write_text("export const endings={a:'Tea ending',b:'Cheese ending'};", encoding="utf-8")

    async def check():
        for branch, ending in [("a", "Tea ending"), ("b", "Cheese ending")]:
            result = await PreviewProjectTool().execute({"steps": [
                {"selector": "#" + branch, "action": "click", "expect_text": ending},
            ], "expect_text": ending}, ToolContext(workspace), "preview_" + branch)
            assert result.ok, result.content
            assert result.metadata["steps_completed"] == 1
            assert ending in result.metadata["text"]
            assert Path(result.metadata["screenshot_path"]).read_bytes().startswith(b"\x89PNG")

    asyncio.run(check())


def test_browser_reports_missing_resources_and_blocks_external_requests(workspace):
    (workspace / "index.html").write_text(
        '<html><h1>Broken</h1><script src="missing.js"></script>'
        '<script>fetch("https://example.invalid/leak").catch(()=>{});</script></html>', encoding="utf-8",
    )
    result = asyncio.run(PreviewProjectTool().execute({}, ToolContext(workspace), "broken_preview"))
    assert not result.ok
    assert any("missing.js" in failure for failure in result.metadata["failures"])
    assert result.metadata["blocked_requests"] == ["https://example.invalid/leak"]
    assert Path(result.metadata["screenshot_path"]).is_file()


def test_failed_interaction_returns_current_visible_state_and_usable_selectors(workspace):
    (workspace / "index.html").write_text(
        '<html><button id="start" onclick="this.hidden=true;document.querySelector(\'#choices\').hidden=false">'
        'Start</button><div id="choices" hidden><p>Choose branch</p>'
        '<button class="choice" onclick="document.querySelector(\'p\').textContent=\'Ending A\'">A</button>'
        '<button class="choice">B</button></div></html>', encoding="utf-8",
    )

    async def check():
        tool, ctx = PreviewProjectTool(), ToolContext(workspace)
        failed = await tool.execute({"steps": [{"selector": "#start", "action": "click"},
                                               {"selector": ".choice", "action": "click"}]}, ctx, "ambiguous")
        assert not failed.ok
        assert failed.metadata["steps_completed"] == 1
        assert "Choose branch" in failed.metadata["text"]
        assert [node["text"] for node in failed.metadata["controls"]] == ["A", "B"]
        selector = failed.metadata["controls"][0]["selector"]
        passed = await tool.execute({"steps": [{"selector": "#start", "action": "click"},
                                               {"selector": selector, "action": "click", "expect_text": "Ending A"}]},
                                    ctx, "repaired_selector")
        assert passed.ok, passed.content

    asyncio.run(check())


def test_state_assertion_cannot_be_satisfied_by_stale_status_text(workspace):
    (workspace / "index.html").write_text(
        '<html><div id="scene">Ending A</div><div id="status">Loaded Ending B</div></html>', encoding="utf-8",
    )
    result = asyncio.run(PreviewProjectTool().execute({"expect_text": "Ending B", "expect_selector": "#scene"},
                                                     ToolContext(workspace), "specific_state"))
    assert not result.ok
    assert "Expected final text missing" in result.content


def test_assertions_wait_for_typewriter_text_and_failed_steps_stop_the_sequence(workspace):
    (workspace / "index.html").write_text(
        '<html><button id="start" onclick="setTimeout(()=>document.querySelector(\'#scene\').textContent='
        '\'Ready\',700)">Start</button><p id="scene">Waiting</p>'
        '<button id="later" onclick="document.querySelector(\'#scene\').textContent=\'Wrong\'">Later</button></html>',
        encoding="utf-8",
    )

    async def check():
        tool, ctx = PreviewProjectTool(), ToolContext(workspace)
        passed = await tool.execute({"steps": [{"selector": "#start", "action": "click",
                                               "expect_text": "Ready", "expect_selector": "#scene"}]},
                                    ctx, "async_text")
        assert passed.ok, passed.content
        failed = await tool.execute({"steps": [
            {"selector": "#start", "action": "click", "expect_text": "Missing", "expect_selector": "#scene"},
            {"selector": "#later", "action": "click"},
        ]}, ctx, "stop_after_assertion")
        assert not failed.ok
        assert failed.metadata["steps_completed"] == 1
        assert "Ready" in failed.metadata["text"] and "Wrong" not in failed.metadata["text"]

    asyncio.run(check())
