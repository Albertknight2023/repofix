"""Security and evidence checks for the optional container backend."""

import shutil
import subprocess
from pathlib import Path

import pytest

from repofix.benchmarks.loader import load_task
from repofix.execution.docker_runner import (
    DockerTestRunner,
    _host_frame,
    _pytest_arguments,
)
from repofix.execution.models import TestRunStatus as RunStatus, TracebackFrame
from repofix.workspace.manager import isolated_workspace


ROOT = Path(__file__).resolve().parents[1]


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "repofix-pytest:py311"],
            capture_output=True, timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def test_docker_runner_refuses_non_pytest_commands() -> None:
    with pytest.raises(ValueError, match="only"):
        _pytest_arguments("python -c 'print(1)'")


def test_docker_frame_maps_to_current_workspace() -> None:
    frame = TracebackFrame(Path("/workspace/repo/src/example.py"), 7, "example")
    mapped = _host_frame(frame, ROOT)
    assert mapped.file_path == ROOT / "src" / "example.py"


@pytest.mark.skipif(not _docker_ready(), reason="local RepoFix Docker image unavailable")
def test_container_captures_real_failure_without_host_execution() -> None:
    task = load_task(ROOT / "benchmarks/tasks/pagination-zero-page-size/task.yaml")
    with isolated_workspace(task) as workspace:
        result = DockerTestRunner()(workspace.repo_path, task.public_test_command, 60)
        assert result.status is RunStatus.FAILED
        assert result.testfailures
        assert any(
            frame.file_path == workspace.repo_path / "src/pagination.py"
            for failure in result.testfailures
            for frame in failure.traceback_frames
        )
