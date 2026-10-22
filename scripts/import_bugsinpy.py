"""Import one BugsInPy bug as a RepoFix candidate task (never auto-qualify it)."""

import argparse
import difflib
import io
import json
import re
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml


SHA = re.compile(r"[0-9a-f]{40}\Z")


def _metadata(path: Path) -> dict[str, str]:
    result = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _git(checkout: Path, *args: str) -> bytes:
    process = subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        check=False,
        timeout=120,
    )
    if process.returncode:
        raise RuntimeError(process.stderr.decode("utf-8", errors="replace"))
    return process.stdout


def _extract_archive(archive: bytes, destination: Path) -> None:
    root = destination.resolve()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as contents:
        for member in contents:
            target = (root / member.name).resolve()
            if not target.is_relative_to(root):
                raise ValueError("upstream archive contains a path outside its destination")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = contents.extractfile(member)
                if source is None:
                    raise ValueError("upstream archive has an unreadable file")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
            else:
                raise ValueError("upstream archive contains unsupported link or device")


def import_bug(
    *, bugsinpy_root: Path, project_checkout: Path, project: str,
    bug_id: int, issue: str, output_root: Path,
) -> Path:
    info_root = bugsinpy_root.resolve() / "projects" / project
    bug_root = info_root / "bugs" / str(bug_id)
    info = _metadata(bug_root / "bug.info")
    project_info = _metadata(info_root / "project.info")
    buggy_sha = info["buggy_commit_id"]
    fixed_sha = info["fixed_commit_id"]
    if not SHA.fullmatch(buggy_sha) or not SHA.fullmatch(fixed_sha):
        raise ValueError("bug has invalid commit IDs")
    upstream_url = project_info["github_url"]
    if not upstream_url.startswith("https://github.com/"):
        raise ValueError("upstream URL must be on GitHub")
    if not project_checkout.is_dir():
        raise FileNotFoundError("project Git checkout is missing")
    actual_url = _git(project_checkout, "remote", "get-url", "origin").decode().strip()
    if actual_url.removesuffix(".git") != upstream_url.removesuffix(".git"):
        raise ValueError("project checkout origin does not match BugsInPy metadata")

    target_paths = re.findall(r"^\+\+\+ b/(.+)$", (bug_root / "bug_patch.txt").read_text(encoding="utf-8"), re.M)
    if len(target_paths) != 1 or not target_paths[0].endswith(".py"):
        raise ValueError("only one-file Python patches can be imported at this stage")
    target_file = target_paths[0]
    if Path(target_file).is_absolute() or ".." in Path(target_file).parts or "tests" in Path(target_file).parts:
        raise ValueError("patch target must be one production Python file")

    trigger = (bug_root / "run_test.sh").read_text(encoding="utf-8").strip()
    tokens = shlex.split(trigger)
    if tokens and tokens[0] == "pytest":
        tokens = ["python", "-m", "pytest", *tokens[1:]]
    if len(tokens) < 4 or tokens[0] not in {"python", "python3"} or tokens[1:3] != ["-m", "pytest"]:
        raise ValueError("the trigger must be a python -m pytest command")
    public_command = " ".join(shlex.quote(token) for token in ("python", *tokens[1:]))
    full_command = " ".join(shlex.quote(token) for token in ("python", *tokens[1:], "../grader_tests", "-q"))

    output_root = output_root.resolve()
    task_id = f"bugsinpy-{project}-{bug_id}"
    target = output_root / task_id
    if not target.is_relative_to(output_root) or target.exists():
        raise ValueError("output task path is invalid or already exists")
    output_root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=f"{task_id}-", dir=output_root) as temporary:
        staging = Path(temporary)
        repo = staging / "repo"
        repo.mkdir()
        _extract_archive(_git(project_checkout, "archive", "--format=tar", buggy_sha), repo)
        if not (repo / target_file).is_file():
            raise ValueError("upstream buggy commit does not contain patch target")

        trigger_file = info.get("test_file", "")
        backported_test_change = False
        if trigger_file:
            trigger_path = Path(trigger_file)
            if trigger_path.is_absolute() or ".." in trigger_path.parts or not (repo / trigger_path).is_file():
                raise ValueError("BugsInPy trigger test file is invalid")
            test_diff = _git(project_checkout, "diff", buggy_sha, fixed_sha, "--", trigger_file)
            if test_diff:
                applied = subprocess.run(
                    ["git", "apply", "-"], input=test_diff, cwd=repo,
                    capture_output=True, check=False, timeout=30,
                )
                if applied.returncode:
                    raise ValueError("upstream regression-test change does not apply to buggy checkout")
                backported_test_change = True

        old_text = _git(project_checkout, "show", f"{buggy_sha}:{target_file}").decode("utf-8")
        new_text = _git(project_checkout, "show", f"{fixed_sha}:{target_file}").decode("utf-8")
        patch = "".join(difflib.unified_diff(
            old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
            fromfile=f"a/{target_file}", tofile=f"b/{target_file}",
        ))
        if not patch:
            raise ValueError("upstream fixed commit has no change in the target file")
        reference = staging / "reference" / target_file
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_text(new_text, encoding="utf-8", newline="\n")
        (staging / "solution.patch").write_text(patch, encoding="utf-8", newline="\n")
        (staging / "grader_tests").mkdir()
        task_data = {
            "id": task_id,
            "title": f"BugsInPy {project} #{bug_id}",
            "language": "python",
            "test_framework": "pytest",
            "issue": issue,
            "repo_path": "repo",
            "grader_test_path": "grader_tests",
            "public_test_command": public_command,
            "full_test_command": full_command,
            "timeout_seconds": 120,
            "allowed_edit_paths": [target_file],
            "protected_paths": ["../grader_tests", "../reference", "../solution.patch"],
            "expected_initial_status": "FAILED",
            "expected_reference_status": "PASSED",
            "container_image": "repofix-pytest:py38" if info.get("python_version", "").startswith("3.8") or project == "tqdm" else None,
        }
        (staging / "task.yaml").write_text(yaml.safe_dump(task_data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        provenance = {
            "dataset": "BugsInPy", "project": project, "bug_id": bug_id,
            "upstream_url": upstream_url, "buggy_commit": buggy_sha,
            "fixed_commit": fixed_sha, "declared_python_version": info.get("python_version"),
            "upstream_trigger": trigger, "benchmark_status": "candidate_not_yet_reproduced",
            "upstream_trigger_test_backported": backported_test_change,
            "reference_patch_visible_to_agent": False,
        }
        (staging / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # This is a generated checkout, not a hand-edited source file.
        shutil.move(str(staging), str(target))
    return target


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bugsinpy-root", type=Path, required=True)
    parser.add_argument("--project-checkout", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--bug-id", type=int, required=True)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    print(import_bug(
        bugsinpy_root=args.bugsinpy_root,
        project_checkout=args.project_checkout,
        project=args.project,
        bug_id=args.bug_id,
        issue=args.issue,
        output_root=args.output_root,
    ))


if __name__ == "__main__":
    main()
