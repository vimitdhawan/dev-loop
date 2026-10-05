from __future__ import annotations

import socket
import sys
import urllib.request
from pathlib import Path

import pytest

from devloop.contracts.artifacts import AppSpec
from devloop.env.app import AppEnvironmentError, AppError, running_app


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http_server(port: int, **kw: object) -> AppSpec:
    return AppSpec(
        start=f"{sys.executable} -m http.server {port} --bind 127.0.0.1",
        url=f"http://127.0.0.1:{port}/",
        ready_timeout_s=15,
        **kw,  # type: ignore[arg-type]
    )


def answers(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=1)
        return True
    except OSError:
        return False


def test_starts_serves_stops_and_runs_hooks(tmp_path: Path) -> None:
    port = free_port()
    spec = http_server(port, setup=["echo up > setup.txt"], teardown=["echo down > teardown.txt"])

    with running_app(tmp_path, spec, tmp_path / "logs"):
        assert answers(spec.url)
        assert (tmp_path / "setup.txt").exists()

    assert not answers(spec.url), "the app must be stopped afterwards"
    assert (tmp_path / "teardown.txt").exists()


def test_port_already_in_use_is_an_environment_problem(tmp_path: Path) -> None:
    port = free_port()
    with running_app(tmp_path, http_server(port), tmp_path / "logs"):
        with pytest.raises(AppEnvironmentError, match="already in use"):
            with running_app(tmp_path, http_server(port), tmp_path / "logs2"):
                pass


def test_app_that_exits_is_a_change_problem(tmp_path: Path) -> None:
    spec = AppSpec(start="echo boom; exit 3", url=f"http://127.0.0.1:{free_port()}/")

    with pytest.raises(AppError, match="boom") as info:
        with running_app(tmp_path, spec, tmp_path / "logs"):
            pass
    assert not isinstance(info.value, AppEnvironmentError)


def test_failing_setup_hook_is_an_environment_problem_and_still_tears_down(
    tmp_path: Path,
) -> None:
    spec = http_server(free_port(), setup=["exit 1"], teardown=["touch torn-down"])

    with pytest.raises(AppEnvironmentError):
        with running_app(tmp_path, spec, tmp_path / "logs"):
            pass
    assert (tmp_path / "torn-down").exists()
