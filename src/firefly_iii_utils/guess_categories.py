import argparse
import csv
import io
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TextIO

from dotenv import load_dotenv
from rich.console import Console
from rich.text import Text

from .api import iter_tags, iter_transactions_for_tag
from .models import ExportArgs, TransactionSplit

CSV_HEADER = (
    "description",
    "amount",
    "date",
    "source_account",
    "destination_account",
)

COLUMN_STYLES = (
    "cyan",
    "green",
    "yellow",
    "magenta",
    "blue",
)


def _matching_tags(prefix: str) -> list[str]:
    return [tag for tag in iter_tags() if tag.startswith(prefix)]


def _parse_date(value: str) -> datetime:
    """Parse Firefly III's ISO 8601 date-time, tolerating a trailing ``Z``."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _format_date(value: str) -> str:
    return _parse_date(value).date().isoformat()


def _parse_amount(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"unparseable amount {value!r}") from exc


def _collect_rows(
    matching: list[str],
    parser: argparse.ArgumentParser,
    console: Console,
) -> list[tuple[str, str, str, str, str]]:
    """Walk every matching tag and return CSV rows for uncategorized splits.

    Enforces two invariants and calls :meth:`parser.error` on violation:

    * each transaction must contain exactly one split, and
    * a given transaction journal id must not appear under more than
      one matching tag.
    """
    seen_journals: dict[str, str] = {}
    rows: list[tuple[str, str, str, str, str]] = []
    for tag in matching:
        tag_uncategorized = 0
        tag_total = 0
        for transaction_id, splits in iter_transactions_for_tag(tag):
            tag_total += 1
            if len(splits) != 1:
                parser.error(
                    f"Transaction {transaction_id!r} under tag {tag!r} has {len(splits)} "
                    + "splits but the export script only handles single-split transactions; "
                    + "un-split the transaction in Firefly III before re-running."
                )
            split = splits[0]
            previous_tag = seen_journals.get(split.transaction_journal_id)
            if previous_tag is not None and previous_tag != tag:
                parser.error(
                    f"Transaction journal id {split.transaction_journal_id!r} appears "
                    + f"under both tag {previous_tag!r} and tag {tag!r}. The Firefly III "
                    + "Data Importer should prevent duplicate imports, so this is "
                    + "unexpected; investigate and remove the redundant tag before "
                    + "re-running."
                )
            seen_journals[split.transaction_journal_id] = tag
            if split.category_id is not None:
                continue
            tag_uncategorized += 1
            rows.append(_row_for(split, transaction_id, tag, parser))
        console.print(
            f"tag {tag!r}: {tag_uncategorized} uncategorized / {tag_total} total",
            highlight=False,
        )
    return rows


def _row_for(
    split: TransactionSplit,
    transaction_id: str,
    tag: str,
    parser: argparse.ArgumentParser,
) -> tuple[str, str, str, str, str]:
    try:
        date_str = _format_date(split.date)
    except ValueError as exc:
        parser.error(
            f"Transaction {transaction_id!r} under tag {tag!r} has unparseable date "
            + f"{split.date!r}: {exc}"
        )
    return (
        split.description,
        split.amount,
        date_str,
        split.source_name or "",
        split.destination_name or "",
    )


def _sort_key(row: tuple[str, str, str, str, str]) -> tuple[str, Decimal]:
    return row[2], _parse_amount(row[1])


def _write_csv(rows: list[tuple[str, str, str, str, str]], sink: TextIO) -> None:
    writer = csv.writer(sink)
    writer.writerow(CSV_HEADER)
    writer.writerows(rows)


def _csv_cell(value: str) -> str:
    """Return the CSV-quoted form of ``value`` (without a trailing newline).

    Uses ``csv.writer`` for a single-cell "row" so quoting, escaping,
    and embedded-comma handling match :func:`_write_csv` exactly. The
    trailing ``\\r\\n`` that ``csv.writer`` appends is stripped so the
    cell can be re-joined with commas downstream.
    """
    buf = io.StringIO()
    csv.writer(buf).writerow([value])
    return buf.getvalue().rstrip("\r\n")


def _write_csv_colored(
    rows: list[tuple[str, str, str, str, str]],
    console: Console,
) -> None:
    """Print ``rows`` as a CSV with one Rich style per column.

    Each cell goes through :func:`_csv_cell` so the visible characters
    are byte-identical to what :func:`_write_csv` would produce; only
    ANSI styling is added on top. ``soft_wrap=True`` prevents Rich from
    re-wrapping long descriptions, which would otherwise corrupt the
    CSV. Intended for the TTY path; when the destination is a file or
    pipe, callers should use :func:`_write_csv` instead so no escape
    codes leak into the output.
    """
    separator = Text(",")
    header = separator.join(
        Text(name, style=style) for name, style in zip(CSV_HEADER, COLUMN_STYLES, strict=True)
    )
    console.print(header, soft_wrap=True, highlight=False)
    for row in rows:
        line = separator.join(
            Text(_csv_cell(cell), style=style)
            for cell, style in zip(row, COLUMN_STYLES, strict=True)
        )
        console.print(line, soft_wrap=True, highlight=False)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export uncategorized Firefly III transactions in tags whose name starts with "
            "PREFIX to a CSV. Errors out if any matching tag contains a split transaction "
            "or if the same transaction appears under more than one matching tag."
        ),
    )
    _ = parser.add_argument(
        "prefix",
        help="Case-sensitive tag-name prefix; every tag whose name starts with this is included.",
    )
    _ = parser.add_argument(
        "-o",
        "--output",
        help="Write the CSV to this path. If omitted, the CSV is written to stdout.",
    )
    _ = parser.add_argument(
        "-N",
        "--no-color",
        action="store_true",
        help=(
            "Disable colored output for the CSV when it's printed to stdout. Colors are "
            "also disabled automatically when stdout is not a terminal or when the "
            "NO_COLOR environment variable is set."
        ),
    )
    args = ExportArgs.model_validate(vars(parser.parse_args()))

    _ = load_dotenv()

    console = Console(stderr=True)

    console.print(f"Looking up tags starting with {args.prefix!r}\u2026", highlight=False)
    matching = _matching_tags(args.prefix)
    if not matching:
        parser.error(
            f"No tags found whose name starts with {args.prefix!r}; "
            + "check the prefix and try again."
        )
    console.print(
        f"Matched {len(matching)} tag(s): {', '.join(repr(t) for t in matching)}",
        highlight=False,
    )

    rows = _collect_rows(matching, parser, console)
    rows.sort(key=_sort_key)
    console.print(f"Writing {len(rows)} row(s)", highlight=False)

    if args.output is None:
        stdout_console = Console(no_color=args.no_color)
        if stdout_console.is_terminal and not args.no_color:
            _write_csv_colored(rows, stdout_console)
        else:
            _write_csv(rows, sys.stdout)
            _ = sys.stdout.flush()
    else:
        out_path = Path(args.output)
        with out_path.open("w", encoding="utf-8", newline="") as sink:
            _write_csv(rows, sink)
        console.print(f"Wrote {out_path}", highlight=False)


if __name__ == "__main__":
    main()
