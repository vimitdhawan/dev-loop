from __future__ import annotations

from pathlib import Path

from devloop.contracts.artifacts import EnvRecipe
from devloop.env.runner import parse_failures, run_checks


def recipe(setup: list[str], commands: dict[str, str]) -> EnvRecipe:
    return EnvRecipe(image="host", setup=setup, commands=commands, verified_at_commit="x")


def test_runs_every_command_and_records_phase(tmp_path: Path) -> None:
    results = run_checks(
        tmp_path,
        recipe([], {"lint": "true", "unit": "echo boom >&2; exit 1"}),
        phase="post_change",
        iteration=2,
        log_dir=tmp_path / "logs",
    )

    assert [(r.type, r.passed, r.phase, r.iteration) for r in results] == [
        ("lint", True, "post_change", 2),
        ("unit", False, "post_change", 2),
    ]
    assert "boom" in results[1].output
    assert (tmp_path / "logs" / "post_change-2-unit.log").exists()


def test_failed_setup_short_circuits(tmp_path: Path) -> None:
    results = run_checks(
        tmp_path,
        recipe(["true", "false"], {"unit": "true"}),
        phase="baseline",
        iteration=0,
        log_dir=tmp_path / "logs",
    )

    assert len(results) == 1
    assert results[0].type == "setup"
    assert not results[0].passed


def test_parse_failures_go_and_pytest() -> None:
    output = (
        "--- FAIL: TestParse (0.00s)\n"
        "    --- FAIL: TestParse/empty (0.00s)\n"
        "FAILED tests/test_x.py::test_y - AssertionError\n"
        "FAILED tests/test_x.py::test_y - AssertionError\n"
    )
    assert [f.name for f in parse_failures(output)] == [
        "TestParse",
        "TestParse/empty",
        "tests/test_x.py::test_y",
    ]
