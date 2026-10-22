"""Loopback-only dashboard and narrowly scoped local job API."""

import json
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from repofix.benchmarks.loader import load_manifest, load_task
from repofix.dashboard.jobs import JobManager


_SAFE_NAME = re.compile(r"[A-Za-z0-9_-]{1,90}\Z")
_SAFE_ASSET = re.compile(r"[A-Za-z0-9_-]+\.(?:js|css|svg|png|woff2)\Z")
_STATIC = Path(__file__).parent / "static"
_WEB_DIST = Path(__file__).parent / "web_dist"


class LoopbackHTTPServer(ThreadingHTTPServer):
    """Prevent a second dashboard from silently sharing the same Windows port."""

    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        allow_reuse_address = False

        def server_bind(self) -> None:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _run_directory(runs_root: Path, run_id: str) -> Path | None:
    if not _SAFE_NAME.fullmatch(run_id):
        return None
    direct = runs_root / run_id
    if (direct / "report.json").is_file():
        return direct
    for path in runs_root.glob(f"*/{run_id}"):
        if (
            path.parent.name not in {"experiments", "evaluations"}
            and _SAFE_NAME.fullmatch(path.parent.name)
            and (path / "report.json").is_file()
        ):
            return path
    for path in (runs_root / "experiments").glob("*/runs/*"):
        if path.name == run_id and (path / "report.json").is_file():
            return path
    return None


def _evaluation_path(runs_root: Path, evaluations_root: Path, evaluation_id: str) -> Path | None:
    if not _SAFE_NAME.fullmatch(evaluation_id):
        return None
    direct = evaluations_root / f"{evaluation_id}.json"
    if direct.is_file():
        return direct
    nested = runs_root / "experiments" / evaluation_id / "evaluation.json"
    return nested if nested.is_file() else None


def _overview(manifests: tuple[Path, ...], runs_root: Path, evaluations_root: Path) -> dict[str, object]:
    tasks: list[dict[str, object]] = []
    for manifest_path in manifests:
        if not manifest_path.is_file():
            continue
        manifest = load_manifest(manifest_path)
        for item in manifest.tasks:
            task_data = _read_json(item.config_path.with_name("provenance.json"))
            try:
                issue = load_task(item.config_path).issue
            except (FileNotFoundError, KeyError, TypeError, ValueError):
                issue = None
            tasks.append({
                "id": item.id, "suite": manifest.suite,
                "category": item.category, "status": item.status,
                "config": str(item.config_path),
                "issue": issue,
                "origin": task_data.get("dataset") if task_data else "development",
            })

    runs: list[dict[str, object]] = []
    if runs_root.is_dir():
        run_paths = (
            list(runs_root.iterdir())
            + list(runs_root.glob("*/*"))
            + list((runs_root / "experiments").glob("*/runs/*"))
        )
        for path in sorted(run_paths, key=lambda item: item.name, reverse=True):
            if not path.is_dir() or not _SAFE_NAME.fullmatch(path.name):
                continue
            if path.parent != runs_root and path.parent.name in {"experiments", "evaluations"}:
                continue
            report = _read_json(path / "report.json")
            if report is None:
                continue
            task = report.get("task") if isinstance(report.get("task"), dict) else {}
            validation = report.get("validation") if isinstance(report.get("validation"), dict) else {}
            runs.append({
                "run_id": path.name,
                "created_at_utc": report.get("created_at_utc"),
                "task_id": task.get("id"),
                "title": task.get("title"),
                "validation_status": validation.get("status", "not_run"),
                "has_patch": (path / "candidate.patch").is_file(),
                "grade_status": (_read_json(path / "grade.json") or {}).get("status"),
            })
            if len(runs) >= 100:
                break

    evaluations: list[dict[str, object]] = []
    eval_paths = list(evaluations_root.glob("*.json")) + list((runs_root / "experiments").glob("*/evaluation.json"))
    for path in sorted(eval_paths, key=lambda item: item.stat().st_mtime, reverse=True):
            evaluation_id = path.parent.name if path.name == "evaluation.json" else path.stem
            if not _SAFE_NAME.fullmatch(evaluation_id):
                continue
            result = _read_json(path)
            if result is None or result.get("schema_version") != 1:
                continue
            evaluations.append({
                "id": evaluation_id, "created_at_utc": result.get("created_at_utc"),
                "task_count": result.get("task_count"),
                "backend": result.get("backend"),
                "methods": result.get("methods"),
                "partial_run": result.get("partial_run", False),
                "selected_task_count": result.get("selected_task_count"),
                "experiment_protocol": result.get("experiment_protocol"),
                "formal_status": result.get("formal_status"),
                "is_reference_fixture": result.get("provider") == "mock" or any(
                    any(word in str(method.get("method", "")).lower() for word in ("reference", "fixture", "mock"))
                    for method in result.get("methods", []) if isinstance(method, dict)
                ),
            })
            if len(evaluations) >= 30:
                break
    return {"tasks": tasks, "runs": runs, "evaluations": evaluations}


def make_handler(
    manifests: tuple[Path, ...], runs_root: Path, evaluations_root: Path,
    job_manager: JobManager | None = None,
):
    jobs = job_manager or JobManager(Path(__file__).resolve().parents[3])

    def ready_tasks() -> dict[str, Path]:
        return {
            item.id: item.config_path
            for manifest_path in manifests if manifest_path.is_file()
            for item in load_manifest(manifest_path).tasks if item.status == "ready"
        }

    def run_options(data: dict[str, object]) -> tuple[str, str, int]:
        workflow = data.get("workflow", "two-agent")
        context = data.get("context", "ast-callgraph")
        max_tokens = data.get("max_tokens", 6000)
        if workflow not in {"two-agent", "single-agent", "competitive", "dynamic"}:
            raise ValueError("workflow 必须是 two-agent、single-agent、competitive 或 dynamic")
        if context not in {"ast-callgraph", "traceback-only"}:
            raise ValueError("context 必须是 ast-callgraph 或 traceback-only")
        if type(max_tokens) is not int or not 512 <= max_tokens <= 12000:
            raise ValueError("max_tokens 必须在 512 到 12000 之间")
        return workflow, context, max_tokens

    class DashboardHandler(BaseHTTPRequestHandler):
        def _send(self, status: int, data: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status: int, data: object) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:
            path = unquote(urlsplit(self.path).path)
            if path == "/" and (_WEB_DIST / "index.html").is_file():
                self._send(200, (_WEB_DIST / "index.html").read_bytes(), "text/html; charset=utf-8")
                return
            if path.startswith("/assets/") and (_WEB_DIST / "index.html").is_file():
                filename = path.removeprefix("/assets/")
                asset = _WEB_DIST / "assets" / filename
                if _SAFE_ASSET.fullmatch(filename) and asset.is_file():
                    mime = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png", ".woff2": "font/woff2"}[asset.suffix]
                    self._send(200, asset.read_bytes(), mime)
                else:
                    self._json(404, {"error": "asset not found"})
                return
            if path in {"/", "/dashboard.css", "/dashboard.js"}:
                filename = {"/": "index.html", "/dashboard.css": "dashboard.css", "/dashboard.js": "dashboard.js"}[path]
                mime = "text/html" if filename.endswith(".html") else "text/css" if filename.endswith(".css") else "text/javascript"
                self._send(200, (_STATIC / filename).read_bytes(), f"{mime}; charset=utf-8")
                return
            if path == "/api/overview":
                self._json(200, _overview(manifests, runs_root, evaluations_root))
                return
            if path == "/api/jobs":
                self._json(200, {"jobs": jobs.list_jobs()})
                return
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "jobs"] and _SAFE_NAME.fullmatch(parts[2]):
                job = jobs.get(parts[2])
                self._json(200, job) if job else self._json(404, {"error": "job not found"})
                return
            if len(parts) == 3 and parts[:2] == ["api", "runs"] and _SAFE_NAME.fullmatch(parts[2]):
                run_directory = _run_directory(runs_root, parts[2])
                report = _read_json(run_directory / "report.json") if run_directory else None
                self._json(200, report) if report is not None else self._json(404, {"error": "run not found"})
                return
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "report.md" and _SAFE_NAME.fullmatch(parts[2]):
                run_directory = _run_directory(runs_root, parts[2])
                document = run_directory / "report.md" if run_directory else None
                self._send(200, document.read_bytes(), "text/markdown; charset=utf-8") if document and document.is_file() else self._json(404, {"error": "markdown report not found"})
                return
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "patch" and _SAFE_NAME.fullmatch(parts[2]):
                run_directory = _run_directory(runs_root, parts[2])
                patch = run_directory / "candidate.patch" if run_directory else None
                self._send(200, patch.read_bytes(), "text/plain; charset=utf-8") if patch and patch.is_file() else self._json(404, {"error": "patch not found"})
                return
            if len(parts) == 5 and parts[:2] == ["api", "runs"] and parts[3] == "attempts" and _SAFE_NAME.fullmatch(parts[2]):
                run_directory = _run_directory(runs_root, parts[2])
                report = _read_json(run_directory / "report.json") if run_directory else None
                artifacts = report.get("artifacts") if isinstance(report, dict) else None
                filenames = artifacts.get("attempt_patches") if isinstance(artifacts, dict) else None
                allowed = isinstance(filenames, dict) and parts[4] in filenames.values()
                patch = run_directory / parts[4] if run_directory and allowed and re.fullmatch(r"candidate_\d{2}\.patch", parts[4]) else None
                self._send(200, patch.read_bytes(), "text/plain; charset=utf-8") if patch and patch.is_file() else self._json(404, {"error": "candidate patch not found"})
                return
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "grade" and _SAFE_NAME.fullmatch(parts[2]):
                run_directory = _run_directory(runs_root, parts[2])
                grade = _read_json(run_directory / "grade.json") if run_directory else None
                self._json(200, grade) if grade else self._json(404, {"error": "grade not found"})
                return
            if len(parts) == 3 and parts[:2] == ["api", "evaluations"] and _SAFE_NAME.fullmatch(parts[2]):
                evaluation_path = _evaluation_path(runs_root, evaluations_root, parts[2])
                result = _read_json(evaluation_path) if evaluation_path else None
                self._json(200, result) if result is not None else self._json(404, {"error": "evaluation not found"})
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            path = unquote(urlsplit(self.path).path)
            if path not in {"/api/jobs/repair", "/api/jobs/grade", "/api/jobs/experiment"}:
                self._json(405, {"error": "this endpoint is read-only"})
                return
            host = self.headers.get("Host", "").split(":", 1)[0]
            origin = self.headers.get("Origin")
            allowed_origins = {
                f"http://{name}:{port}"
                for name in ("127.0.0.1", "localhost")
                for port in (self.server.server_port, 5173)
            }
            if (
                host not in {"127.0.0.1", "localhost"}
                or (origin is not None and origin not in allowed_origins)
                or self.headers.get("X-RepoFix-Action") != "1"
            ):
                self._json(403, {"error": "local action header and origin required"})
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                self._json(415, {"error": "JSON content required"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
                if not 0 < length <= 4096:
                    raise ValueError("请求体大小无效")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("请求体必须是 JSON 对象")
                tasks = ready_tasks()
                if path == "/api/jobs/repair":
                    if set(data) - {"task_id", "workflow", "context", "max_tokens"}:
                        raise ValueError("包含不支持的参数")
                    task_id = data.get("task_id")
                    if not isinstance(task_id, str) or task_id not in tasks:
                        raise ValueError("只能修复已注册的 ready 任务")
                    workflow, context, max_tokens = run_options(data)
                    args = [
                        "repair", "--task", str(tasks[task_id]), "--provider", "api",
                        "--runner", "docker", "--output-root", str(runs_root),
                        "--workflow", workflow, "--context", context,
                        "--max-tokens", str(max_tokens),
                    ]
                    job = jobs.submit("repair", args, {"task_id": task_id, "workflow": workflow, "context": context}, 900)
                elif path == "/api/jobs/grade":
                    if set(data) != {"task_id", "run_id"}:
                        raise ValueError("验收需要 task_id 和 run_id")
                    task_id, run_id = data["task_id"], data["run_id"]
                    if not isinstance(task_id, str) or task_id not in tasks or not isinstance(run_id, str):
                        raise ValueError("任务或运行编号无效")
                    directory = _run_directory(runs_root, run_id)
                    report = _read_json(directory / "report.json") if directory else None
                    report_task = report.get("task") if report else None
                    patch = directory / "candidate.patch" if directory else None
                    if not isinstance(report_task, dict) or report_task.get("id") != task_id or not patch or not patch.is_file():
                        raise ValueError("该任务没有可验收的补丁")
                    args = ["grade", "--task", str(tasks[task_id]), "--patch", str(patch), "--runner", "docker"]
                    job = jobs.submit("grade", args, {"task_id": task_id, "run_id": run_id}, 900)
                else:
                    if set(data) - {"suite", "method", "workflow", "context", "limit", "max_tokens", "formal", "max_submission_attempts", "max_model_calls"}:
                        raise ValueError("包含不支持的参数")
                    suite, method = data.get("suite"), data.get("method")
                    selected = next((path for path in manifests if path.is_file() and load_manifest(path).suite == suite), None)
                    if selected is None or not isinstance(method, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", method):
                        raise ValueError("任务集或方法标签无效")
                    ready_count = sum(item.status == "ready" for item in load_manifest(selected).tasks)
                    limit = data.get("limit")
                    if limit is not None and (type(limit) is not int or not 1 <= limit <= ready_count):
                        raise ValueError("limit 必须在 1 到 ready 任务数之间")
                    workflow, context, max_tokens = run_options(data)
                    formal = data.get("formal", False)
                    attempts = data.get("max_submission_attempts", 3)
                    max_calls = data.get("max_model_calls")
                    if type(formal) is not bool:
                        raise ValueError("formal 必须是布尔值")
                    if type(attempts) is not int or not 1 <= attempts <= 10:
                        raise ValueError("每题重试次数必须在 1 到 10 之间")
                    if max_calls is not None and (type(max_calls) is not int or not 1 <= max_calls <= 200):
                        raise ValueError("总模型调用上限必须在 1 到 200 之间")
                    if formal and max_calls is None:
                        raise ValueError("正式实验必须设置总模型调用上限")
                    args = [
                        "experiment", "--manifest", str(selected), "--method", method,
                        "--provider", "api", "--runner", "docker",
                        "--workflow", workflow, "--context", context,
                        "--max-tokens", str(max_tokens),
                        "--output-root", str(runs_root / "experiments"),
                    ]
                    if limit is not None:
                        args.extend(["--limit", str(limit)])
                    if formal:
                        args.extend(["--formal", "--max-submission-attempts", str(attempts)])
                    if max_calls is not None:
                        args.extend(["--max-model-calls", str(max_calls)])
                    job = jobs.submit("experiment", args, {"suite": suite, "method": method, "workflow": workflow, "context": context, "limit": limit, "formal": formal, "max_model_calls": max_calls}, 900 * (limit or ready_count))
                self._json(202, job)
            except (OSError, ValueError) as error:
                self._json(400, {"error": str(error)[:300]})

    return DashboardHandler


def serve_dashboard(
    *, manifests: tuple[Path, ...], runs_root: Path, evaluations_root: Path,
    port: int = 8765,
) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    server = LoopbackHTTPServer(
        ("127.0.0.1", port),
        make_handler(tuple(path.resolve() for path in manifests), runs_root.resolve(), evaluations_root.resolve()),
    )
    print(f"RepoFix dashboard: http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
