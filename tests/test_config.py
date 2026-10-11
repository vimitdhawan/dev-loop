from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from devloop.config import ConfigError, load_config
from devloop.contracts.runs import AgentConfig, McpServerConfig, Role, TeamConfig

CONFIG = """\
agents:
  engineer: {runtime: claude, model: opus, timeout_s: 2400}
  qa: {runtime: opencode}
repo: {url: https://github.com/org/repo.git, base_branch: develop}
pull_request: {draft: true}
budget_usd: 5
"""


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for var in ("DEVLOOP_CONFIG", "DEVLOOP_REPO_URL", "DEVLOOP_BASE_BRANCH"):
        monkeypatch.delenv(var, raising=False)


def test_defaults_without_a_file() -> None:
    config = load_config()
    assert config.agents.for_role(Role.REVIEWER).runtime == "claude"
    assert config.repo.url is None and config.pull_request.enabled


def test_reads_cwd_file(tmp_path: Path) -> None:
    (tmp_path / "devloop.config.yaml").write_text(CONFIG)

    config = load_config()

    engineer = config.agents.for_role(Role.ENGINEER)
    assert (engineer.model, engineer.timeout_s) == ("opus", 2400)
    assert config.agents.for_role(Role.QA).runtime == "opencode"
    assert config.agents.for_role(Role.PRODUCT_OWNER).runtime == "claude"  # default kept
    assert config.repo.base_branch == "develop" and config.pull_request.draft
    assert config.budget_usd == 5


def test_home_file_is_the_fallback(devloop_home: Path) -> None:
    devloop_home.mkdir(parents=True)
    (devloop_home / "config.yaml").write_text("budget_usd: 7\n")
    assert load_config().budget_usd == 7


def test_env_overrides_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "devloop.config.yaml").write_text(CONFIG)
    monkeypatch.setenv("DEVLOOP_REPO_URL", "git@github.com:other/repo.git")
    monkeypatch.setenv("DEVLOOP_BASE_BRANCH", "main")

    config = load_config()

    assert config.repo.url == "git@github.com:other/repo.git"
    assert config.repo.base_branch == "main"


def test_explicit_path_must_exist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEVLOOP_CONFIG", str(tmp_path / "missing.yaml"))
    with pytest.raises(ConfigError, match="does not exist"):
        load_config()


@pytest.mark.parametrize(
    "content",
    ["agents: {engineer: {timeout_s: 5}}", "agents: {intern: {}}", "budget_usd: [1"],
)
def test_invalid_config_is_rejected(tmp_path: Path, content: str) -> None:
    (tmp_path / "devloop.config.yaml").write_text(content)
    with pytest.raises(ConfigError):
        load_config()


# --- roles added after a config was written --------------------------------


def test_unset_planner_and_ux_run_as_their_fallback_roles() -> None:
    team = TeamConfig(
        engineer=AgentConfig(runtime="opencode", model="nvidia/x"),
        product_owner=AgentConfig(model="sonnet"),
    )

    assert team.for_role(Role.PLANNER).model == "nvidia/x"
    assert team.for_role(Role.UX).model == "sonnet"
    team.engineer.model = "opus"
    assert team.for_role(Role.PLANNER).model == "opus", "follows the engineer until set"

    team.own(Role.PLANNER).model = "haiku"
    assert team.for_role(Role.PLANNER).model == "haiku"
    assert team.engineer.model == "opus", "overriding the planner leaves the engineer alone"


def test_mcp_server_needs_exactly_one_transport() -> None:
    assert McpServerConfig(url="https://x/mcp").url
    assert McpServerConfig(command=["npx", "srv"]).command
    bad_configs: list[dict[str, object]] = [
        {},
        {"url": "https://x", "command": ["a"]},
        {"command": []},
    ]
    for bad in bad_configs:
        with pytest.raises(ValidationError):
            McpServerConfig.model_validate(bad)


def test_the_example_config_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    example = Path(__file__).parents[1] / "devloop.config.example.yaml"
    monkeypatch.setenv("DEVLOOP_CONFIG", str(example))

    config = load_config()

    assert config.agents.for_role(Role.UX).mcp_servers["stitch"].url
    assert config.github.labels == ["devloop"]
