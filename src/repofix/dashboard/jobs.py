"""Bounded local job queue for dashboard-triggered CLI operations."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4


class JobManager:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self._jobs: dict[str, dict[str, object]] = {}
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="repofix-job")

    def list_jobs(self) -> list[dict[str, object]]:
        with self._lock:
            return [dict(job) for job in reversed(list(self._jobs.values()))]

    def get(self, job_id: str) -> dict[str, object] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def submit(self, kind: str, arguments: list[str], details: dict[str, object], timeout: int) -> dict[str, object]:
        with self._lock:
            active = sum(job["state"] in {"queued", "running"} for job in self._jobs.values())
            if active >= 3:
                raise ValueError("已有 3 个待执行任务，请等待完成后再提交")
            job_id = uuid4().hex[:12]
            job: dict[str, object] = {
                "id": job_id, "kind": kind, "state": "queued",
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "details": details, "result": None, "error": None,
            }
            self._jobs[job_id] = job
            if len(self._jobs) > 50:
                for old_id, old_job in list(self._jobs.items()):
                    if len(self._jobs) <= 50:
                        break
                    if old_job["state"] not in {"queued", "running"}:
                        del self._jobs[old_id]
        self._executor.submit(self._execute, job_id, arguments, timeout)
        return dict(job)

    def _execute(self, job_id: str, arguments: list[str], timeout: int) -> None:
        with self._lock:
            self._jobs[job_id]["state"] = "running"
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "repofix.cli", *arguments],
                cwd=self.project_root, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
                shell=False, check=False,
            )
            if completed.returncode not in {0, 2} or (completed.returncode == 2 and arguments[0] == "experiment"):
                raise RuntimeError((completed.stderr or completed.stdout or "运行失败").strip()[:500])
            result = json.loads(completed.stdout)
            if not isinstance(result, dict):
                raise ValueError("运行结果不是 JSON 对象")
            if arguments[0] == "repair":
                result = {key: result.get(key) for key in (
                    "task_id", "validation_status", "public_tests_passed",
                    "run_directory", "report_path", "patch_path", "patch_review",
                    "candidate_count", "selected_attempt", "selection_reason", "model_calls",
                )}
            elif arguments[0] == "experiment":
                result = {key: result.get(key) for key in (
                    "directory", "evaluation_path", "summary", "task_errors",
                )}
            elif arguments[0] == "grade":
                patch = Path(arguments[arguments.index("--patch") + 1])
                (patch.parent / "grade.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
            with self._lock:
                self._jobs[job_id].update(state="succeeded", result=result)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            with self._lock:
                self._jobs[job_id].update(state="failed", error=str(error)[:500])
