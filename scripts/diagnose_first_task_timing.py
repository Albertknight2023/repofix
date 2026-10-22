"""One bounded, instrumented diagnostic run of the frozen dataset's first task.

This script lives outside the frozen Agent implementation and does not change
the formal experiment protocol. It logs timing metadata, never prompts or keys.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from repofix.cli import _create_client, _parser
from repofix.experiments import run_experiment
import repofix.llm.client as client_module


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FREEZE_PATH = PROJECT_ROOT / "docs/formal-freeze-20261015-v2.json"
HARD_TIMEOUT_SECONDS = 240


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.started = perf_counter()

    def write(self, event: str, **details: object) -> None:
        record = {
            "event": event,
            "utc": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(perf_counter() - self.started, 3),
            **details,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()


class TimedResponse:
    def __init__(self, response: object, log: EventLog) -> None:
        self.response = response
        self.log = log

    def __enter__(self) -> "TimedResponse":
        self.response.__enter__()
        return self

    def __exit__(self, *args: object) -> object:
        return self.response.__exit__(*args)

    def read(self, size: int) -> bytes:
        self.log.write("body_read_started")
        try:
            first = self.response.read(1)
            self.log.write("first_body_byte", received=bool(first))
            rest = self.response.read(size - 1)
            body = first + rest
            self.log.write("body_read_finished", bytes_read=len(body))
            return body
        except BaseException as error:
            self.log.write("body_read_error", error_type=type(error).__name__)
            raise


def instrument_http(log: EventLog) -> None:
    original_build_opener = client_module.build_opener
    original_urlopen = client_module.urlopen

    def timed_build_opener(*handlers: object) -> object:
        opener = original_build_opener(*handlers)
        original_open = opener.open

        def timed_open(*args: object, **kwargs: object) -> TimedResponse:
            log.write("http_open_started")
            try:
                response = original_open(*args, **kwargs)
            except BaseException as error:
                log.write("http_open_error", error_type=type(error).__name__)
                raise
            log.write("http_headers_received")
            return TimedResponse(response, log)

        opener.open = timed_open
        return opener

    def timed_urlopen(*args: object, **kwargs: object) -> TimedResponse:
        log.write("http_open_started")
        try:
            response = original_urlopen(*args, **kwargs)
        except BaseException as error:
            log.write("http_open_error", error_type=type(error).__name__)
            raise
        log.write("http_headers_received")
        return TimedResponse(response, log)

    client_module.build_opener = timed_build_opener
    client_module.urlopen = timed_urlopen


class TimedClient:
    def __init__(self, client: object, log: EventLog) -> None:
        self.client = client
        self.log = log

    def __getattr__(self, name: str) -> object:
        return getattr(self.client, name)

    def complete(self, messages: object) -> object:
        self.log.write(
            "model_call_started",
            message_count=len(messages),
            prompt_characters=sum(len(message.content) for message in messages),
        )
        try:
            response = self.client.complete(messages)
        except BaseException as error:
            self.log.write("model_call_error", error_type=type(error).__name__)
            raise
        self.log.write(
            "model_call_finished",
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
        )
        return response


def run_worker(log_path: Path, output_root: Path) -> int:
    log = EventLog(log_path)
    freeze = json.loads(FREEZE_PATH.read_text(encoding="utf-8"))
    task_id = freeze["ready_task_ids"][0]
    log.write("diagnostic_started", task_id=task_id)
    args = _parser().parse_args([
        "experiment",
        "--manifest", str(PROJECT_ROOT / freeze["manifest"]),
        "--method", "first-task-timing-diagnostic",
        "--provider", "api",
        "--runner", "docker",
        "--workflow", "single-agent",
        "--context", "ast-callgraph",
        "--model", freeze["protocol"]["model"],
        "--max-tokens", "6000",
        "--model-timeout", "180",
        "--thinking", "disabled",
        "--max-model-calls", "1",
        "--max-submission-attempts", "1",
        "--task-id", task_id,
        "--formal",
        "--output-root", str(output_root),
    ])
    instrument_http(log)
    client = TimedClient(_create_client(args), log)
    log.write("preflight_started")
    try:
        result = run_experiment(
            manifest_path=args.manifest,
            method=args.method,
            locator_client=client,
            repair_client=client,
            output_root=args.output_root,
            backend=args.runner,
            workflow_mode=args.workflow,
            context_mode=args.context,
            task_ids=tuple(args.task_id),
            max_model_calls=args.max_model_calls,
            formal=args.formal,
            max_submission_attempts=args.max_submission_attempts,
        )
    except BaseException as error:
        log.write(
            "diagnostic_error", error_type=type(error).__name__,
            error_message=str(error)[:300],
        )
        raise
    log.write(
        "diagnostic_finished",
        formal_status=result["formal_status"],
        evaluation_path=result["evaluation_path"],
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--log-path", type=Path)
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    if args.worker:
        if args.log_path is None or args.output_root is None:
            parser.error("worker requires log and output paths")
        return run_worker(args.log_path, args.output_root)

    run_directory = PROJECT_ROOT / "runs/diagnostics" / (
        f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    )
    run_directory.mkdir(parents=True, exist_ok=False)
    log_path = run_directory / "timing.jsonl"
    command = [
        sys.executable, str(Path(__file__).resolve()),
        "--worker", "--log-path", str(log_path),
        "--output-root", str(run_directory / "experiments"),
    ]
    try:
        process = subprocess.run(
            command, cwd=PROJECT_ROOT, text=True, capture_output=True,
            timeout=HARD_TIMEOUT_SECONDS, check=False,
        )
        status = "finished" if process.returncode == 0 else "worker_error"
        error_type = None if process.returncode == 0 else "WorkerExit"
    except subprocess.TimeoutExpired:
        status = "hard_timeout"
        error_type = "TimeoutExpired"
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "event": status,
            "utc": datetime.now(timezone.utc).isoformat(),
            "hard_timeout_seconds": HARD_TIMEOUT_SECONDS,
            "error_type": error_type,
        }) + "\n")
    print(json.dumps({
        "status": status, "timing_log": str(log_path),
        "run_directory": str(run_directory),
    }, ensure_ascii=False, indent=2))
    return 0 if status == "finished" else 1


if __name__ == "__main__":
    raise SystemExit(main())
