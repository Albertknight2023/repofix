"""Build a repository-wide Python symbol index with the standard AST module."""

import ast
import tokenize
from pathlib import Path

from repofix.analysis.models import (
    ParameterKind,
    ParseIssue,
    SourceIndex,
    SourceModule,
    SourceParameter,
    SourceSymbol,
    SymbolKind,
)


DEFAULT_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)


def _expression_text(expression: ast.expr | None) -> str | None:
    if expression is None:
        return None

    return ast.unparse(expression)


def _parameters(arguments: ast.arguments) -> tuple[SourceParameter, ...]:
    parameters: list[SourceParameter] = []
    positional_arguments = [*arguments.posonlyargs, *arguments.args]
    default_offset = len(positional_arguments) - len(arguments.defaults)

    for index, argument in enumerate(positional_arguments):
        if index < len(arguments.posonlyargs):
            kind = ParameterKind.POSITIONAL_ONLY
        else:
            kind = ParameterKind.POSITIONAL_OR_KEYWORD

        default_index = index - default_offset
        default = (
            _expression_text(arguments.defaults[default_index])
            if default_index >= 0
            else None
        )
        parameters.append(
            SourceParameter(
                name=argument.arg,
                kind=kind,
                annotation=_expression_text(argument.annotation),
                default=default,
            )
        )

    if arguments.vararg is not None:
        parameters.append(
            SourceParameter(
                name=arguments.vararg.arg,
                kind=ParameterKind.VAR_POSITIONAL,
                annotation=_expression_text(arguments.vararg.annotation),
            )
        )

    for argument, default_node in zip(
        arguments.kwonlyargs,
        arguments.kw_defaults,
        strict=True,
    ):
        parameters.append(
            SourceParameter(
                name=argument.arg,
                kind=ParameterKind.KEYWORD_ONLY,
                annotation=_expression_text(argument.annotation),
                default=_expression_text(default_node),
            )
        )

    if arguments.kwarg is not None:
        parameters.append(
            SourceParameter(
                name=arguments.kwarg.arg,
                kind=ParameterKind.VAR_KEYWORD,
                annotation=_expression_text(arguments.kwarg.annotation),
            )
        )

    return tuple(parameters)


class _SymbolVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        file_path: Path,
        relative_path: Path,
        source_code: str,
    ) -> None:
        self._file_path = file_path
        self._relative_path = relative_path
        self._source_code = source_code
        self._scope: list[str] = []
        self.symbols: list[SourceSymbol] = []

    def _record_symbol(
        self,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        kind: SymbolKind,
    ) -> None:
        parent_qualified_name = ".".join(self._scope) or None
        qualified_name = ".".join([*self._scope, node.name])
        parameters = (
            _parameters(node.args)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            else ()
        )
        source_segment = ast.get_source_segment(self._source_code, node) or ""

        self.symbols.append(
            SourceSymbol(
                name=node.name,
                qualified_name=qualified_name,
                kind=kind,
                file_path=self._file_path,
                relative_path=self._relative_path,
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                start_column=node.col_offset,
                end_column=node.end_col_offset or node.col_offset,
                parent_qualified_name=parent_qualified_name,
                parameters=parameters,
                decorators=tuple(
                    ast.unparse(decorator)
                    for decorator in node.decorator_list
                ),
                docstring=ast.get_docstring(node, clean=False),
                source_code=source_segment,
            )
        )

        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._record_symbol(node, SymbolKind.CLASS)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_symbol(node, SymbolKind.FUNCTION)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record_symbol(node, SymbolKind.ASYNC_FUNCTION)


def _module_name(relative_path: Path) -> str:
    parts = list(relative_path.with_suffix("").parts)

    if parts and parts[0] == "src":
        parts.pop(0)

    if parts and parts[-1] == "__init__":
        parts.pop()

    return ".".join(parts) or relative_path.parent.name or "__root__"


def _python_files(
    root_path: Path,
    excluded_directories: frozenset[str],
) -> tuple[Path, ...]:
    files: list[Path] = []

    for candidate in root_path.rglob("*.py"):
        relative_path = candidate.relative_to(root_path)

        if any(
            part in excluded_directories
            for part in relative_path.parts[:-1]
        ):
            continue

        resolved_path = candidate.resolve()
        if not resolved_path.is_relative_to(root_path):
            continue

        if resolved_path.is_file():
            files.append(resolved_path)

    return tuple(sorted(files, key=lambda path: str(path).casefold()))


def build_source_index(
    root_path: Path,
    *,
    excluded_directories: frozenset[str] = DEFAULT_EXCLUDED_DIRECTORIES,
) -> SourceIndex:
    """Parse Python files below ``root_path`` into a reusable symbol index."""

    root_path = root_path.resolve()
    if not root_path.is_dir():
        raise FileNotFoundError(
            f"source-index root directory does not exist: {root_path}"
        )

    modules: list[SourceModule] = []
    issues: list[ParseIssue] = []

    for file_path in _python_files(root_path, excluded_directories):
        relative_path = file_path.relative_to(root_path)

        try:
            with tokenize.open(file_path) as source_file:
                source_code = source_file.read()
            tree = ast.parse(
                source_code,
                filename=str(file_path),
                type_comments=True,
            )
        except (OSError, SyntaxError, UnicodeError) as error:
            issues.append(
                ParseIssue(
                    file_path=file_path,
                    relative_path=relative_path,
                    message=str(error),
                    line_number=getattr(error, "lineno", None),
                    column_number=getattr(error, "offset", None),
                )
            )
            continue

        visitor = _SymbolVisitor(
            file_path=file_path,
            relative_path=relative_path,
            source_code=source_code,
        )
        visitor.visit(tree)
        modules.append(
            SourceModule(
                name=_module_name(relative_path),
                file_path=file_path,
                relative_path=relative_path,
                symbols=tuple(visitor.symbols),
            )
        )

    return SourceIndex(
        root_path=root_path,
        modules=tuple(modules),
        issues=tuple(issues),
    )
