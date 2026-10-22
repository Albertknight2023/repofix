"""the RepoFix benchmark system 使用的数据模型"""
from dataclasses import dataclass
from pathlib import Path

#对象里面的值不能修改，遵循yaml里面的定义，且不能增加新属性
@dataclass(frozen=True,slots=True)
class ManifestTask:
    id:str
    config_path:Path
    category:str
    status:str

#load_manifest
@dataclass(frozen=True,slots=True) 
class BenchmarkManifest:
    version:int
    suite:str
    language: str
    test_framework: str
    tasks: tuple[ManifestTask, ...]

#load_task
@dataclass(frozen=True,slots=True)
class TaskSpec:
    id: str 
    title: str
    language: str
    test_framework: str
    issue: str

    task_root:Path
    repo_path: Path
    grader_test_path: Path

    public_test_command: str
    full_test_command: str
    timeout_seconds: int

    allowed_edit_paths:tuple[str,...]
    protected_paths:tuple[str,...]

    expected_initial_status: str
    expected_reference_status: str
    container_image: str | None = None
