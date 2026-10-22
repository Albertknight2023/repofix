"""Deterministic, conservative checks for suspicious changes in Python patches."""

import ast
import difflib
from collections import defaultdict
from dataclasses import dataclass


POLICY_VERSION = "patch-review-v1"


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    code: str
    severity: str
    message: str
    line_number: int | None = None


@dataclass(frozen=True, slots=True)
class PatchReview:
    status: str
    changed_lines: int
    findings: tuple[ReviewFinding, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_version": POLICY_VERSION,
            "status": self.status,
            "changed_lines": self.changed_lines,
            "findings": [
                {
                    "code": item.code,
                    "severity": item.severity,
                    "message": item.message,
                    "line_number": item.line_number,
                }
                for item in self.findings
            ],
        }


_MESSAGES = {
    "test_skip": ("block", "补丁新增 pytest.skip/xfail，可能跳过测试或代码路径。"),
    "swallowed_exception": ("block", "补丁新增宽泛异常捕获，并用 pass 或 return None 吞掉异常。"),
    "broad_exception": ("warning", "补丁新增宽泛异常捕获，需要人工核查回退逻辑。"),
    "constant_function": ("warning", "补丁将函数逻辑替换为常量返回值，需要核查是否写死结果。"),
}


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _is_broad_handler(node: ast.ExceptHandler) -> bool:
    if node.type is None:
        return True
    names = (
        node.type.elts if isinstance(node.type, ast.Tuple) else (node.type,)
    )
    return any(
        _dotted_name(item) in {"Exception", "BaseException", "builtins.Exception", "builtins.BaseException"}
        for item in names
    )


def _swallows(node: ast.ExceptHandler) -> bool:
    return any(
        isinstance(statement, ast.Pass)
        or (
            isinstance(statement, ast.Return)
            and (statement.value is None or isinstance(statement.value, ast.Constant) and statement.value.value is None)
        )
        for statement in node.body
    )


def _features(source: str) -> dict[str, list[int]]:
    found: dict[str, list[int]] = defaultdict(list)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and _dotted_name(node.func) in {
            "pytest.skip", "pytest.xfail", "pytest.mark.skip", "pytest.mark.skipif", "pytest.mark.xfail",
        }:
            found["test_skip"].append(node.lineno)
        if isinstance(node, ast.ExceptHandler) and _is_broad_handler(node):
            found["swallowed_exception" if _swallows(node) else "broad_exception"].append(node.lineno)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and len(node.body) == 1:
            statement = node.body[0]
            if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Constant):
                found["constant_function"].append(statement.lineno)
    return found


def review_python_patch(before: str, after: str) -> PatchReview:
    """Compare the baseline and edited module; only newly introduced patterns count."""

    try:
        previous = _features(before)
        current = _features(after)
    except SyntaxError:
        return PatchReview("rejected", 0, (
            ReviewFinding("syntax_error", "block", "补丁导致 Python 语法错误。"),
        ))

    findings: list[ReviewFinding] = []
    for code, (severity, message) in _MESSAGES.items():
        new_count = len(current.get(code, [])) - len(previous.get(code, []))
        if new_count > 0:
            # Counts are reliable; an exact new location is not when old and new
            # instances share a rule, so do not invent a line number.
            line_number = current[code][0] if not previous.get(code) else None
            findings.append(ReviewFinding(code, severity, message, line_number))

    before_lines = before.splitlines()
    after_lines = after.splitlines()
    changed_lines = sum(
        max(old_end - old_start, new_end - new_start)
        for tag, old_start, old_end, new_start, new_end in
        difflib.SequenceMatcher(a=before_lines, b=after_lines).get_opcodes()
        if tag != "equal"
    )
    if changed_lines > 80:
        findings.append(ReviewFinding(
            "large_change", "warning", "补丁修改超过 80 行，需要人工核查改动范围。"
        ))

    status = (
        "rejected" if any(item.severity == "block" for item in findings)
        else "warnings" if findings else "passed"
    )
    return PatchReview(status, changed_lines, tuple(findings))
