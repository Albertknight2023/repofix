"""Run untrusted pytest code in a disposable, constrained Linux container."""

import os
import shlex
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from repofix.execution.models import TestFailure, TestRunResult, TestRunStatus, TracebackFrame
from repofix.execution.parser import parse_pytest_report
from repofix.execution.runner import _output_to_text, _status_from_exit_code


DEFAULT_IMAGE = "repofix-pytest:py311"
_CONTAINER_REPO = "/workspace/repo"
_REPORT_PATH = "/workspace/report/report.json"


def _pytest_arguments(command: str) -> tuple[str, ...]:
    """Accept only pytest invocation; never pass a task command through a shell."""

    args = tuple(shlex.split(command))
    if len(args) >= 3 and args[0].lower() in {"python", "python3", "python.exe"} and args[1:3] == ("-m", "pytest"):
        return ("python", "-m", "pytest", "-p", "repofix.execution.pytest_capture", "-p", "no:cacheprovider", *args[3:])
    if args and Path(args[0]).name.lower() in {"pytest", "pytest.exe"}:
        return ("python", "-m", "pytest", "-p", "repofix.execution.pytest_capture", "-p", "no:cacheprovider", *args[1:])
    raise ValueError("Docker runner accepts only python -m pytest or pytest commands")


def _host_frame(frame: TracebackFrame, working_path: Path) -> TracebackFrame:
    path = frame.file_path
    container_path = str(path).replace("\\", "/")
    if container_path.startswith(f"{_CONTAINER_REPO}/"):
        path = working_path / container_path[len(_CONTAINER_REPO) + 1 :]
    return replace(frame, file_path=path)


def _host_failure(failure: TestFailure, working_path: Path) -> TestFailure:
    return replace(
        failure,
        traceback_frames=tuple(_host_frame(frame, working_path) for frame in failure.traceback_frames),
    )


class DockerTestRunner:
    """Callable test backend for public validation and offline grading.

    The repository is mounted read-only. Hidden tests are mounted separately,
    only when the command explicitly requests ../grader_tests.
    """

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        *,
        docker_binary: str = "docker",
        memory: str = "1g",
        cpus: str = "1",
        pids_limit: int = 128,
    ) -> None:
        if not image or image.startswith("-"):
            raise ValueError("Docker image name is invalid")
        self.image = image
        self.docker_binary = docker_binary
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit

    def __call__(self, working_path: Path, command: str, timeout_seconds: float) -> TestRunResult:
        working_path = working_path.resolve()
        if not working_path.is_dir():
            raise FileNotFoundError(f"test working directory does not exist: {working_path}")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        requested = _pytest_arguments(command)
        hidden_requested = "../grader_tests" in requested
        hidden_path = working_path.parent / "grader_tests"
        if hidden_requested and not hidden_path.is_dir():
            raise FileNotFoundError("hidden test directory is not staged for grading")

        name = f"repofix-test-{uuid4().hex[:16]}"
        with TemporaryDirectory(prefix="repofix-docker-report-") as report_directory:
            report_root = Path(report_directory)
            # A non-root user in the container needs to create the report file.
            os.chmod(report_root, 0o777)
            report_path = report_root / "report.json"
            command_line = [
                self.docker_binary, "run", "--rm", "--name", name,
                "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--user", "65534:65534",
                "--memory", self.memory, "--cpus", self.cpus,
                "--pids-limit", str(self.pids_limit),
                "--tmpfs", "/tmp:rw,nosuid,size=256m",
                "--mount", f"type=bind,src={working_path},dst={_CONTAINER_REPO},readonly",
                "--mount", f"type=bind,src={report_root},dst=/workspace/report",
                "--env", f"REPOFIX_PYTEST_REPORT_PATH={_REPORT_PATH}",
                "--env", "PYTHONPATH=/opt/repofix:/workspace/repo/src:/workspace/repo",
                "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                "--workdir", str(_CONTAINER_REPO),
            ]
            if hidden_requested:
                command_line.extend((
                    "--mount",
                    f"type=bind,src={hidden_path},dst=/workspace/grader_tests,readonly",
                ))
            command_line.extend((self.image, *requested))

            started = time.perf_counter()
            try:
                process = subprocess.run(
                    command_line,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                    check=False,
                    shell=False,
                )
                duration = time.perf_counter() - started
                status = _status_from_exit_code(process.returncode)
                failures: tuple[TestFailure, ...] = ()
                stderr = process.stderr
                if report_path.is_file():
                    try:
                        failures = tuple(
                            _host_failure(failure, working_path)
                            for failure in parse_pytest_report(report_path)
                        )
                    except (OSError, ValueError) as error:
                        status = TestRunStatus.ERROR
                        stderr = f"{stderr}\nRepoFix could not parse container report: {error}".strip()
                else:
                    status = TestRunStatus.ERROR
                    stderr = f"{stderr}\nRepoFix container did not produce a pytest report.".strip()
                return TestRunResult(
                    status=status,
                    command=requested,
                    working_path=working_path,
                    exit_code=process.returncode,
                    stdout=process.stdout,
                    stderr=stderr,
                    duration_seconds=duration,
                    testfailures=failures,
                )
            except subprocess.TimeoutExpired as error:
                subprocess.run(
                    [self.docker_binary, "rm", "-f", name],
                    capture_output=True, timeout=15, check=False, shell=False,
                )
                return TestRunResult(
                    status=TestRunStatus.TIMEOUT,
                    command=requested,
                    working_path=working_path,
                    exit_code=None,
                    stdout=_output_to_text(error.stdout),
                    stderr=_output_to_text(error.stderr),
                    duration_seconds=time.perf_counter() - started,
                    testfailures=(),
                )
            except OSError as error:
                return TestRunResult(
                    status=TestRunStatus.ERROR,
                    command=requested,
                    working_path=working_path,
                    exit_code=None,
                    stdout="",
                    stderr=str(error),
                    duration_seconds=time.perf_counter() - started,
                    testfailures=(),
                )
