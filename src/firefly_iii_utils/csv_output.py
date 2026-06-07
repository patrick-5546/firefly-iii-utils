"""Shared CSV-emission helpers for the export scripts.

Extracted from :mod:`firefly_iii_utils.guess_categories` so multiple
scripts (e.g. ``guess-categories``, ``find-unmatched-transfers``) can
share the same per-column-colored CSV behaviour on TTY and identical
plain output when stdout is redirected.

The helpers are intentionally generic: they take a header tuple, a
sequence of row tuples (each a parallel tuple of strings), and a
parallel tuple of Rich styles. The script is responsible for shaping
its data into those tuples before calling :func:`emit_csv`.
"""

import csv
import io
import sys
from collections.abc import Sequence
from typing import TextIO

from rich.console import Console
from rich.text import Text


def csv_cell(value: str) -> str:
    """Return the CSV-quoted form of ``value`` (without a trailing newline).

    Uses ``csv.writer`` for a single-cell "row" so quoting, escaping,
    and embedded-comma handling match :func:`write_csv` exactly. The
    trailing ``\\r\\n`` that ``csv.writer`` appends is stripped so the
    cell can be re-joined with commas downstream.

    Special-cases the empty string: a single-cell csv-writer row would
    emit ``""`` (a quoted empty cell, to disambiguate from a zero-cell
    row), but ``csv.writer.writerows`` leaves an empty cell bare in
    multi-cell rows. We mirror the multi-cell behaviour so colored
    output stays byte-identical to :func:`write_csv`.
    """
    if value == "":
        return ""
    buf = io.StringIO()
    csv.writer(buf).writerow([value])
    return buf.getvalue().rstrip("\r\n")


def write_csv(
    header: Sequence[str],
    rows: Sequence[Sequence[str]],
    sink: TextIO,
) -> None:
    """Write ``header`` then every ``row`` to ``sink`` as plain CSV."""
    writer = csv.writer(sink)
    writer.writerow(header)
    writer.writerows(rows)


def write_csv_colored(
    header: Sequence[str],
    rows: Sequence[Sequence[str]],
    column_styles: Sequence[str],
    console: Console,
) -> None:
    """Print ``rows`` as a CSV with one Rich style per column.

    Each cell goes through :func:`csv_cell` so the visible characters
    are byte-identical to what :func:`write_csv` would produce; only
    ANSI styling is added on top. ``soft_wrap=True`` prevents Rich from
    re-wrapping long descriptions, which would otherwise corrupt the
    CSV. Intended for the TTY path; when the destination is a file or
    pipe, callers should use :func:`write_csv` instead so no escape
    codes leak into the output.
    """
    separator = Text(",")
    header_line = separator.join(
        Text(name, style=style) for name, style in zip(header, column_styles, strict=True)
    )
    console.print(header_line, soft_wrap=True, highlight=False)
    for row in rows:
        line = separator.join(
            Text(csv_cell(cell), style=style)
            for cell, style in zip(row, column_styles, strict=True)
        )
        console.print(line, soft_wrap=True, highlight=False)


def emit_csv(
    header: Sequence[str],
    rows: Sequence[Sequence[str]],
    column_styles: Sequence[str],
    *,
    no_color: bool,
) -> None:
    """Emit a CSV to stdout, colored on TTY and plain when redirected.

    Mirrors the dispatch logic that ``guess-categories`` used inline:
    when stdout is a terminal and ``no_color`` is ``False``, route
    through :func:`write_csv_colored`; otherwise write a plain CSV via
    :func:`write_csv` and flush. ``no_color`` also threads through to
    Rich's ``Console`` so any incidental styling (none, in practice)
    is suppressed.
    """
    stdout_console = Console(no_color=no_color)
    if stdout_console.is_terminal and not no_color:
        write_csv_colored(header, rows, column_styles, stdout_console)
    else:
        write_csv(header, rows, sys.stdout)
        _ = sys.stdout.flush()
