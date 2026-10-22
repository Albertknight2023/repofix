from dataclasses import dataclass
from pathlib import Path
from enum import StrEnum

#记录TestRunResult完整pytest运行所有可能的结果
#StrEnum 是“字符串枚举”类型
class TestRunStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ERROR = "error"

#堆栈中的一层
# 错误代码的文件路径，代码行号，函数名，源代码
@dataclass(frozen=True,slots=True)
class TracebackFrame:
    file_path: Path
    line_number: int
    function_name: str
    source_line: str | None = None
#一个失败测试
"""
pytest在测试中有三个阶段:setup 准备阶段,call执行函数阶段,teardown 清理函数阶段
def example()-> Iterator[str]
        print()
    yield
        print()
    finally
        print()
with example() as value:
    print("2.", value)

yeild前面是setup前置操作
yeild后面是teardown后置操作
"""


@dataclass(frozen=True,slots=True)
class TestFailure:
    test_id: str
    phase: str
    exception_type: str
    message: str
    traceback_frames:tuple[TracebackFrame,...]=() #默认为空元组

#一次完整的 pytest 运行 ,包含多个TestFailure
@dataclass(frozen=True,slots=True)
class TestRunResult:
    status: TestRunStatus
    command: tuple[str,...] #examlpe: python -m pytest tests -q
    working_path: Path #命令的工作目录
    exit_code: int | None

    stdout: str #standard out标准输出
    stderr: str #standard error标准错误
    duration_seconds: float #命令运行持续时间
    testfailures:tuple[TestFailure,...]=()

    @property
    def passed(self) -> bool:
        return self.status is TestRunStatus.PASSED

    @property
    def timed_out(self) -> bool:
        return self.status is TestRunStatus.TIMEOUT
