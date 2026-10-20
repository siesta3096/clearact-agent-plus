"""Do not turn missing model usage into zero-cost evaluation claims."""

import runpy
from pathlib import Path

import pytest

meter = runpy.run_path(str(Path(__file__).parents[2] / "scripts/evaluate_lightweight.py"))["reported_tokens"]


@pytest.mark.parametrize("requests", [[], [{"status": 200, "usage": {}}], [{"status": None}],
                                    [{"status": 200, "usage": {"completion_tokens": 10}}]])
def test_missing_usage_remains_unknown(requests):
    assert meter(requests)["prompt_tokens"] is None
    assert meter(requests)["usage_reported"] is False


def test_actual_http_usage_counts_successful_fallback_and_all_reported_output():
    result = meter([{"status": 400}, {"status": 200, "usage": {"prompt_tokens": 20, "completion_tokens": 4}},
                    {"status": 200, "usage": {"prompt_tokens": 30, "completion_tokens": 0}}])
    assert result == {"prompt_tokens": 50, "completion_tokens": 4, "usage_reported": True}


def test_native_ollama_counts_and_genuine_zero_are_distinguished_from_missing():
    result = meter([{"status": 200, "prompt_eval_count": 20, "eval_count": 0}])
    assert result == {"prompt_tokens": 20, "completion_tokens": 0, "usage_reported": True}
