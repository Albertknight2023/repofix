from pathlib import Path

from repofix.benchmarks.loader import load_manifest, load_task, load_tasks
PROJECT_ROOT = Path(__file__).resolve().parents[1]

MANIFEST_PATH= PROJECT_ROOT / "benchmarks" / "manifest.yaml"

def test_loads_manifest()->None:
    manifest= load_manifest(MANIFEST_PATH)
    #测试读取到的benchmark\manifest.yaml文件里面的version值是否为1，否则AssertionErrOr
    assert manifest.version == 1
    assert manifest.suite == "repofix-development"
    assert manifest.language == "python"
    assert manifest.test_framework == "pytest"
    assert len(manifest.tasks) == 5
    assert manifest.tasks[0].id == "pagination-zero-page-size"
    assert all(task.config_path.is_file() for task in manifest.tasks)

def test_loads_pagination_task()->None:
    manifest= load_manifest(MANIFEST_PATH)
    task= load_task(manifest.tasks[0].config_path) #

    assert task.id == "pagination-zero-page-size"
    assert task.timeout_seconds == 120
    assert task.repo_path.is_dir()
    assert task.grader_test_path.is_dir()
    assert task.allowed_edit_paths == ("src/",)
    assert "tests/" in task.protected_paths

def test_loads_all_ready_tasks() -> None:
    tasks= load_tasks(MANIFEST_PATH)

    assert len(tasks) == 5
    assert tasks[0].id == "pagination-zero-page-size"
    assert {task.id for task in tasks} == {
        "pagination-zero-page-size",
        "display-name-none",
        "shipping-member-rule",
        "setting-key-casefold",
        "asset-path-traversal",
    }
