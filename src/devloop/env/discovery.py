"""Find out how a repo builds and tests, without a model.

Priority order: `devloop.yml` (explicit, always wins) → lockfile/manifest
heuristics. `.devcontainer/` and CI-workflow parsing plus the Bootstrap
agent arrive with the Docker sandbox; until then a repo the heuristics
can't read needs a `devloop.yml`, and the task escalates saying so.

    # devloop.yml
    setup:
      - uv sync
    commands:          # name -> command; the name becomes TestRunResult.type
      lint: uv run ruff check .
      unit: uv run pytest -q
    app:               # optional; enables browser QA
      start: npm run dev -- -p 3100
      url: http://localhost:3100
      ready_timeout_s: 120
      env: {NEXT_PUBLIC_API_URL: http://localhost:8080}
      setup: [supabase start]       # before start
      teardown: [supabase stop]     # after the app is stopped
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from devloop.contracts.artifacts import AppSpec, EnvRecipe
from devloop.errors import EnvError

HOST_IMAGE = "host"


class DevloopYml(BaseModel):
    setup: list[str] = Field(default_factory=list)
    commands: dict[str, str] = Field(default_factory=dict)
    app: AppSpec | None = None

    @field_validator("setup", "commands", mode="before")
    @classmethod
    def _yaml_scalars_are_commands(cls, value: Any) -> Any:
        # YAML reads `unit: true` as a bool; a command is always a string,
        # so don't make users quote `true`.
        if isinstance(value, dict):
            return {k: _as_command(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_as_command(v) for v in value]
        return value


def _as_command(value: Any) -> Any:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int | float):
        return str(value)
    return value


def discover_recipe(workspace: Path, base_commit: str) -> EnvRecipe | None:
    config = workspace / "devloop.yml"
    if config.exists():
        try:
            raw = yaml.safe_load(config.read_text()) or {}
            parsed = DevloopYml.model_validate(raw)
        except (yaml.YAMLError, ValidationError) as exc:
            raise EnvError(f"invalid devloop.yml: {exc}") from exc
        recipe = _recipe(parsed.setup, parsed.commands, base_commit)
        recipe.app = parsed.app
        return recipe

    detected = _heuristic(workspace)
    if detected is None:
        return None
    setup, commands = detected
    return _recipe(setup, commands, base_commit)


def _recipe(setup: list[str], commands: dict[str, str], base_commit: str) -> EnvRecipe:
    return EnvRecipe(
        image=HOST_IMAGE,
        setup=setup,
        commands=commands,
        verified_at_commit=base_commit,
    )


def _heuristic(ws: Path) -> tuple[list[str], dict[str, str]] | None:
    if (ws / "go.mod").exists():
        return [], {"build": "go build ./...", "vet": "go vet ./...", "unit": "go test ./..."}

    if (ws / "pyproject.toml").exists() and (ws / "uv.lock").exists():
        return ["uv sync"], {"unit": "uv run pytest -q"}

    package_json = ws / "package.json"
    if package_json.exists():
        try:
            scripts = json.loads(package_json.read_text()).get("scripts", {})
        except json.JSONDecodeError as exc:
            raise EnvError(f"invalid package.json: {exc}") from exc
        if "test" not in scripts:
            return None
        if (ws / "pnpm-lock.yaml").exists():
            setup, run = "pnpm install --frozen-lockfile", "pnpm"
        elif (ws / "yarn.lock").exists():
            setup, run = "yarn install --frozen-lockfile", "yarn"
        else:
            setup, run = "npm ci" if (ws / "package-lock.json").exists() else "npm install", "npm"
        commands = {"unit": f"{run} test"}
        if "lint" in scripts:
            commands["lint"] = f"{run} run lint"
        return [setup], commands

    return None
