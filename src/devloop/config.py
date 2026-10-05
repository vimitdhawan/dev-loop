"""Operator configuration: who is on the agent team, where the code comes
from, and whether to open a PR.

Lookup order for the file (first found wins, none is fine):
`DEVLOOP_CONFIG` → `./devloop.config.yaml` → `$DEVLOOP_HOME/config.yaml`.

Precedence for each value: CLI flag > environment variable > file > default.
Environment variables: `DEVLOOP_REPO_URL`, `DEVLOOP_BASE_BRANCH`.

This is *operator* config — your model choices and credentials-adjacent
settings. How a target repo builds, tests and runs lives in that repo's own
`devloop.yml`. See `devloop.config.example.yaml`.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from devloop.contracts.runs import PullRequestConfig, TeamConfig
from devloop.contracts.state import BUDGET_USD_DEFAULT
from devloop.errors import DevLoopError
from devloop.paths import devloop_home

CONFIG_FILE = "devloop.config.yaml"


class ConfigError(DevLoopError):
    pass


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A clone URL or a local path. Overridden by `--repo` / DEVLOOP_REPO_URL.
    url: str | None = None
    base_branch: str | None = None


class DevLoopConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agents: TeamConfig = Field(default_factory=TeamConfig)
    repo: RepoConfig = Field(default_factory=RepoConfig)
    pull_request: PullRequestConfig = Field(default_factory=PullRequestConfig)
    budget_usd: float = BUDGET_USD_DEFAULT


def config_path() -> Path | None:
    explicit = os.environ.get("DEVLOOP_CONFIG")
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(f"DEVLOOP_CONFIG points at {path}, which does not exist")
        return path
    for candidate in (Path.cwd() / CONFIG_FILE, devloop_home() / "config.yaml"):
        if candidate.is_file():
            return candidate
    return None


def load_config() -> DevLoopConfig:
    path = config_path()
    raw: object = {}
    if path is not None:
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    try:
        config = DevLoopConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    if url := os.environ.get("DEVLOOP_REPO_URL"):
        config.repo.url = url
    if branch := os.environ.get("DEVLOOP_BASE_BRANCH"):
        config.repo.base_branch = branch
    return config
