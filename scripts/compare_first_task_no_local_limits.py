"""Exploratory same-task workflow comparison without local output/time caps.

DeepSeek still applies its own limits (8K default completion tokens in
non-thinking mode). This script never logs prompts, responses, or credentials.
It does not change the frozen RepoFix implementation or formal experiment data.
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from diagnose_first_task_timing import (
    EventLog,
    FREEZE_PATH,
    PROJECT_ROOT,
    TimedClient,
    instrument_http,
)
from repofix.cli import _create_client, _parser
from repofix.experiments import run_experiment
import repofix.llm.client as client_module


class UncappedTimedClient(TimedClient):
    @property
    def max_output_tokens(self) -> None:
        # No local max_tokens is sent; the provider supplies its default.
        return None


def omit_local_limits(log: EventLog) -> None:
    """Rewrite only request limits, leaving the frozen Agent logic unchanged."""

    instrument_http(log)
    timed_build_opener = client_module.build_opener
    timed_urlopen = client_module.urlopen

    def prepare_request(request: object) -> None:
        payload = json.loads(request.data)
        assert payload.pop("max_tokens", None) is not None
        request.data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        log.write(
            "request_prepared", explicit_max_tokens=False,
            json_mode=payload.get("response_format") == {"type": "json_object"},
            stream=payload.get("stream"),
        )

    def build_without_limits(*handlers: object) -> object:
        opener = timed_build_opener(*handlers)
        timed_open = opener.open

        def open_without_limits(request: object, **kwargs: object) -> object:
            prepare_request(request)
            # None means no socket-level timeout; no wall-clock deadline is set.
            return timed_open(request, timeout=None)

        opener.open = open_without_limits
        return opener

    def urlopen_without_limits(request: object, **kwargs: object) -> object:
        prepare_request(request)
        return timed_urlopen(request, timeout=None)

    client_module.build_opener = build_without_limits
    client_module.urlopen = urlopen_without_limits


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workflow", required=True, choices=("dynamic", "single-agent")
    )
    args = parser.parse_args()
    freeze = json.loads(FREEZE_PATH.read_text(encoding="utf-8"))
    task_id = freeze["ready_task_ids"][0]
    run_directory = PROJECT_ROOT / "runs/diagnostics" / (
        f"compare-{args.workflow}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-"
        f"{uuid4().hex[:8]}"
    )
    run_directory.mkdir(parents=True, exist_ok=False)
    log = EventLog(run_directory / "timing.jsonl")
    log.write(
        "comparison_started", workflow=args.workflow, task_id=task_id,
        server_default_output_tokens=8192, local_wait_deadline_seconds=None,
    )
    cli_args = _parser().parse_args([
        "experiment",
        "--manifest", str(PROJECT_ROOT / freeze["manifest"]),
        "--method", f"no-local-limits-{args.workflow}",
        "--provider", "api",
        "--runner", "docker",
        "--workflow", args.workflow,
        "--context", "ast-callgraph",
        "--model", freeze["protocol"]["model"],
        "--thinking", "disabled",
        "--task-id", task_id,
        "--max-submission-attempts", "1",
        "--max-model-calls", "8",
        "--formal",
        "--output-root", str(run_directory / "experiments"),
    ])
    omit_local_limits(log)
    client = UncappedTimedClient(_create_client(cli_args), log)
    try:
        result = run_experiment(
            manifest_path=cli_args.manifest,
            method=cli_args.method,
            locator_client=client,
            repair_client=client,
            output_root=cli_args.output_root,
            backend=cli_args.runner,
            workflow_mode=cli_args.workflow,
            context_mode=cli_args.context,
            task_ids=tuple(cli_args.task_id),
            max_model_calls=cli_args.max_model_calls,
            formal=cli_args.formal,
            max_submission_attempts=cli_args.max_submission_attempts,
        )
    except BaseException as error:
        log.write(
            "comparison_error", error_type=type(error).__name__,
            error_message=str(error)[:300],
        )
        raise
    log.write(
        "comparison_finished", formal_status=result["formal_status"],
        evaluation_path=result["evaluation_path"],
    )
    print(json.dumps({
        "workflow": args.workflow,
        "run_directory": str(run_directory),
        "evaluation_path": result["evaluation_path"],
        "formal_status": result["formal_status"],
        "submission_metrics": result["submission_metrics"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
