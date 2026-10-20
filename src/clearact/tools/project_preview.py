"""Bounded browser checks of local web projects, with external requests blocked."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from clearact.domain.errors import ToolValidationError
from clearact.domain.models import ToolDefinition, ToolResult
from clearact.tools.artifacts import save_artifact
from clearact.tools.base import ToolContext


class PreviewProjectTool:
    name = "preview_project"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description=(
            "Run a workspace HTML project in an isolated headless browser, block external HTTP/WebSocket, collect "
            "page errors and a screenshot. Interactions need a unique visible selector. Returns current text and "
            "visible controls with selectors even after failed steps. Use expect_selector for a specific state "
            "region so stale status text cannot satisfy an ending assertion. Each call starts fresh; put a "
            "sequence in one steps array. Text assertions wait up to 3 seconds for asynchronous rendering; "
            "the sequence stops at the first failed assertion. Optional wait_ms pauses after an action."
        ), parameters={"type": "object", "properties": {
            "entry": {"type": "string", "default": "index.html"},
            "steps": {"type": "array", "maxItems": 20, "items": {"type": "object", "properties": {
                "selector": {"type": "string"}, "action": {"type": "string", "enum": ["click", "press", "fill"]},
                "value": {"type": "string"}, "expect_text": {"type": "string"},
                "wait_ms": {"type": "integer", "minimum": 0, "maximum": 2000, "default": 0},
                "expect_selector": {"type": "string", "description": "Unique region to assert; default body."},
            }, "required": ["selector", "action"]}},
            "expect_text": {"type": "string", "description": "Text that must be visible after the steps."},
            "expect_selector": {"type": "string", "default": "body"},
        }})

    async def execute(self, arguments: dict, context: ToolContext, action_id: str) -> ToolResult:
        entry, steps = arguments.get("entry", "index.html"), arguments.get("steps", [])
        expected = arguments.get("expect_text", "")
        expected_selector = arguments.get("expect_selector", "body")
        if (not isinstance(entry, str) or not isinstance(steps, list) or len(steps) > 20
                or not isinstance(expected, str) or len(expected) > 2000
                or not isinstance(expected_selector, str) or not 1 <= len(expected_selector) <= 500):
            raise ToolValidationError("Invalid preview entry, steps or expected text")
        root = context.workspace_root.resolve()
        target = (root / entry).resolve()
        if not target.is_relative_to(root) or not target.is_file() or target.suffix.lower() not in {".html", ".htm"}:
            raise ToolValidationError("Preview entry must be an HTML file inside the workspace")
        for step in steps:
            if (not isinstance(step, dict) or step.get("action") not in {"click", "press", "fill"}
                    or not isinstance(step.get("selector"), str) or not 1 <= len(step["selector"]) <= 500
                    or not isinstance(step.get("value", ""), str) or len(step.get("value", "")) > 2000
                    or not isinstance(step.get("expect_text", ""), str)
                    or len(step.get("expect_text", "")) > 2000
                    or not isinstance(step.get("expect_selector", "body"), str)
                    or not 1 <= len(step.get("expect_selector", "body")) <= 500
                    or type(step.get("wait_ms", 0)) is not int
                    or not 0 <= step.get("wait_ms", 0) <= 2000):
                raise ToolValidationError("Invalid preview interaction")
        if sum(step.get("wait_ms", 0) for step in steps) > 10000:
            raise ToolValidationError("Total preview wait_ms must not exceed 10000")

        class StaticHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = unquote(urlsplit(self.path).path).lstrip("/")
                if "\x00" in path:
                    self.send_error(400)
                    return
                candidate = (root / path).resolve()
                if path == "favicon.ico" and not candidate.exists():
                    self.send_response(204)
                    self.end_headers()
                    return
                if (any(part.startswith(".") for part in Path(path).parts)
                        or not candidate.is_relative_to(root) or not candidate.is_file()):
                    self.send_error(404)
                    return
                try:
                    with candidate.open("rb") as handle:
                        data = handle.read(5 * 1024 * 1024 + 1)
                    if len(data) > 5 * 1024 * 1024:
                        self.send_error(413)
                        return
                    self.send_response(200)
                    content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Security-Policy", "worker-src 'none'; object-src 'none'")
                    self.end_headers()
                    self.wfile.write(data)
                except (OSError, ValueError):
                    self.send_error(404)

        from playwright.async_api import Error, async_playwright, expect

        server = ThreadingHTTPServer(("127.0.0.1", 0), StaticHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        errors, blocked, failures = [], [], []
        completed_steps = 0
        try:
            async with async_playwright() as playwright:
                launch = {"headless": True, "args": ["--disable-background-networking", "--disable-sync"]}
                if os.name == "nt":
                    launch["channel"] = "msedge"
                browser = await playwright.chromium.launch(**launch)
                async with browser:
                    browser_context = await browser.new_context(viewport={"width": 1280, "height": 820},
                                                                accept_downloads=False, service_workers="block")

                    async def route_request(route):
                        url = urlsplit(route.request.url)
                        if f"{url.scheme}://{url.netloc}" == origin and route.request.method == "GET":
                            await route.continue_()
                        else:
                            blocked.append(route.request.url[:500])
                            await route.abort()

                    await browser_context.route("**/*", route_request)
                    # HTTP routing does not intercept WebSocket connections.
                    async def block_socket(socket):
                        blocked.append(socket.url[:500])
                        await socket.close()

                    await browser_context.route_web_socket("**/*", block_socket)
                    page = await browser_context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)[:1600]))
                    page.on("console", lambda message: errors.append(message.text[:1600])
                            if message.type == "error" else None)
                    page.on("dialog", lambda dialog: dialog.dismiss())
                    page.on("response", lambda response: failures.append(
                        f"HTTP {response.status}: {response.url[:300]}")
                            if response.status >= 400 else None)
                    try:
                        await page.goto(origin + "/" + quote(target.relative_to(root).as_posix()),
                                        wait_until="networkidle", timeout=15000)
                        for step in steps:
                            node = page.locator(step["selector"])
                            operation = step["action"]
                            if operation == "click":
                                await node.click(timeout=3000)
                            elif operation == "fill":
                                await node.fill(step.get("value", ""), timeout=3000)
                            else:
                                await node.press(step.get("value", "Enter"), timeout=3000)
                            completed_steps += 1
                            if step.get("wait_ms"):
                                await page.wait_for_timeout(step["wait_ms"])
                            if step.get("expect_text"):
                                try:
                                    await expect(page.locator(step.get("expect_selector", "body"))).to_contain_text(
                                        step["expect_text"], timeout=3000, use_inner_text=True,
                                    )
                                except AssertionError:
                                    failures.append("Expected text missing after interaction: "
                                                    + step["expect_text"][:200])
                                    break
                        if expected and not failures:
                            try:
                                await expect(page.locator(expected_selector)).to_contain_text(
                                    expected, timeout=3000, use_inner_text=True,
                                )
                            except AssertionError:
                                failures.append("Expected final text missing: " + expected[:200])
                        text = await page.locator("body").inner_text(timeout=3000)
                    except Error as exc:
                        failures.append(str(exc)[:1600])
                        try:
                            text = await page.locator("body").inner_text(timeout=3000)
                        except Error:
                            text = ""
                    screenshot = await page.screenshot(timeout=10000)
                    image_name = hashlib.sha256(action_id.encode()).hexdigest() + ".png"
                    preview_path = root / ".clearact" / "previews" / image_name
                    # The resolved destination must still be within the authorized workspace.
                    if not preview_path.resolve().is_relative_to(root):
                        raise ToolValidationError("Preview destination changed outside the workspace")
                    saved = save_artifact(context, preview_path, screenshot)
                    controls = await page.locator("button,a,input,select").evaluate_all(
                        """nodes => {
                          const selector = node => {
                            const parts = [];
                            for (let n = node; n && n.nodeType === 1; n = n.parentElement) {
                              if (n.id && document.querySelectorAll('#' + CSS.escape(n.id)).length === 1) {
                                parts.unshift('#' + CSS.escape(n.id)); break;
                              }
                              const siblings = n.parentElement
                                ? [...n.parentElement.children].filter(e => e.tagName === n.tagName) : [n];
                              parts.unshift(n.tagName.toLowerCase() + ':nth-of-type(' +
                                (siblings.indexOf(n) + 1) + ')');
                            }
                            return parts.join(' > ');
                          };
                          return nodes.filter(n => n.getClientRects().length &&
                            getComputedStyle(n).visibility !== 'hidden').slice(0,30).map(n => ({
                              tag:n.tagName,id:n.id,text:(n.innerText||n.value||'').slice(0,100),
                              selector:selector(n),enabled:!n.disabled
                            }));
                        }"""
                    )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        ok = not (errors or failures or blocked)
        metadata = {"entry": entry, "page_errors": errors, "failures": failures, "blocked_requests": blocked,
                    "steps_completed": completed_steps, "text": text[:3000],
                    "assertions": sum(bool(step.get("expect_text")) for step in steps) + bool(expected),
                    "controls": controls, "screenshot_path": saved["path"], "content_sha256": saved["content_sha256"],
                    "verification_passed": ok}
        return ToolResult(action_id=action_id, tool_name=self.name, ok=ok,
                          content=json.dumps(metadata, ensure_ascii=False), metadata=metadata)
