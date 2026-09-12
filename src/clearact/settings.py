from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import AliasChoices, BaseModel, Field

from clearact.domain.enums import RiskLevel, ViewMode

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::([^}]*))?\}")


class AgentSettings(BaseModel):
    max_iterations: int = Field(default=80, validation_alias=AliasChoices("maxIterations", "max_iterations"))
    max_tool_calls_per_run: int = Field(
        default=240, validation_alias=AliasChoices("maxToolCallsPerRun", "max_tool_calls_per_run")
    )
    tool_timeout_seconds: float = Field(
        default=60, validation_alias=AliasChoices("toolTimeoutSeconds", "tool_timeout_seconds")
    )
    context_budget_ratio: float = Field(
        default=0.80, validation_alias=AliasChoices("contextBudgetRatio", "context_budget_ratio")
    )


class NetworkSettings(BaseModel):
    allow_localhost: bool = Field(default=True, validation_alias=AliasChoices("allowLocalhost", "allow_localhost"))


class AppSettings(BaseModel):
    project_root: Path
    workspace_root: Path
    data_root: Path
    agent: AgentSettings
    network: NetworkSettings
    default_autonomy: RiskLevel
    default_capability_rules: dict[str, str]
    default_view_mode: ViewMode
    models: dict[str, Any]
    tools: dict[str, Any]
    risk_rules: dict[str, Any]
    mcp_servers: dict[str, dict[str, Any]]


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda match: os.getenv(match.group(1), match.group(2) or ""), value)
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return _expand_env(yaml.safe_load(handle) or {})


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return _expand_env(json.load(handle))


def _model_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize the human-friendly JSON keys at the runtime boundary."""
    profiles = {}
    for name, raw_profile in config["profiles"].items():
        profile = dict(raw_profile)
        profile["base_url"] = profile.pop("baseUrl")
        profile["context_window"] = profile.pop("contextWindow")
        profile["api_key"] = profile.pop("apiKey", "")
        if "apiKeyEnv" in profile:
            profile["api_key_env"] = profile.pop("apiKeyEnv")
        profiles[name] = profile
    return {"default_profile": config["defaultProfile"], "profiles": profiles}


def load_settings(project_root: Path | None = None) -> AppSettings:
    root = (project_root or Path.cwd()).resolve()
    config_dir = root / "config"
    config = _load_json(root / "clearact.json")
    tools = _load_yaml(config_dir / "tools.yaml")
    risk_rules = _load_yaml(config_dir / "risk_rules.yaml")
    return AppSettings(
        project_root=root,
        workspace_root=(root / config["workspace"]["defaultRoot"]).resolve(),
        data_root=(root / config["storage"]["dataRoot"]).resolve(),
        agent=AgentSettings(**config["agent"]),
        network=NetworkSettings(**config.get("network", {})),
        default_autonomy=RiskLevel(config["policy"]["defaultAutonomy"]),
        default_capability_rules={
            str(name): str(rule)
            for name, rule in config.get("policy", {}).get("defaultCapabilities", {}).items()
            if rule in {"allow", "ask", "deny"}
        },
        default_view_mode=ViewMode(config["policy"]["defaultViewMode"]),
        models=_model_settings(config),
        tools=tools["tools"],
        risk_rules=risk_rules,
        # Accept both ClearAct's section and the de-facto mcpServers import shape.
        mcp_servers={
            str(name): dict(server)
            for name, server in (config.get("mcp", {}).get("servers", config.get("mcpServers", {})) or {}).items()
            if isinstance(server, dict)
        },
    )
