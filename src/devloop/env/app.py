"""Run the target app for QA: setup → start → wait until it answers →
(QA tests it) → stop → teardown.

The orchestrator owns this lifecycle, not the QA agent: an agent that
starts servers can leave them running, bind the wrong port, or test a
stale build. Runs on the host for now, like the checks.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from devloop.contracts.artifacts import AppSpec
from devloop.errors import EnvError

log = logging.getLogger("devloop.app")

_STOP_GRACE_S = 10
_TAIL = 2_000


class AppError(EnvError):
    """The app could not be brought up. Carries the output tail so the
    Engineer can be told *why*."""


class AppEnvironmentError(AppError):
    """The app can't be tested for a reason the change didn't cause — the
    port is taken, a setup hook (`supabase start`) failed. Escalates
    instead of being sent to the Engineer as a bug."""


@contextmanager
def running_app(workspace: Path, spec: AppSpec, log_dir: Path) -> Iterator[None]:
    log_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **spec.env}
    try:
        for command in spec.setup:
            _run_hook(workspace, command, env, log_dir / "app-setup.log")

        host, port = _host_port(spec.url)
        if _port_open(host, port):
            # Something else (often your own dev server) already answers
            # there — testing it would test the wrong code.
            raise AppEnvironmentError(
                f"{host}:{port} is already in use; stop whatever is listening there"
            )

        log_path = log_dir / "app.log"
        with log_path.open("w") as out:
            proc = subprocess.Popen(
                spec.start,
                shell=True,  # from the repo's own devloop.yml
                cwd=workspace,
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        try:
            _wait_ready(proc, spec, log_path)
            log.info("app ready at %s — log: %s", spec.url, log_path)
            yield
        finally:
            _stop(proc)
    finally:
        for command in spec.teardown:
            try:
                _run_hook(workspace, command, env, log_dir / "app-teardown.log")
            except AppEnvironmentError as exc:  # teardown must not mask the real outcome
                log.warning("app teardown failed: %s", exc)


def _run_hook(workspace: Path, command: str, env: dict[str, str], log_path: Path) -> None:
    """Setup/teardown hooks: environment, not the change under test."""

    proc = subprocess.run(
        command, shell=True, cwd=workspace, env=env, capture_output=True, text=True, timeout=600
    )
    with log_path.open("a") as fh:
        fh.write(f"$ {command}\n{proc.stdout}{proc.stderr}\n")
    if proc.returncode != 0:
        raise AppEnvironmentError(f"`{command}` failed: {(proc.stdout + proc.stderr)[-_TAIL:]}")


def _host_port(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    if not parsed.hostname:
        raise AppError(f"app.url {url!r} has no host")
    return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _wait_ready(proc: subprocess.Popen[bytes], spec: AppSpec, log_path: Path) -> None:
    deadline = time.monotonic() + spec.ready_timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AppError(
                f"`{spec.start}` exited with {proc.returncode} before {spec.url} answered:\n"
                f"{log_path.read_text()[-_TAIL:]}"
            )
        try:
            with urllib.request.urlopen(spec.url, timeout=2) as resp:  # noqa: S310 (operator URL)
                if resp.status < 500:
                    return
        except urllib.error.HTTPError as exc:
            if exc.code < 500:  # a 404 or a redirect to sign-in still means "up"
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    raise AppError(
        f"{spec.url} did not answer within {spec.ready_timeout_s}s:\n"
        f"{log_path.read_text()[-_TAIL:]}"
    )


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    for sig, wait_s in ((signal.SIGTERM, _STOP_GRACE_S), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait_s)
            return
        except subprocess.TimeoutExpired:
            continue
