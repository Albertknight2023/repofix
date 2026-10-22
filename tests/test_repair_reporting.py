"""Durable reports must survive cleanup of the isolated worktree."""

import json
from pathlib import Path

from repofix.agents.workflow import run_repair_task
from repofix.benchmarks.loader import load_task
from repofix.cli import main
from repofix.llm import ScriptedModelClient
from repofix.reporting import save_repair_run


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK_PATH = (
    PROJECT_ROOT
    / "benchmarks"
    / "tasks"
    / "pagination-zero-page-size"
    / "task.yaml"
)
LOCATOR_RESPONSE = PROJECT_ROOT / "examples" / "pagination-locator-response.json"
REPAIR_RESPONSE = PROJECT_ROOT / "examples" / "pagination-repair-response.json"


def test_cli_saves_patch_and_evidence_report(
    tmp_path: Path,
    capsys,
) -> None:
    task = load_task(TASK_PATH)
    original_path = task.repo_path / "src" / "pagination.py"
    original_bytes = original_path.read_bytes()

    status = main(
        [
            "repair",
            "--task", str(TASK_PATH),
            "--provider", "mock",
            "--runner", "host",
            "--mock-response", str(LOCATOR_RESPONSE),
            "--mock-repair-response", str(REPAIR_RESPONSE),
            "--output-root", str(tmp_path),
        ]
    )

    assert status == 0
    output = json.loads(capsys.readouterr().out)
    run_directory = Path(output["run_directory"])
    report_path = Path(output["report_path"])
    patch_path = Path(output["patch_path"])
    assert run_directory.parent == tmp_path.resolve() / task.id
    assert report_path == run_directory / "report.json"
    assert patch_path == run_directory / "candidate.patch"
    assert report_path.is_file() and patch_path.is_file()
    assert patch_path.read_text(encoding="utf-8") == output["patch"]
    assert "+    if page_size <= 0:" in output["patch"]
    assert original_path.read_bytes() == original_bytes

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert report["run_id"] == run_directory.name
    assert report["task"]["id"] == task.id
    assert report["initial_public_test"]["status"] == "failed"
    assert report["validation"]["status"] == "public_tests_passed"
    assert report["validation"]["hidden_tests_run"] is False
    assert report["validation"]["patch_review"]["status"] == "passed"
    assert report["artifacts"]["candidate_patch"] == "candidate.patch"
    assert report["context_selection"]["target"] == {
        "file_path": "src/pagination.py",
        "symbol_name": "paginate",
    }
    assert report["context_selection"]["related"]
    evidence_ids = {item["id"] for item in report["evidence"]}
    assert {"F1", "L1", "S1", "T1"}.issubset(evidence_ids)
    assert set(report["diagnosis"]["hypothesis"]["evidence_ids"]) <= evidence_ids
    assert set(report["repair"]["candidate"]["evidence_ids"]) <= evidence_ids
    assert "grader_tests" not in report_path.read_text(encoding="utf-8")


def test_repeated_cli_repairs_share_task_folder(tmp_path: Path, capsys) -> None:
    run_directories = []
    for _ in range(2):
        assert main([
            "repair",
            "--task", str(TASK_PATH),
            "--provider", "mock",
            "--runner", "host",
            "--mock-response", str(LOCATOR_RESPONSE),
            "--mock-repair-response", str(REPAIR_RESPONSE),
            "--output-root", str(tmp_path),
        ]) == 0
        run_directories.append(Path(json.loads(capsys.readouterr().out)["run_directory"]))

    assert run_directories[0] != run_directories[1]
    assert all(directory.parent == tmp_path.resolve() / "pagination-zero-page-size" for directory in run_directories)
    assert all((directory / "report.json").is_file() for directory in run_directories)


def test_no_patch_still_saves_report_without_claiming_patch(tmp_path: Path) -> None:
    task = load_task(TASK_PATH)
    locator_client = ScriptedModelClient(
        '{"status":"insufficient_evidence","explanation":"Need more evidence."}'
    )
    repair_client = ScriptedModelClient("unused")
    run = run_repair_task(task, locator_client, repair_client)

    saved = save_repair_run(task=task, run=run, output_root=tmp_path)

    assert saved.report_path.is_file()
    assert saved.patch_path is None
    assert not (saved.run_directory / "candidate.patch").exists()
    report = json.loads(saved.report_path.read_text(encoding="utf-8"))
    assert report["diagnosis"]["status"] == "insufficient_evidence"
    assert report["repair"]["status"] == "no_patch"
    assert report["validation"] is None
    assert report["artifacts"]["candidate_patch"] is None
