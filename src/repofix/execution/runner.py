import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from repofix.execution.models import TestRunResult, TestRunStatus
from repofix.execution.parser import parse_pytest_report
from repofix.execution.pytest_capture import (
    REPORT_PATH_ENVIRONMENT_VARIABLE,
)


PYTEST_CAPTURE_PLUGIN = "repofix.execution.pytest_capture"

def _normalize_command(command: str) -> tuple[str, ...]:
    """把输入的字符串命令转为子进程的参数"""
    #参数
    arguments = tuple(shlex.split(command))

    #确保命令不为空
    if not arguments:
        raise ValueError("test command must not be empty")

    #确保输入的是一个python命令,替换第一个参数为环境可执行python
    if arguments[0].lower() in {
        "python",
        "python.exe",
        "python3",
        "py",
    }:
        arguments = (
            sys.executable,
            *arguments[1:],
        )
    return arguments

def _build_environment(working_path:Path)->dict[str,str]:
    #eg: 分页的benchmark在 benchmarks/task/.../repo/src里面
    # repo里面存放的是有bug的文件 repo/src 源文件
    # reference存放正确修改的文件 reference/src

    #grader_tests存放agent不可见的测试
    #repo/sre/tests存放agent可见测试
    environment = os.environ.copy()
    source_path = working_path / "src"
    if source_path.is_dir():
        previous_python_path = environment.get("PYTHONPATH")
        if previous_python_path:
            environment["PYTHONPATH"] = (
                f"{source_path}{os.pathsep}{previous_python_path}"
            )
        else:
            environment["PYTHONPATH"] = str(source_path)

    environment["PYTHONUTF8"] = "1"
    return environment


def _status_from_exit_code(exit_code: int) -> TestRunStatus:
    """把 pytest 退出码转换成 RepoFix 测试状态。"""

    if exit_code == 0:
        return TestRunStatus.PASSED

    if exit_code == 1:
        return TestRunStatus.FAILED

    return TestRunStatus.ERROR


def _is_pytest_command(arguments: tuple[str, ...]) -> bool:
    """Return whether arguments launch pytest directly or via Python."""

    if len(arguments) >= 3 and arguments[1:3] == ("-m", "pytest"):
        return True

    executable_name = Path(arguments[0]).stem.lower()
    return executable_name == "pytest"


def _instrument_pytest_command(arguments: tuple[str, ...]) -> tuple[str, ...]:
    """Load RepoFix's capture plugin without changing the task test files."""

    if len(arguments) >= 3 and arguments[1:3] == ("-m", "pytest"):
        return (
            *arguments[:3],
            "-p",
            PYTEST_CAPTURE_PLUGIN,
            *arguments[3:],
        )

    return (
        arguments[0],
        "-p",
        PYTEST_CAPTURE_PLUGIN,
        *arguments[1:],
    )

def _output_to_text(output: str | bytes | None) -> str:
    """Convert subprocess output into text."""

    if output is None:
        return ""

    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")

    return output


def run_test_command(
    working_path: Path,
    command: str,
    timeout_seconds: float,
) -> TestRunResult:
    """Run one test command and return its structured result."""

    working_path = working_path.resolve()

    if not working_path.is_dir():
        raise FileNotFoundError(
            f"test working directory does not exist: {working_path}"
        )

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than 0")

    requested_arguments = _normalize_command(command)
    environment = _build_environment(working_path)

    with TemporaryDirectory(prefix="repofix-pytest-report-") as report_directory:
        report_path = Path(report_directory) / "report.json"
        is_pytest = _is_pytest_command(requested_arguments)

        if is_pytest:
            arguments = _instrument_pytest_command(requested_arguments)
            environment[REPORT_PATH_ENVIRONMENT_VARIABLE] = str(report_path)
        else:
            arguments = requested_arguments

        started_at = time.perf_counter()

        try:
            completed_process = subprocess.run(
                arguments,
                cwd=working_path,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                shell=False,
            )

            duration_seconds = time.perf_counter() - started_at
            status = _status_from_exit_code(
                completed_process.returncode
            )

            testfailures = ()
            stderr = completed_process.stderr

            if is_pytest:
                try:
                    testfailures = parse_pytest_report(report_path)
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    status = TestRunStatus.ERROR
                    stderr = (
                        f"{stderr}\nRepoFix could not parse pytest report: {error}"
                    ).strip()

            return TestRunResult(
                status=status,
                command=arguments,
                working_path=working_path,
                exit_code=completed_process.returncode,
                stdout=completed_process.stdout,
                stderr=stderr,
                duration_seconds=duration_seconds,
                testfailures=testfailures,
            )

        except subprocess.TimeoutExpired as error:
            duration_seconds = time.perf_counter() - started_at

            return TestRunResult(
                status=TestRunStatus.TIMEOUT,
                command=arguments,
                working_path=working_path,
                exit_code=None,
                stdout=_output_to_text(error.stdout),
                stderr=_output_to_text(error.stderr),
                duration_seconds=duration_seconds,
                testfailures=(),
            )

        except OSError as error:
            duration_seconds = time.perf_counter() - started_at

            return TestRunResult(
                status=TestRunStatus.ERROR,
                command=arguments,
                working_path=working_path,
                exit_code=None,
                stdout="",
                stderr=str(error),
                duration_seconds=duration_seconds,
                testfailures=(),
            )
