"""Select a repair orchestrator without loading optional graph dependencies eagerly."""

from __future__ import annotations

from collections.abc import Callable

from repofix.agents.workflow import RepairRun, run_repair_task


def repair_runner(engine: str) -> Callable[..., RepairRun]:
    if engine == "legacy":
        return run_repair_task
    if engine != "langgraph":
        raise ValueError("execution engine must be legacy or langgraph")
    try:
        from repofix.agents.langgraph_workflow import run_repair_task_langgraph
    except ModuleNotFoundError as error:
        if error.name and error.name.startswith("langgraph"):
            raise RuntimeError(
                "LangGraph is not installed; run: python -m pip install -e '.[graph]'"
            ) from error
        raise
    return run_repair_task_langgraph
