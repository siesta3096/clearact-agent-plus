import shutil
from pathlib import Path

import pytest

from clearact.settings import load_settings

PROJECT_ROOT = Path(__file__).parents[2]


@pytest.fixture()
def template_settings(tmp_path):
    """Load the checked-in template, not the local working clearact.json.

    The working config is gitignored and may carry real credentials, so the
    template assertions must run against a clean copy of clearact.json.example.
    """
    (tmp_path / "clearact.json").write_text(
        (PROJECT_ROOT / "clearact.json.example").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    shutil.copytree(PROJECT_ROOT / "config", tmp_path / "config")
    return load_settings(tmp_path)


def test_single_file_settings_favor_large_transparent_runs(template_settings):
    settings = template_settings
    assert settings.agent.max_iterations == 80
    assert settings.agent.max_tool_calls_per_run == 240
    assert settings.network.allow_localhost is True
    # The checked-in local template has no plaintext credential and can start
    # with its Ollama profile while preserving an environment-based cloud option.
    assert settings.models["default_profile"] == "ollama-local"
    assert isinstance(settings.models["profiles"]["openai-compatible"]["api_key"], str)


def test_environment_style_keys_are_normalized_for_runtime(template_settings):
    profile = template_settings.models["profiles"]["openai-compatible"]
    assert profile["base_url"] == "https://api.example.com/v1"
    assert profile["api_key_env"] == "OPENAI_API_KEY"
