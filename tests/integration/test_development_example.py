"""Verify an actual model-generated project, not a hand-written expected tool transcript."""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "cheese-case"


def test_example_matches_actual_model_written_file_hashes():
    proof = json.loads((ROOT / "docs/evaluations/2026-10-18-development.json").read_text(encoding="utf-8"))
    assert proof["attempts"][0]["status"] == "needs_review"
    assert proof["attempts"][1]["status"] == "completed"
    for item in proof["files"]:
        assert hashlib.sha256((EXAMPLE / item["path"]).read_bytes()).hexdigest() == item["sha256"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_example_all_endings_save_reload_load_and_restart(workspace):
    shutil.copytree(EXAMPLE, workspace, dirs_exist_ok=True)
    result = subprocess.run(["node", "--eval", (
        "const fs=require('node:fs'),vm=require('node:vm');"
        "console.log(vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8')+';JSON.stringify(STORY)',"
        "Object.create(null),{timeout:1000}));"
    ), str(workspace / "js/story.js")], check=True, capture_output=True, text=True, encoding="utf-8", timeout=5)
    story = json.loads(result.stdout)
    paths, visited, queue = {}, set(), [(story["meta"]["startNode"], [])]
    while queue:
        node_id, route = queue.pop(0)
        if node_id in visited:
            continue
        visited.add(node_id)
        node = story["nodes"][node_id]
        if node.get("ending"):
            paths[node_id] = route
        else:
            assert node.get("choices"), f"Dead end: {node_id}"
            for index, choice in enumerate(node["choices"]):
                assert choice["to"] in story["nodes"], f"Broken link from {node_id}"
                queue.append((choice["to"], [*route, index]))
    assert len(visited) == len(story["nodes"]) and len(paths) >= 2

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(workspace)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def check():
        async with async_playwright() as playwright:
            launch = {"headless": True, **({"channel": "msedge"} if os.name == "nt" else {})}
            async with await playwright.chromium.launch(**launch) as browser:
                page = await browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                origin = f"http://127.0.0.1:{server.server_port}"
                for node_id, route in paths.items():
                    await page.goto(origin + "/index.html")
                    await page.click("#btn-new")
                    for index in route:
                        await page.locator("#choices button").nth(index).click()
                    expected = story["nodes"][node_id]["endingTitle"]
                    assert expected in await page.locator(".ending-badge").inner_text()
                    await page.click("#btn-save")
                    await page.reload()
                    await page.click("#btn-load")
                    assert expected in await page.locator(".ending-badge").inner_text()
                    await page.get_by_role("button", name="⚔️ 重新开始", exact=True).click()
                    assert await page.locator(".ending-badge").count() == 0
                    assert await page.locator("#choices button").count() == len(story["nodes"]["start"]["choices"])
                assert errors == []

    try:
        asyncio.run(check())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
