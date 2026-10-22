"""Choose a bounded set of code snippets around a failed traceback."""

import ast
import textwrap
import tokenize
from dataclasses import dataclass
from pathlib import Path

from repofix.analysis.callgraph import CallGraph
from repofix.analysis.locator import locate_test_failure
from repofix.analysis.models import SourceIndex, SourceSymbol
from repofix.execution.models import TestFailure


@dataclass(frozen=True, slots=True)
class ContextSnippet:
    file_path: Path
    symbol_name: str
    relation: str
    call_line: int
    source_code: str


@dataclass(frozen=True, slots=True)
class ContextBundle:
    target: SourceSymbol
    related: tuple[ContextSnippet, ...]
    selection_rule: str


def _code_excerpt(symbol: SourceSymbol, *, max_lines: int = 40) -> str:
    with tokenize.open(symbol.file_path) as source_file:
        lines = source_file.readlines()
    end = min(symbol.end_line, symbol.start_line + max_lines - 1)
    return "\n".join(
        f"{number:4}: {lines[number - 1].rstrip()}"
        for number in range(symbol.start_line, end + 1)
    )


def _initializer_attribute_excerpt(
    initializer: SourceSymbol, target: SourceSymbol, *, max_lines: int = 40
) -> str:
    """Show writes to attributes read by a sibling method, even far into __init__."""

    try:
        target_tree = ast.parse(textwrap.dedent(target.source_code))
        init_tree = ast.parse(textwrap.dedent(initializer.source_code))
    except SyntaxError:
        return ""
    read_names = {
        node.attr for node in ast.walk(target_tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and isinstance(node.ctx, ast.Load)
    }
    write_lines = sorted({
        initializer.start_line + node.lineno - 1
        for node in ast.walk(init_tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and isinstance(node.ctx, ast.Store)
        and node.attr in read_names
    })
    if not write_lines:
        return ""
    with tokenize.open(initializer.file_path) as source_file:
        lines = source_file.readlines()
    selected = {initializer.start_line}
    for line in write_lines:
        selected.update(range(
            max(initializer.start_line, line - 2),
            min(initializer.end_line, line + 2) + 1,
        ))
    return "\n".join(
        f"{number:4}: {lines[number - 1].rstrip()}"
        for number in sorted(selected)[:max_lines]
    )


def _unique_imported_method_alias(
    source_index: SourceIndex,
    test_symbols: tuple[SourceSymbol, ...],
) -> SourceSymbol | None:
    """Resolve `alias = ImportedClass.method; alias(...)` only if unique."""

    candidates: set[SourceSymbol] = set()
    for test_symbol in test_symbols:
        module = next(
            (item for item in source_index.modules if item.file_path == test_symbol.file_path),
            None,
        )
        if module is None:
            continue
        with tokenize.open(module.file_path) as source_file:
            module_tree = ast.parse(source_file.read(), filename=str(module.file_path))
        imported_classes = {
            alias.asname or alias.name: alias.name
            for node in module_tree.body
            if isinstance(node, ast.ImportFrom) and node.level == 0
            for alias in node.names
        }
        try:
            test_tree = ast.parse(textwrap.dedent(test_symbol.source_code))
        except SyntaxError:
            continue
        aliases: dict[str, tuple[str, str]] = {}
        for node in ast.walk(test_tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
            ):
                aliases[node.targets[0].id] = (
                    node.value.value.id, node.value.attr
                )
        for node in ast.walk(test_tree):
            if not isinstance(node, ast.Call):
                continue
            receiver_and_method = None
            if isinstance(node.func, ast.Name):
                receiver_and_method = aliases.get(node.func.id)
            elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                receiver_and_method = (node.func.value.id, node.func.attr)
            if receiver_and_method is None:
                continue
            receiver, method = receiver_and_method
            class_name = imported_classes.get(receiver)
            if class_name is None:
                continue
            matches = [
                symbol for symbol in source_index.symbols
                if symbol.is_function
                and symbol.qualified_name == f"{class_name}.{method}"
                and "tests" not in symbol.relative_path.parts
            ]
            if len(matches) == 1:
                candidates.add(matches[0])
    return next(iter(candidates)) if len(candidates) == 1 else None


def _unique_allowed_test_name_target(
    source_index: SourceIndex,
    test_symbols: tuple[SourceSymbol, ...],
    allowed_edit_paths: tuple[str, ...],
) -> SourceSymbol | None:
    """Use a public test name only when the edit policy leaves one matching function.

    Fixture receivers have no statically known type, so calls such as
    ``shell.info()`` cannot be linked through the call graph. This is a
    low-confidence fallback, never a reason to guess among multiple symbols.
    """

    if not allowed_edit_paths:
        return None
    names = {
        candidate
        for symbol in test_symbols
        if symbol.name.startswith("test_") and len(symbol.name) > len("test_")
        for candidate in (symbol.name[len("test_"):], f"_{symbol.name[len('test_'):]}")
    }
    if not names:
        return None
    allowed = tuple(Path(path) for path in allowed_edit_paths)
    matches = {
        symbol for symbol in source_index.symbols
        if symbol.is_function
        and symbol.name in names
        and "tests" not in symbol.relative_path.parts
        and not symbol.relative_path.name.startswith("test_")
        and any(symbol.relative_path == path or path in symbol.relative_path.parents
                for path in allowed)
    }
    return next(iter(matches)) if len(matches) == 1 else None


def build_relevant_context(
    source_index: SourceIndex,
    call_graph: CallGraph,
    failure: TestFailure,
    *,
    max_related: int = 4,
    max_characters: int = 8000,
    allowed_edit_paths: tuple[str, ...] = (),
) -> ContextBundle | None:
    """Return direct callers and callees of the deepest production frame."""

    if max_related < 0 or max_characters < 0:
        raise ValueError("context limits must not be negative")
    if call_graph.root_path != source_index.root_path:
        raise ValueError("call graph and source index must share a workspace")

    locations = locate_test_failure(source_index, failure)
    production = [
        location for location in locations
        if "tests" not in location.module.relative_path.parts
        and not location.module.relative_path.name.startswith("test_")
    ]
    if production:
        target = production[-1].symbol
        selection_rule = "deepest production traceback frame"
    else:
        # Assertion failures usually stop in the test, not the called function.
        # Select only when its static direct production callee is unambiguous.
        candidates_from_tests = {
            edge.callee
            for location in locations
            if "tests" in location.module.relative_path.parts
            or location.module.relative_path.name.startswith("test_")
            for edge in call_graph.callees_of(location.symbol)
            if edge.callee is not None
            and "tests" not in edge.callee.relative_path.parts
            and not edge.callee.relative_path.name.startswith("test_")
        }
        if len(candidates_from_tests) == 1:
            target = candidates_from_tests.pop()
            selection_rule = "unique direct production callee of failing public test"
        elif candidates_from_tests:
            return None
        else:
            test_symbols = tuple(
                location.symbol for location in locations
                if "tests" in location.module.relative_path.parts
                or location.module.relative_path.name.startswith("test_")
            )
            target = _unique_imported_method_alias(source_index, test_symbols)
            if target is None:
                target = _unique_allowed_test_name_target(
                    source_index, test_symbols, allowed_edit_paths
                )
                if target is None:
                    return None
                selection_rule = "unique allowed function matching failing public test name"
            else:
                selection_rule = "unique imported method alias in failing public test"
    candidates: list[tuple[SourceSymbol, str, int]] = []
    if target.parent_qualified_name and target.name != "__init__":
        initializer = next((
            symbol for symbol in source_index.symbols
            if symbol.file_path == target.file_path
            and symbol.qualified_name == f"{target.parent_qualified_name}.__init__"
            and symbol.is_function
        ), None)
        if initializer is not None:
            candidates.append((initializer, "class_initializer", initializer.start_line))
    for edge in call_graph.callers_of(target):
        candidates.append((edge.caller, "caller", edge.call_line))
    for edge in call_graph.callees_of(target):
        assert edge.callee is not None
        candidates.append((edge.callee, "callee", edge.call_line))

    candidates.sort(
        key=lambda entry: (
            0 if entry[1] == "class_initializer" else (
                1 if entry[1] == "caller" else 2
            ),
            entry[0].relative_path.as_posix(),
            entry[2],
        )
    )
    related: list[ContextSnippet] = []
    seen: set[tuple[Path, str]] = {(target.relative_path, target.qualified_name)}
    remaining_characters = max_characters

    for symbol, relation, call_line in candidates:
        key = (symbol.relative_path, symbol.qualified_name)
        if key in seen or len(related) >= max_related or remaining_characters <= 0:
            continue
        seen.add(key)
        excerpt = (
            _initializer_attribute_excerpt(symbol, target)
            if relation == "class_initializer" else _code_excerpt(symbol)
        )
        if not excerpt:
            continue
        if len(excerpt) > remaining_characters:
            continue
        related.append(
            ContextSnippet(
                file_path=symbol.relative_path,
                symbol_name=symbol.qualified_name,
                relation=relation,
                call_line=call_line,
                source_code=excerpt,
            )
        )
        remaining_characters -= len(excerpt)

    return ContextBundle(
        target=target,
        related=tuple(related),
        selection_rule=selection_rule,
    )
