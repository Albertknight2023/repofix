"""Tests for isolated benchmark workspaces."""

from pathlib import Path

from repofix.benchmarks.loader import load_tasks
from repofix.workspace.manager import isolated_workspace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "benchmarks" / "manifest.yaml"


def test_workspace_contains_repository_copy() -> None:
    task = load_tasks(MANIFEST_PATH)[0]

    with isolated_workspace(task) as workspace:
        copied_source = workspace.repo_path / "src" / "pagination.py"

        assert workspace.root.is_dir()
        assert workspace.repo_path.is_dir()
        assert copied_source.is_file()

        assert workspace.repo_path != task.repo_path


def test_modifying_workspace_does_not_modify_original() -> None:
    task = load_tasks(MANIFEST_PATH)[0]

    original_source = task.repo_path / "src" / "pagination.py"
    original_content = original_source.read_text(encoding="utf-8")

    with isolated_workspace(task) as workspace:
        copied_source = workspace.repo_path / "src" / "pagination.py"

        copied_source.write_text(
            "# changed inside temporary workspace\n",
            encoding="utf-8",
        )

        assert copied_source.read_text(encoding="utf-8") != original_content
        assert original_source.read_text(encoding="utf-8") == original_content


def test_workspace_does_not_contain_hidden_materials() -> None:
    task = load_tasks(MANIFEST_PATH)[0]

    with isolated_workspace(task) as workspace:
        assert not (workspace.root / "grader_tests").exists()
        assert not (workspace.root / "reference").exists()
        assert not (workspace.root / "solution.patch").exists()


def test_workspace_is_removed_after_use() -> None:
    task = load_tasks(MANIFEST_PATH)[0]

    with isolated_workspace(task) as workspace:
        workspace_root = workspace.root
        assert workspace_root.exists()

    assert not workspace_root.exists()