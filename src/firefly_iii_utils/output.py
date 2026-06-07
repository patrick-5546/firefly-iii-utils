import re
from collections import Counter
from enum import StrEnum
from typing import Self

from pydantic import BaseModel
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
)
from rich.text import Text


class Severity(StrEnum):
    CREATED = "created"
    DUPLICATE = "duplicate"
    ISSUE = "issue"
    ERROR = "error"
    INFO = "info"
    OTHER = "other"


_SEVERITY_STYLE: dict[Severity, str] = {
    Severity.CREATED: "green",
    Severity.DUPLICATE: "yellow",
    Severity.ISSUE: "yellow",
    Severity.ERROR: "red",
    Severity.INFO: "cyan",
    Severity.OTHER: "",
}


_PATTERNS: tuple[tuple[Severity, re.Pattern[str]], ...] = (
    (
        Severity.CREATED,
        re.compile(r"Import index \d+: Created (?:withdrawal|deposit|transfer)\b"),
    ),
    (
        Severity.DUPLICATE,
        re.compile(
            r"Import index \d+: \[\w+\]: transactions\.\d+\.description: "
            + r"Duplicate of transaction #"
        ),
    ),
    (Severity.ISSUE, re.compile(r"Import index \d+:")),
    (
        Severity.ERROR,
        re.compile(
            r"(?:Error message:|Cannot find|Too many errors|"
            + r'Could not|Zero transactions|^\{"error":)'
        ),
    ),
    (
        Severity.INFO,
        re.compile(r"(?:Done converting from file|Done!$|Conversion index \()"),
    ),
)


class Counts(BaseModel):
    """Per-severity counts of importer response lines."""

    created: int = 0
    duplicate: int = 0
    issue: int = 0
    error: int = 0

    def __iadd__(self, other: "Counts") -> Self:
        self.created += other.created
        self.duplicate += other.duplicate
        self.issue += other.issue
        self.error += other.error
        return self


def classify(line: str) -> Severity:
    """Classify a single response line by severity. First matching pattern wins."""
    for severity, pattern in _PATTERNS:
        if pattern.search(line):
            return severity
    return Severity.OTHER


def make_console(no_color: bool) -> Console:
    """Build the module's Console.

    Rich auto-detects TTY and respects the ``NO_COLOR`` / ``FORCE_COLOR``
    environment variables. Passing ``no_color=True`` here forces colors
    off in addition to those checks.
    """
    return Console(no_color=no_color)


def make_progress(console: Console, *, disable: bool = False) -> Progress:
    """Build the directory-mode progress bar.

    Uses ``transient=True`` so the bar erases itself when the
    surrounding ``with progress:`` block exits, leaving no trace below
    the per-file output. Auto-disables (no output at all) when the
    console is not a TTY so piping stdout produces clean output. Pass
    ``disable=True`` for single-file invocations where a bar is not
    useful; calls on the returned Progress are still valid no-ops in
    that case.
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
        transient=True,
        disable=disable or not console.is_terminal,
    )


def print_header(console: Console, name: str) -> None:
    """Print a bold ``=== <name> ===`` per-file header."""
    console.print(f"=== {name} ===", style="bold", markup=False, highlight=False)


def _build_summary(
    label: str,
    counts: "Counts",
    *,
    prefix: str | None = None,
) -> Text:
    text = Text(f"{label}: ", style="bold")
    if prefix is not None:
        _ = text.append(prefix)
        _ = text.append(", ")
    _ = text.append(f"{counts.created} created", style=_SEVERITY_STYLE[Severity.CREATED])
    _ = text.append(", ")
    _ = text.append(f"{counts.duplicate} duplicates", style=_SEVERITY_STYLE[Severity.DUPLICATE])
    _ = text.append(", ")
    _ = text.append(f"{counts.issue} issues", style=_SEVERITY_STYLE[Severity.ISSUE])
    _ = text.append(", ")
    _ = text.append(f"{counts.error} errors", style=_SEVERITY_STYLE[Severity.ERROR])
    return text


def print_response(console: Console, text: str) -> Counts:
    """Print the importer's response text with per-line color + a summary footer.

    Each line is classified by :func:`classify` and printed in the
    corresponding rich style. After the body, a bold ``Summary:`` line
    is printed with per-severity counts. The same counts are returned
    so callers can aggregate them across multiple files.
    """
    counter: Counter[Severity] = Counter()
    for line in text.splitlines():
        severity = classify(line)
        counter[severity] += 1
        style = _SEVERITY_STYLE[severity] or None
        console.print(line, style=style, markup=False, highlight=False)

    counts = Counts(
        created=counter[Severity.CREATED],
        duplicate=counter[Severity.DUPLICATE],
        issue=counter[Severity.ISSUE],
        error=counter[Severity.ERROR],
    )
    console.print(_build_summary("Summary", counts), highlight=False)
    return counts


def print_aggregate(console: Console, n_files: int, totals: Counts) -> None:
    """Print a bold ``Aggregate: N files, ...`` footer summing across the run."""
    file_word = "file" if n_files == 1 else "files"
    console.print(
        _build_summary("Aggregate", totals, prefix=f"{n_files} {file_word}"),
        highlight=False,
    )
