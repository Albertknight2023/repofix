"""Verify that a formal experiment still matches its registered local freeze."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from repofix.benchmarks.loader import load_tasks
from repofix.experiments import _experiment_fingerprints


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: verify_experiment_freeze.py FREEZE.json", file=sys.stderr)
        return 2
    freeze_path = Path(sys.argv[1]).resolve()
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    project_root = Path(__file__).resolve().parent.parent
    manifest = project_root / freeze["manifest"]
    tasks = load_tasks(manifest)
    implementation, dataset = _experiment_fingerprints(manifest, tasks)
    failures: list[str] = []
    if implementation != freeze["implementation_fingerprint"]:
        failures.append("Agent implementation changed")
    if dataset != freeze["dataset_fingerprint"]:
        failures.append("ready task dataset changed")
    if [task.id for task in tasks] != freeze["ready_task_ids"]:
        failures.append("ready task order or membership changed")
    for relative_name, expected_hash in freeze["extra_file_sha256"].items():
        actual_hash = hashlib.sha256((project_root / relative_name).read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            failures.append(f"file changed: {relative_name}")
    for image, expected_id in freeze["docker_image_ids"].items():
        result = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
            capture_output=True, text=True, check=False, timeout=20,
        )
        if result.returncode != 0 or result.stdout.strip() != expected_id:
            failures.append(f"Docker image changed or unavailable: {image}")
    if failures:
        for failure in failures:
            print(f"FREEZE MISMATCH: {failure}", file=sys.stderr)
        return 1
    print(f"freeze verified: {len(tasks)} ready tasks")
    print(f"implementation={implementation}")
    print(f"dataset={dataset}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
