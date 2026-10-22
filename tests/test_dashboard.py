"""The dashboard may expose reports, never benchmark-hidden files."""

import json
import re
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from repofix.dashboard.server import LoopbackHTTPServer, _overview, make_handler


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_cannot_share_an_existing_listening_port(tmp_path: Path) -> None:
    handler = make_handler((), tmp_path / "runs", tmp_path / "evals")
    server = LoopbackHTTPServer(("127.0.0.1", 0), handler)
    try:
        with pytest.raises(OSError):
            LoopbackHTTPServer(("127.0.0.1", server.server_port), handler)
    finally:
        server.server_close()


def test_overview_uses_current_task_description(tmp_path: Path) -> None:
    overview = _overview(
        (ROOT / "benchmarks/real/manifest.yaml",), tmp_path / "runs", tmp_path / "evals"
    )
    task = next(item for item in overview["tasks"] if item["id"] == "bugsinpy-tqdm-1")
    assert task["issue"].startswith("tenumerate 是带进度条的 enumerate")


def test_dashboard_reads_grouped_and_legacy_runs(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    legacy = runs_root / "20261009T094618Z-bd645464"
    grouped = runs_root / "pagination-zero-page-size" / "20261010T071531Z-6cc43f94"
    for directory in (legacy, grouped):
        directory.mkdir(parents=True)
        (directory / "report.json").write_text(
            json.dumps({
                "run_id": directory.name,
                "created_at_utc": "2026-10-10T07:15:31+00:00",
                "task": {"id": "pagination-zero-page-size", "title": "Pagination"},
                "validation": {"status": "public_tests_passed"},
            }),
            encoding="utf-8",
        )
        (directory / "candidate.patch").write_text("example patch", encoding="utf-8")

    overview = _overview((), runs_root, tmp_path / "evals")
    assert {run["run_id"] for run in overview["runs"]} == {legacy.name, grouped.name}
    assert all(run["has_patch"] for run in overview["runs"])

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler((), runs_root, tmp_path / "evals")
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for directory in (legacy, grouped):
            with urlopen(f"http://127.0.0.1:{server.server_port}/api/runs/{directory.name}") as response:
                assert json.load(response)["run_id"] == directory.name
            with urlopen(f"http://127.0.0.1:{server.server_port}/api/runs/{directory.name}/patch") as response:
                assert response.read() == b"example patch"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_only_serves_registered_candidate_artifacts(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    directory = runs_root / "pagination-zero-page-size" / "20261013T080000Z-aabbccdd"
    directory.mkdir(parents=True)
    (directory / "candidate_01.patch").write_text("candidate one", encoding="utf-8")
    (directory / "candidate_02.patch").write_text("unregistered", encoding="utf-8")
    (directory / "report.md").write_text("# evidence", encoding="utf-8")
    (directory / "report.json").write_text(json.dumps({
        "task": {"id": "pagination-zero-page-size"},
        "artifacts": {"attempt_patches": {"1": "candidate_01.patch"}},
    }), encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler((), runs_root, tmp_path / "evals"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}/api/runs/{directory.name}"
    try:
        with urlopen(f"{base}/report.md") as response:
            assert response.read() == b"# evidence"
        with urlopen(f"{base}/attempts/candidate_01.patch") as response:
            assert response.read() == b"candidate one"
        with pytest.raises(HTTPError, match="HTTP Error 404"):
            urlopen(f"{base}/attempts/candidate_02.patch")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_is_read_only_and_does_not_serve_hidden_files(tmp_path: Path) -> None:
    handler = make_handler(
        (ROOT / "benchmarks/manifest.yaml",), tmp_path / "runs", tmp_path / "evals"
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base}/api/overview") as response:
            data = json.load(response)
        assert len(data["tasks"]) == 5
        assert data["tasks"][0]["issue"].startswith("paginate")
        with urlopen(f"{base}/") as response:
            page = response.read()
            assert b"RepoFix" in page
        if (ROOT / "src/repofix/dashboard/web_dist/index.html").is_file():
            asset = re.search(rb'src="(/assets/[^\"]+\.js)"', page)
            assert asset is not None
            with urlopen(f"{base}{asset.group(1).decode()}") as response:
                assert response.headers.get_content_type() == "text/javascript"
                assert response.read()
        try:
            urlopen(f"{base}/assets/%2e%2e%2findex.html")
            assert False, "asset traversal should not be served"
        except HTTPError as error:
            assert error.code == 404
        try:
            urlopen(f"{base}/benchmarks/tasks/pagination-zero-page-size/grader_tests/test_pagination_hidden.py")
            assert False, "hidden path should not be served"
        except HTTPError as error:
            assert error.code == 404
        try:
            urlopen(Request(f"{base}/api/overview", data=b"{}", method="POST"))
            assert False, "dashboard should reject writes"
        except HTTPError as error:
            assert error.code == 405
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_job_api_only_accepts_ready_tasks_and_known_options(tmp_path: Path) -> None:
    class FakeJobs:
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[str]]] = []

        def submit(self, kind, arguments, details, timeout):
            self.calls.append((kind, arguments))
            return {"id": "job123", "kind": kind, "state": "queued", "details": details}

        def list_jobs(self):
            return []

        def get(self, job_id):
            return None

    jobs = FakeJobs()
    handler = make_handler(
        (ROOT / "benchmarks/real/manifest.yaml",), tmp_path / "runs", tmp_path / "evals",
        job_manager=jobs,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def post(path: str, payload: dict, *, action_header: bool = True, origin: str | None = None):
        headers = {"Content-Type": "application/json"}
        if action_header:
            headers["X-RepoFix-Action"] = "1"
        if origin:
            headers["Origin"] = origin
        request = Request(base + path, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urlopen(request) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    try:
        status, _ = post("/api/jobs/repair", {"task_id": "bugsinpy-tqdm-1"}, action_header=False)
        assert status == 403
        status, _ = post("/api/jobs/repair", {"task_id": "bugsinpy-tqdm-1"}, origin="https://attacker.example")
        assert status == 403
        status, _ = post("/api/jobs/repair", {"task_id": "bugsinpy-tqdm-3"})
        assert status == 400  # candidate, not ready
        status, _ = post("/api/jobs/repair", {"task_id": "bugsinpy-tqdm-1", "command": "echo unsafe"})
        assert status == 400
        status, response = post("/api/jobs/repair", {"task_id": "bugsinpy-tqdm-1", "workflow": "single-agent"})
        assert status == 202 and response["state"] == "queued"
        status, response = post("/api/jobs/repair", {"task_id": "bugsinpy-tqdm-1", "workflow": "dynamic"})
        assert status == 202 and response["state"] == "queued"
        assert jobs.calls[-1][1][jobs.calls[-1][1].index("--workflow") + 1] == "dynamic"
        assert jobs.calls[-1][0] == "repair"
        assert "--runner" in jobs.calls[-1][1] and "docker" in jobs.calls[-1][1]

        status, _ = post("/api/jobs/experiment", {"suite": "bugsinpy-curated-candidates", "method": "trial", "limit": 99})
        assert status == 400
        status, _ = post("/api/jobs/experiment", {"suite": "bugsinpy-curated-candidates", "method": "trial", "limit": 1})
        assert status == 202
        assert jobs.calls[-1][0] == "experiment"
        assert jobs.calls[-1][1][-2:] == ["--limit", "1"]
        status, _ = post("/api/jobs/experiment", {
            "suite": "bugsinpy-curated-candidates", "method": "formal-trial",
            "limit": 1, "formal": True, "max_submission_attempts": 2,
        })
        assert status == 400  # formal API runs need a hard call budget
        status, _ = post("/api/jobs/experiment", {
            "suite": "bugsinpy-curated-candidates", "method": "formal-trial",
            "limit": 1, "formal": True, "max_submission_attempts": 2,
            "max_model_calls": 5,
        })
        assert status == 202
        assert "--formal" in jobs.calls[-1][1]
        assert jobs.calls[-1][1][jobs.calls[-1][1].index("--max-model-calls") + 1] == "5"
        assert jobs.calls[-1][1][jobs.calls[-1][1].index("--max-submission-attempts") + 1] == "2"

        status, _ = post("/api/jobs/grade", {"task_id": "bugsinpy-tqdm-1", "run_id": "../../outside"})
        assert status == 400
        directory = tmp_path / "runs" / "bugsinpy-tqdm-1" / "20261013T060959Z-b6bd95c7"
        directory.mkdir(parents=True)
        (directory / "report.json").write_text(json.dumps({"task": {"id": "bugsinpy-tqdm-1"}}), encoding="utf-8")
        (directory / "candidate.patch").write_text("patch", encoding="utf-8")
        status, _ = post("/api/jobs/grade", {"task_id": "bugsinpy-tqdm-1", "run_id": directory.name})
        assert status == 202
        assert jobs.calls[-1][0] == "grade"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
