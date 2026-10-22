"""Map structured pytest traceback frames to indexed Python functions."""

import os
from pathlib import Path

from repofix.analysis.models import (
    SourceIndex,
    SourceModule,
    SourceSymbol,
    TracebackLocation,
)
from repofix.execution.models import TestFailure, TracebackFrame


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _matching_module(
    source_index: SourceIndex,
    frame_path: Path,
) -> SourceModule | None:
    if frame_path.is_absolute():
        absolute_candidate = frame_path
    else:
        absolute_candidate = source_index.root_path / frame_path

    candidate_key = _path_key(absolute_candidate)
    for module in source_index.modules:
        if _path_key(module.file_path) == candidate_key:
            return module

    frame_parts = tuple(part.casefold() for part in frame_path.parts)
    suffix_matches = [
        module
        for module in source_index.modules
        if tuple(
            part.casefold()
            for part in module.relative_path.parts[-len(frame_parts):]
        )
        == frame_parts
    ]

    if len(suffix_matches) == 1:
        return suffix_matches[0]

    return None


def _best_function(
    module: SourceModule,
    frame: TracebackFrame,
) -> tuple[SourceSymbol, bool] | None:
    candidates = [
        symbol
        for symbol in module.symbols
        if symbol.is_function and symbol.contains_line(frame.line_number)
    ]

    if not candidates:
        return None

    exact_matches = [
        symbol
        for symbol in candidates
        if symbol.name == frame.function_name
        or symbol.qualified_name == frame.function_name
    ]
    pool = exact_matches or candidates
    selected = min(
        pool,
        key=lambda symbol: (
            -symbol.nesting_depth,
            symbol.end_line - symbol.start_line,
            symbol.start_line,
        ),
    )
    return selected, bool(exact_matches)


def locate_traceback_frame(
    source_index: SourceIndex,
    frame: TracebackFrame,
) -> TracebackLocation | None:
    """Locate the innermost indexed function containing one traceback frame."""

    if frame.line_number < 1:
        raise ValueError("traceback line_number must be greater than 0")

    module = _matching_module(source_index, frame.file_path)
    if module is None:
        return None

    match = _best_function(module, frame)
    if match is None:
        return None

    symbol, exact_name_match = match
    return TracebackLocation(
        frame=frame,
        module=module,
        symbol=symbol,
        exact_name_match=exact_name_match,
    )


def locate_test_failure(
    source_index: SourceIndex,
    failure: TestFailure,
) -> tuple[TracebackLocation, ...]:
    """Locate every repository-owned function in one failed-test traceback."""

    locations: list[TracebackLocation] = []

    for frame in failure.traceback_frames:
        location = locate_traceback_frame(source_index, frame)
        if location is not None:
            locations.append(location)

    return tuple(locations)
