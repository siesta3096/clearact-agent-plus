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


def test_web_assets_keep_untrusted_content_out_of_html_attributes():
    html = (webapp._ASSET_DIR / "index.html").read_text(encoding="utf-8")
    script = (webapp._ASSET_DIR / "app.js").read_text(encoding="utf-8")

    assert 'id="approval-dialog"' in html
    assert "data-copy=\"${" not in script
    assert "button.dataset.copy=final.content" in script
    assert "safeHttpUrl" in script
    assert "replace(/[&<>\"']/g" in script
