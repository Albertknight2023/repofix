"""Conservative, static call relations between indexed Python functions."""

import ast
import tokenize
from dataclasses import dataclass
from pathlib import Path

from repofix.analysis.models import SourceIndex, SourceModule, SourceSymbol


@dataclass(frozen=True, slots=True)
class CallEdge:
    caller: SourceSymbol
    callee: SourceSymbol | None
    call_line: int
    expression: str


@dataclass(frozen=True, slots=True)
class CallGraph:
    root_path: Path
    edges: tuple[CallEdge, ...]

    def callers_of(self, symbol: SourceSymbol) -> tuple[CallEdge, ...]:
        return tuple(edge for edge in self.edges if edge.callee == symbol)

    def callees_of(self, symbol: SourceSymbol) -> tuple[CallEdge, ...]:
        return tuple(
            edge for edge in self.edges
            if edge.caller == symbol and edge.callee is not None
        )


def _enclosing_function(
    module: SourceModule,
    line_number: int,
) -> SourceSymbol | None:
    candidates = [
        symbol
        for symbol in module.symbols
        if symbol.is_function and symbol.contains_line(line_number)
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda symbol: (
            -symbol.nesting_depth,
            symbol.end_line - symbol.start_line,
        ),
    )


def _imports(tree: ast.Module) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    modules: dict[str, str] = {}
    names: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                if alias.name != "*":
                    names[alias.asname or alias.name] = (node.module, alias.name)
    return modules, names


def _lookup(
    symbols: dict[tuple[str, str], SourceSymbol],
    module_name: str,
    qualified_name: str,
) -> SourceSymbol | None:
    return symbols.get((module_name, qualified_name))


def _resolve_call(
    function: ast.expr,
    caller: SourceSymbol,
    module: SourceModule,
    symbols: dict[tuple[str, str], SourceSymbol],
    imported_modules: dict[str, str],
    imported_names: dict[str, tuple[str, str]],
) -> SourceSymbol | None:
    if isinstance(function, ast.Name):
        if function.id in imported_names:
            imported_module, imported_name = imported_names[function.id]
            return _lookup(symbols, imported_module, imported_name)

        scopes = caller.qualified_name.split(".")[:-1]
        for depth in range(len(scopes), -1, -1):
            candidate = ".".join([*scopes[:depth], function.id])
            found = _lookup(symbols, module.name, candidate)
            if found is not None:
                return found
        return None

    if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
        receiver = function.value.id
        if receiver in {"self", "cls"} and caller.parent_qualified_name:
            return _lookup(
                symbols,
                module.name,
                f"{caller.parent_qualified_name}.{function.attr}",
            )
        imported_module = imported_modules.get(receiver)
        if imported_module:
            return _lookup(symbols, imported_module, function.attr)

    return None


def build_call_graph(source_index: SourceIndex) -> CallGraph:
    """Resolve direct calls conservatively; leave dynamic calls unresolved."""

    symbols = {
        (module.name, symbol.qualified_name): symbol
        for module in source_index.modules
        for symbol in module.symbols
        if symbol.is_function
    }
    edges: list[CallEdge] = []

    for module in source_index.modules:
        with tokenize.open(module.file_path) as source_file:
            tree = ast.parse(source_file.read(), filename=str(module.file_path))
        imported_modules, imported_names = _imports(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            caller = _enclosing_function(module, node.lineno)
            if caller is None:
                continue
            edges.append(
                CallEdge(
                    caller=caller,
                    callee=_resolve_call(
                        node.func,
                        caller,
                        module,
                        symbols,
                        imported_modules,
                        imported_names,
                    ),
                    call_line=node.lineno,
                    expression=ast.unparse(node.func),
                )
            )

    edges.sort(
        key=lambda edge: (
            edge.caller.relative_path.as_posix(),
            edge.call_line,
            edge.expression,
        )
    )
    return CallGraph(root_path=source_index.root_path, edges=tuple(edges))
