"""Immutable data models for RepoFix's Python source index."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from repofix.execution.models import TracebackFrame


class SymbolKind(StrEnum):
    """Python definitions that can own a source-code range."""

    CLASS = "class"
    FUNCTION = "function"
    ASYNC_FUNCTION = "async_function"


class ParameterKind(StrEnum):
    """Kinds of parameters represented by ``ast.arguments``."""

    POSITIONAL_ONLY = "positional_only"
    POSITIONAL_OR_KEYWORD = "positional_or_keyword"
    VAR_POSITIONAL = "var_positional"
    KEYWORD_ONLY = "keyword_only"
    VAR_KEYWORD = "var_keyword"


@dataclass(frozen=True, slots=True)
class SourceParameter:
    """One function parameter extracted from the AST."""

    name: str
    kind: ParameterKind
    annotation: str | None = None
    default: str | None = None


@dataclass(frozen=True, slots=True)
class SourceSymbol:
    """One class, function, method, or nested function in a Python file."""

    name: str
    qualified_name: str
    kind: SymbolKind
    file_path: Path
    relative_path: Path
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    parent_qualified_name: str | None = None
    parameters: tuple[SourceParameter, ...] = ()
    decorators: tuple[str, ...] = ()
    docstring: str | None = None
    source_code: str = ""

    @property
    def is_function(self) -> bool:
        """Return whether this symbol can correspond to a function frame."""

        return self.kind in {
            SymbolKind.FUNCTION,
            SymbolKind.ASYNC_FUNCTION,
        }

    @property
    def nesting_depth(self) -> int:
        """Return the number of parent scopes in the qualified name."""

        return self.qualified_name.count(".")

    def contains_line(self, line_number: int) -> bool:
        """Return whether a one-based line lies inside this definition."""

        return self.start_line <= line_number <= self.end_line


@dataclass(frozen=True, slots=True)
class SourceModule:
    """A successfully parsed Python source file."""

    name: str
    file_path: Path
    relative_path: Path
    symbols: tuple[SourceSymbol, ...]


@dataclass(frozen=True, slots=True)
class ParseIssue:
    """A source file that could not be added to the AST index."""

    file_path: Path
    relative_path: Path
    message: str
    line_number: int | None = None
    column_number: int | None = None


@dataclass(frozen=True, slots=True)
class SourceIndex:
    """The queryable AST index for one repository snapshot."""

    root_path: Path
    modules: tuple[SourceModule, ...]
    issues: tuple[ParseIssue, ...] = ()

    @property
    def symbols(self) -> tuple[SourceSymbol, ...]:
        """Return every symbol in source order across indexed modules."""

        return tuple(
            symbol
            for module in self.modules
            for symbol in module.symbols
        )


@dataclass(frozen=True, slots=True)
class TracebackLocation:
    """A traceback frame mapped to a function from the source index."""

    frame: TracebackFrame
    module: SourceModule
    symbol: SourceSymbol
    exact_name_match: bool
