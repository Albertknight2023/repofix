"""
    数据模型必须隔离任务工作区
"""
from dataclasses import dataclass
from pathlib import Path

#root:临时工作区根目录
#repo_path:临时工作区里面的任务目录
@dataclass(frozen=True,slots=True)
class Workspace:
    root:Path
    repo_path:Path