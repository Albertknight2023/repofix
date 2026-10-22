import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from repofix.benchmarks.models import TaskSpec
from repofix.workspace.models import Workspace

"""
因为使用完临时工作区后要删掉，所以使用contextmanager,
利用with进入文件时打开，退出文件时删掉的特性
Iterator(迭代器)和只能一次遍历的字典差不多
"""
@contextmanager
def isolated_workspace(task: TaskSpec) -> Iterator[Workspace]:
    #修复前的路径
    prefix=f"repofix-{task.id}"

    with TemporaryDirectory(prefix=prefix) as temporary_directory:
        workspace_root = Path(temporary_directory).resolve()
        workspace_repo = workspace_root / "repo"

        #shutil.copytree()：复制整个文件夹及其内容
        #src=原目录,
        #dst=目标目录,
        shutil.copytree(
            src=task.repo_path,
            dst=workspace_repo,
        )
        
        yield Workspace(
            root=workspace_root,
            repo_path=workspace_repo,
        )