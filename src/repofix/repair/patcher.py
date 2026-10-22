"""Apply a single-function candidate inside an isolated repository copy."""

import ast
import difflib
import io
import textwrap
import tokenize
from pathlib import Path

from repofix.agents.repair import PatchCandidate
from repofix.analysis import SourceIndex
from repofix.benchmarks.models import TaskSpec


class PatchRejected(ValueError):
    """The candidate violates a structural or path restriction."""


def validate_edit_target(task: TaskSpec, repo_path: Path, target_file: Path) -> Path:
    if target_file.is_absolute() or ".." in target_file.parts:
        raise PatchRejected("patch target must be repository-relative")
    if "tests" in target_file.parts or target_file.name.startswith("test_"):
        raise PatchRejected("patch cannot modify tests")

    resolved_repo = repo_path.resolve()
    resolved_target = (resolved_repo / target_file).resolve()
    if not resolved_target.is_relative_to(resolved_repo):
        raise PatchRejected("patch target escapes the workspace")
    if not resolved_target.is_file():
        raise PatchRejected("patch target does not exist")

    if not any(
        resolved_target.is_relative_to((resolved_repo / allowed).resolve())
        for allowed in task.allowed_edit_paths
    ):
        raise PatchRejected("patch target is outside allowed_edit_paths")
    if any(
        resolved_target.is_relative_to((resolved_repo / protected).resolve())
        for protected in task.protected_paths
    ):
        raise PatchRejected("patch target is protected")
    return resolved_target


def _one_function(source: str, expected_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    first_line = next((line for line in source.splitlines() if line.strip()), "")
    indentation = first_line[: len(first_line) - len(first_line.lstrip())]
    try:
        # Class methods can contain multiline strings whose contents begin in
        # column zero. textwrap.dedent() sees those string lines and leaves the
        # method indentation in place, incorrectly rejecting valid Python.
        # Parse the original indentation inside a synthetic class instead.
        parsed = ast.parse("class _PatchWrapper:\n" + source if indentation else source)
    except SyntaxError as error:
        raise PatchRejected("replacement function has invalid Python syntax") from error
    body = parsed.body[0].body if indentation and len(parsed.body) == 1 else parsed.body
    if len(body) != 1 or not isinstance(
        body[0], (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        raise PatchRejected("replacement must contain exactly one function")
    function = body[0]
    if function.name != expected_name:
        raise PatchRejected("replacement function name does not match target")
    if function.decorator_list:
        raise PatchRejected("replacement must not add decorators")
    return function


def _annotation_dump(annotation: ast.expr | None) -> str | None:
    return ast.dump(annotation) if annotation is not None else None


def apply_function_patch(
    *,
    task: TaskSpec,
    repo_path: Path,
    source_index: SourceIndex,
    candidate: PatchCandidate,
) -> str:
    """Replace one indexed function and return a unified diff."""

    if source_index.root_path != repo_path.resolve():
        raise PatchRejected("patch index belongs to a different workspace")
    file_path = validate_edit_target(task, repo_path, candidate.target_file)
    targets = [
        symbol for symbol in source_index.symbols
        if symbol.file_path == file_path
        and symbol.qualified_name == candidate.target_symbol
        and symbol.is_function
    ]
    if not targets:
        raise PatchRejected("patch target function is absent from AST index")
    if len(targets) > 1:
        if candidate.old_snippet is None:
            raise PatchRejected("patch target function is ambiguous in AST index")
        old_text = candidate.old_snippet.replace("\r\n", "\n")
        matching = [symbol for symbol in targets if old_text in symbol.source_code]
        if len(matching) != 1 and "\n" not in old_text:
            matching = [
                symbol for symbol in targets
                if any(line.strip() == old_text.strip() for line in symbol.source_code.splitlines()[1:])
            ]
        if len(matching) != 1:
            raise PatchRejected("patch target function is ambiguous in AST index")
        targets = matching
    target = targets[0]

    original_definition = _one_function(
        target.source_code, target.name
    )
    raw_bytes = file_path.read_bytes()
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw_bytes).readline)
    original_text = raw_bytes.decode(encoding)
    newline = "\r\n" if "\r\n" in original_text else "\n"
    original_lines = original_text.splitlines(keepends=True)
    if not 1 <= target.start_line <= target.end_line <= len(original_lines):
        raise PatchRejected("indexed function range is no longer valid")

    if candidate.old_snippet is not None or candidate.new_snippet is not None:
        if candidate.replacement_source or not candidate.old_snippet or not candidate.new_snippet:
            raise PatchRejected("snippet edit must contain old_snippet and new_snippet only")
        original_segment = "".join(original_lines[target.start_line - 1 : target.end_line])
        normalized_segment = original_segment.replace("\r\n", "\n")
        old_snippet = candidate.old_snippet.replace("\r\n", "\n")
        new_snippet = candidate.new_snippet.replace("\r\n", "\n")
        if old_snippet == new_snippet:
            raise PatchRejected("snippet replacement makes no change")
        if (
            normalized_segment.count(old_snippet) == 0
            and "\n" not in old_snippet
            and "\n" not in new_snippet
            and old_snippet == old_snippet.strip()
            and new_snippet == new_snippet.strip()
        ):
            # A model may quote one body line without its leading indentation.
            # Align only when the exact line occurs once; never guess among
            # repeated lines or normalize a multi-line edit.
            matches = [
                line for line in normalized_segment.splitlines()[1:]
                if line.strip() == old_snippet
            ]
            if len(matches) == 1:
                indentation = matches[0][:len(matches[0]) - len(matches[0].lstrip())]
                old_snippet = indentation + old_snippet
                new_snippet = indentation + new_snippet
        occurrence_count = normalized_segment.count(old_snippet)
        if occurrence_count != 1:
            detail = (
                "copy an exact line or block from the target function"
                if occurrence_count == 0 else
                "provide a longer old_snippet that identifies one occurrence"
            )
            raise PatchRejected(
                f"old_snippet matched {occurrence_count} times inside target function; "
                f"{detail}, or submit a full replacement function"
            )
        if normalized_segment.find(old_snippet) < normalized_segment.find("\n"):
            raise PatchRejected("snippet cannot replace the function signature")
        updated_segment = normalized_segment.replace(old_snippet, new_snippet, 1)
        replacement_text = updated_segment.replace("\n", newline)
        new_definition = _one_function(updated_segment, target.name)
    else:
        new_definition = _one_function(candidate.replacement_source, target.name)
        first_line = original_lines[target.start_line - 1]
        indentation = first_line[: len(first_line) - len(first_line.lstrip())]
        replacement_lines = textwrap.dedent(
            candidate.replacement_source
        ).strip("\r\n").splitlines()
        replacement_text = newline.join(
            indentation + line if line.strip() else ""
            for line in replacement_lines
        ) + newline
    if type(original_definition) is not type(new_definition):
        raise PatchRejected("replacement changes sync/async function kind")
    if (
        ast.dump(original_definition.args) != ast.dump(new_definition.args)
        or _annotation_dump(original_definition.returns)
        != _annotation_dump(new_definition.returns)
    ):
        raise PatchRejected("replacement changes the function signature")
    updated_text = (
        "".join(original_lines[: target.start_line - 1])
        + replacement_text
        + "".join(original_lines[target.end_line :])
    )
    try:
        ast.parse(updated_text, filename=str(file_path))
    except SyntaxError as error:
        raise PatchRejected("replacement breaks module syntax") from error
    if updated_text == original_text:
        raise PatchRejected("replacement makes no change")

    diff = "".join(
        difflib.unified_diff(
            original_text.splitlines(keepends=True),
            updated_text.splitlines(keepends=True),
            fromfile=f"a/{candidate.target_file.as_posix()}",
            tofile=f"b/{candidate.target_file.as_posix()}",
        )
    )
    file_path.write_bytes(updated_text.encode(encoding))
    return diff
