import asyncio
import json

import httpx
import pytest

from clearact.domain.errors import ToolValidationError
from clearact.tools.base import ToolContext
from clearact.tools.network import PinnedNetworkBackend, client_kwargs
from clearact.tools.web import FetchUrlTool, WebSearchTool


def test_search_uses_nanobot_default_ddgs_and_formats_results(monkeypatch, workspace):
    class DDGS:
        def __init__(self, **_kwargs):
            pass

        def text(self, query, max_results):
            assert query == "clearact"
            assert max_results == 2
            return [
                {
                    "title": "<b>Official</b> report",
                    "href": "https://example.com/report",
                    "body": "A <i>fresh</i> result.",
                }
            ]

    monkeypatch.setattr("ddgs.DDGS", DDGS)
    result = asyncio.run(
        WebSearchTool().execute({"query": "clearact", "count": 2}, ToolContext(workspace), "act_search")
    )
    assert result.ok is True
    assert result.metadata == {"query": "clearact", "provider": "duckduckgo-ddgs", "count": 1}
    assert "Official report" in result.content
    assert "https://example.com/report" in result.content
    assert "fresh result" in result.content


def test_search_falls_back_when_ddgs_times_out(monkeypatch, workspace):
    class DDGS:
        def __init__(self, **_kwargs):
            pass

        def text(self, *_args, **_kwargs):
            raise TimeoutError

    class Response:
        text = (
            '<li class="b_algo"><h2><a href="https://example.com/report">Official report</a></h2>'
            "<p>Fresh result.</p></li>"
        )

        def raise_for_status(self):
            return None

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **_kwargs):
            if "duckduckgo" in url:
                raise httpx.ConnectTimeout("blocked")
            return Response()

    monkeypatch.setattr("ddgs.DDGS", DDGS)
    monkeypatch.setattr("clearact.tools.web.httpx.AsyncClient", lambda **_kwargs: Client())
    result = asyncio.run(WebSearchTool().execute({"query": "clearact"}, ToolContext(workspace), "act_search"))
    assert result.ok is True
    assert result.metadata["provider"] == "bing-html"
    assert "https://example.com/report" in result.content


def test_fetch_blocks_localhost_when_context_disallows_it(workspace):
    with pytest.raises(ToolValidationError, match="private/internal"):
        asyncio.run(
            FetchUrlTool().execute(
                {"url": "http://127.0.0.1:9999"}, ToolContext(workspace, allow_localhost=False), "act_local"
            )
        )


def test_pinned_backend_connects_to_the_validated_address(monkeypatch):
    calls = []

    class Backend:
        async def connect_tcp(self, host, port, **_kwargs):
            calls.append((host, port))
            return object()

    monkeypatch.setattr(
        "clearact.tools.network.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("203.0.113.8", 0))],
    )
    result = asyncio.run(PinnedNetworkBackend(backend=Backend()).connect_tcp(b"example.test", 443))

    assert result is not None
    assert calls == [("203.0.113.8", 443)]


def test_safe_fetch_client_ignores_environment_proxies(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8888")

    options = client_kwargs(timeout=10, allow_loopback=False)

    assert options["trust_env"] is False
    assert "mounts" not in options


def test_fetch_falls_back_to_local_readability_and_marks_content_untrusted(monkeypatch, workspace):
    class Response:
        status_code = 200
        url = "https://example.com/report"
        headers = {"content-type": "text/html"}
        text = (
            "<html><title>Quarterly report</title><body><article>"
            "<h1>Results</h1><p>Revenue rose.</p></article></body></html>"
        )

        def raise_for_status(self):
            return None

    async def no_jina(self, *_args):
        return None

    async def local(self, *_args):
        return self._result(
            "# Results\n\nRevenue rose.",
            50_000,
            {
                "url": "https://example.com/report",
                "final_url": "https://example.com/report",
                "status_code": 200,
                "extractor": "readability",
            },
        )

    monkeypatch.setattr(FetchUrlTool, "_fetch_jina", no_jina)
    monkeypatch.setattr(FetchUrlTool, "_fetch_readability", local)
    result = asyncio.run(
        FetchUrlTool().execute({"url": "https://example.com/report"}, ToolContext(workspace), "act_fetch")
    )
    assert result.ok is True
    assert result.metadata["extractor"] == "readability"
    assert result.metadata["untrusted"] is True
    assert result.content.startswith("[External content")
    assert "Revenue rose" in result.content


def test_fetch_rejects_reader_proxied_not_found_page_and_falls_back(monkeypatch, workspace):
    class ReaderResponse:
        status_code = 200
        text = "# Page not found\n\nWE'RE SORRY. We could not find the page you requested."

        def raise_for_status(self):
            return None

        def json(self):
            raise json.JSONDecodeError("not json", self.text, 0)

    class ReaderClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return ReaderResponse()

    async def direct_fetch(self, *_args):
        return self._result(
            "# Snapdragon Wear 4100\n\nFour Cortex-A53 CPU cores.",
            50_000,
            {
                "url": "https://example.com/w4100",
                "final_url": "https://example.com/w4100",
                "status_code": 200,
                "extractor": "readability",
            },
        )

    # A reader proxy's own 200 must not make an origin 404 a usable source.
    monkeypatch.setattr("clearact.tools.web.httpx.AsyncClient", lambda **_kwargs: ReaderClient())
    monkeypatch.setattr(FetchUrlTool, "_fetch_readability", direct_fetch)
    result = asyncio.run(
        FetchUrlTool().execute({"url": "https://example.com/w4100"}, ToolContext(workspace), "act_fetch")
    )
    assert result.ok is True
    assert result.metadata["extractor"] == "readability"
    assert "Page not found" not in result.content
    assert "Cortex-A53" in result.content
