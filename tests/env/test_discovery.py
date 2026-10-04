from __future__ import annotations

import json
from pathlib import Path

import pytest

from devloop.env.discovery import discover_recipe
from devloop.errors import EnvError


def test_devloop_yml_wins(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x")
    (tmp_path / "devloop.yml").write_text("setup: [make deps]\ncommands:\n  unit: make test\n")

    recipe = discover_recipe(tmp_path, "abc")

    assert recipe is not None
    assert recipe.setup == ["make deps"]
    assert recipe.commands == {"unit": "make test"}
    assert recipe.verified_at_commit == "abc"


@pytest.mark.parametrize("content", ["commands: [not, a, map]", "setup: [unclosed"])
def test_invalid_devloop_yml_raises(tmp_path: Path, content: str) -> None:
    (tmp_path / "devloop.yml").write_text(content)
    with pytest.raises(EnvError):
        discover_recipe(tmp_path, "abc")


@pytest.mark.parametrize(
    "files,expected_setup,expected_unit",
    [
        ({"go.mod": "module x"}, [], "go test ./..."),
        ({"pyproject.toml": "", "uv.lock": ""}, ["uv sync"], "uv run pytest -q"),
        (
            {"package.json": json.dumps({"scripts": {"test": "vitest"}}), "pnpm-lock.yaml": ""},
            ["pnpm install --frozen-lockfile"],
            "pnpm test",
        ),
        (
            {"package.json": json.dumps({"scripts": {"test": "jest"}}), "package-lock.json": ""},
            ["npm ci"],
            "npm test",
        ),
    ],
)
def test_heuristics(
    tmp_path: Path, files: dict[str, str], expected_setup: list[str], expected_unit: str
) -> None:
    for name, content in files.items():
        (tmp_path / name).write_text(content)

    recipe = discover_recipe(tmp_path, "abc")

    assert recipe is not None
    assert recipe.setup == expected_setup
    assert recipe.commands["unit"] == expected_unit


def test_package_json_without_test_script_is_not_enough(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {}}))
    assert discover_recipe(tmp_path, "abc") is None


def test_unknown_repo_returns_none(tmp_path: Path) -> None:
    assert discover_recipe(tmp_path, "abc") is None


def test_yaml_scalars_are_read_as_commands(tmp_path: Path) -> None:
    (tmp_path / "devloop.yml").write_text("setup: [true]\ncommands:\n  unit: true\n")

    recipe = discover_recipe(tmp_path, "abc")

    assert recipe is not None
    assert recipe.setup == ["true"] and recipe.commands == {"unit": "true"}
