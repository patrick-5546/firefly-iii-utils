import re
from collections import Counter
from enum import StrEnum

from rich.console import Console
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


def print_header(console: Console, name: str) -> None:
    """Print a bold ``=== <name> ===`` per-file header."""
    console.print(f"=== {name} ===", style="bold", markup=False, highlight=False)


def print_response(console: Console, text: str) -> None:
    """Print the importer's response text with per-line color + a summary footer.

    Each line is classified by :func:`classify` and printed in the
    corresponding rich style. After the body, a ``Summary:`` line is
    appended with per-severity counts (``created`` / ``duplicate`` /
    ``issue`` / ``error``).
    """
    counts: Counter[Severity] = Counter()
    for line in text.splitlines():
        severity = classify(line)
        counts[severity] += 1
        style = _SEVERITY_STYLE[severity] or None
        console.print(line, style=style, markup=False, highlight=False)

    summary = Text("Summary: ", style="bold")
    _ = summary.append(
        f"{counts[Severity.CREATED]} created", style=_SEVERITY_STYLE[Severity.CREATED]
    )
    _ = summary.append(", ")
    _ = summary.append(
        f"{counts[Severity.DUPLICATE]} duplicates",
        style=_SEVERITY_STYLE[Severity.DUPLICATE],
    )
    _ = summary.append(", ")
    _ = summary.append(f"{counts[Severity.ISSUE]} issues", style=_SEVERITY_STYLE[Severity.ISSUE])
    _ = summary.append(", ")
    _ = summary.append(f"{counts[Severity.ERROR]} errors", style=_SEVERITY_STYLE[Severity.ERROR])
    console.print(summary, highlight=False)
