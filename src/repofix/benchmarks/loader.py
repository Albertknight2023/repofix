
r"""
├── benchmarks\                            ← benchmark_root
│   ├── manifest.yaml                      ← manifest_path
│   │
│   └── tasks\
│       └── pagination-zero-page-size\      ← task_root
│           ├── task.yaml                   ← config_path / task_path
│           ├── repo\                       ← repo_path 需要修复的项目
│           │   ├── src\
│           │   │   └── pagination.py
│           │   └── tests\                  ← 公开测试
│           │       └── test_pagination.py
│           │
│           ├── grader_tests\               ← grader_test_path 隐藏测试
│           ├── reference\
│           └── solution.patch
│
├── src\repofix\                            ← RepoFix 平台自己的代码
└── tests\                                  ← RepoFix 平台自己的测试
"""
from pathlib import Path
from typing import Any
import yaml
from repofix.benchmarks.models import(
    ManifestTask,
    BenchmarkManifest,
    TaskSpec,
)

#读取yaml文件的路径，返回的是整个yaml字典，键为字符串，值任意
def _read_yaml(path:Path) -> dict[str,Any]:
    #如果找到的不是文件而是文件夹之类的,终止运行，报错
    if not path.is_file():
        raise FileNotFoundError(f"yaml文件不存在：{path}")
    text = path.read_text(encoding="utf-8")

    #把 YAML 文本转换成 Python 对象
    data = yaml.safe_load(text)

    #变量data是不是字典
    if not isinstance(data,dict):
        raise ValueError(f"yaml 必须是字典{path}")

    return data

def _resolve_child_path(base: Path, relative_path:str, field: str) ->Path:
    #基础路径规范成绝对路径
    base = base.resolve()
    #完整路径规范成绝对路径
    resolved = (base/relative_path).resolve()

    #检查resolved是不是在base内部
    if not resolved.is_relative_to(base):
        raise ValueError(
            f"{field} 在允许的目录之外{relative_path}"
        )
    
    return resolved

#如何读取manifest,在benchmarks/manifest.yaml 加载任务清单
def load_manifest(manifest_path: str|Path) ->BenchmarkManifest:
    manifest_path=Path(manifest_path).resolve()
    data = _read_yaml(manifest_path)
    #benchmark文件夹的路径为manifest文件的父目录
    #_benchmark
    # |_manifest.yaml
    benchmark_root = manifest_path.parent

    tasks: list[ManifestTask] = []

    for task_data in data["tasks"]:
        #config_path的绝对路径
        config_path = _resolve_child_path(
            base= benchmark_root,
            relative_path= task_data["config"],
            field= "config"
        )
        tasks.append(
            ManifestTask(
                id= task_data["id"],
                config_path= config_path,
                category= task_data["category"],
                status= task_data["status"],
            )
        )
    return BenchmarkManifest(
        version= data["version"],
        suite= data["suite"],
        language= data["language"],
        test_framework= data["test_framework"],
        tasks= tuple(tasks),
    )

#加载单个任务
def load_task(config_path: str|Path)-> TaskSpec:
    config_path= Path(config_path).resolve()
    data= _read_yaml(config_path)
    task_root= config_path.parent

    repo_path= _resolve_child_path(
        base= task_root,
        relative_path= data["repo_path"],
        field="repo_path"
        )

    grader_test_path= _resolve_child_path(
        base= task_root,
        relative_path= data["grader_test_path"],
        field= "grader_test_path"
    )
    if not repo_path.is_dir():
        raise FileNotFoundError(f"需要修复的任务仓库 不存在: {repo_path}")

    if not grader_test_path.is_dir():
        raise FileNotFoundError(
            f"隐藏测试目录不存在: {grader_test_path}"
        )
    return TaskSpec(
        id= data["id"],
        title= data["title"],
        language= data["language"],
        test_framework= data["test_framework"],
        issue= data["issue"],
        task_root= task_root,
        repo_path= repo_path,
        grader_test_path= grader_test_path,
        public_test_command= data["public_test_command"],
        full_test_command= data["full_test_command"],
        timeout_seconds= data["timeout_seconds"],
        allowed_edit_paths= tuple(data["allowed_edit_paths"]),
        protected_paths= tuple(data["protected_paths"]),
        expected_initial_status=  data["expected_initial_status"],
        expected_reference_status= data["expected_reference_status"] ,
        container_image=data.get("container_image"),
    )

#加载所有ready项目
def load_tasks(manifest_path: str|Path)-> tuple[TaskSpec,...]:
    manifest= load_manifest(manifest_path)
    tasks: list[TaskSpec]= []
    for task in manifest.tasks:
        if task.status != "ready":
            continue
        tasks.append(
            load_task(task.config_path)
        )
    
    return tuple(
        tasks
    )
