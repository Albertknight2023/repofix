"""Static-analysis primitives used to build evidence for repair agents."""

from repofix.analysis.indexer import build_source_index
from repofix.analysis.callgraph import CallEdge, CallGraph, build_call_graph
from repofix.analysis.context import ContextBundle, ContextSnippet, build_relevant_context
from repofix.analysis.locator import locate_test_failure, locate_traceback_frame
from repofix.analysis.models import (
    ParameterKind,
    ParseIssue,
    SourceIndex,
    SourceModule,
    SourceParameter,
    SourceSymbol,
    SymbolKind,
    TracebackLocation,
)

__all__ = [
    "ParameterKind",
    "CallEdge",
    "CallGraph",
    "ContextBundle",
    "ContextSnippet",
    "ParseIssue",
    "SourceIndex",
    "SourceModule",
    "SourceParameter",
    "SourceSymbol",
    "SymbolKind",
    "TracebackLocation",
    "build_source_index",
    "build_call_graph",
    "build_relevant_context",
    "locate_test_failure",
    "locate_traceback_frame",
]
