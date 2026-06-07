import argparse
import csv
import sys
from pathlib import Path
from typing import NamedTuple

from dotenv import load_dotenv
from requests import HTTPError
from rich.console import Console

from .api import (
    get_transaction_journal,
    iter_categories,
    update_transaction_category,
)
from .guess_categories import CSV_HEADER
from .models import ImportCategoriesArgs

_JOURNAL_ID_COL = CSV_HEADER.index("transaction_id")
_CATEGORY_COL = CSV_HEADER.index("category")


class _ParsedRow(NamedTuple):
    """One CSV body row, keyed back to the source line for error messages."""

    csv_row_number: int
    transaction_journal_id: str
    category: str


class _Validated(NamedTuple):
    """A row that passed every check, paired with its parent transaction group id."""

    row: _ParsedRow
    group_id: str


def _read_csv(path: Path, parser: argparse.ArgumentParser) -> list[_ParsedRow]:
    """Read the CSV at ``path`` and return its body rows.

    Validates the header matches :data:`CSV_HEADER` exactly and that
    every body row has the expected column count. Blank lines are
    skipped. Per-cell content validation (empty ids, empty categories,
    duplicates) happens in :func:`_validate_rows` so all such issues
    can be reported together.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        parser.error(f"CSV file not found: {path}")
    except OSError as exc:
        parser.error(f"Could not read {path}: {exc}")

    reader = csv.reader(text.splitlines())
    try:
        header = tuple(next(reader))
    except StopIteration:
        parser.error(f"CSV file is empty: {path}")
    if header != CSV_HEADER:
        expected = ",".join(CSV_HEADER)
        got = ",".join(header)
        parser.error(
            f"CSV header in {path.name} does not match the format produced by "
            + f"firefly-iii-guess-categories.\n  expected: {expected}\n  got:      {got}"
        )

    rows: list[_ParsedRow] = []
    for row_number, raw in enumerate(reader, start=2):
        if not raw or not any(cell.strip() for cell in raw):
            continue
        if len(raw) != len(CSV_HEADER):
            parser.error(
                f"CSV row {row_number} in {path.name} has {len(raw)} column(s); "
                + f"expected {len(CSV_HEADER)}."
            )
        rows.append(
            _ParsedRow(
                csv_row_number=row_number,
                transaction_journal_id=raw[_JOURNAL_ID_COL].strip(),
                category=raw[_CATEGORY_COL].strip(),
            )
        )
    return rows


def _validate_rows(
    rows: list[_ParsedRow],
    existing_categories: set[str],
    console: Console,
) -> tuple[list[_Validated], list[str]]:
    """Check every row against every precondition.

    Returns ``(valid, errors)``. ``valid`` is empty if any error is
    collected; callers must not run partial updates from it. ``errors``
    is a flat list of human-readable strings, grouped by category in
    listing order (CSV-level → category-existence → transaction-state).
    """
    csv_errors: list[str] = []
    category_errors: list[str] = []
    state_errors: list[str] = []

    fmt_ready: list[_ParsedRow] = []
    seen_ids: dict[str, int] = {}
    for row in rows:
        if not row.transaction_journal_id:
            csv_errors.append(f"row {row.csv_row_number}: transaction_id is empty")
            continue
        first_seen = seen_ids.get(row.transaction_journal_id)
        if first_seen is not None:
            csv_errors.append(
                f"row {row.csv_row_number}: transaction_id="
                + f"{row.transaction_journal_id!r} also appears at row {first_seen}"
            )
            continue
        seen_ids[row.transaction_journal_id] = row.csv_row_number
        if not row.category:
            csv_errors.append(
                f"row {row.csv_row_number}: category is empty for "
                + f"transaction_id={row.transaction_journal_id!r}"
            )
            continue
        fmt_ready.append(row)

    cat_ready: list[_ParsedRow] = []
    for row in fmt_ready:
        if row.category not in existing_categories:
            category_errors.append(
                f"row {row.csv_row_number}: category {row.category!r} (for "
                + f"transaction_id={row.transaction_journal_id!r}) is not an "
                + "existing Firefly III category"
            )
            continue
        cat_ready.append(row)

    valid: list[_Validated] = []
    if cat_ready:
        console.print(
            f"Looking up {len(cat_ready)} transaction journal(s)\u2026",
            highlight=False,
        )
    for row in cat_ready:
        try:
            result = get_transaction_journal(row.transaction_journal_id)
        except HTTPError as exc:
            state_errors.append(
                f"row {row.csv_row_number}: lookup of transaction_id="
                + f"{row.transaction_journal_id!r} failed: {exc}"
            )
            continue
        if result is None:
            state_errors.append(
                f"row {row.csv_row_number}: no transaction journal found with "
                + f"id {row.transaction_journal_id!r}"
            )
            continue
        group_id, splits = result
        if len(splits) != 1:
            state_errors.append(
                f"row {row.csv_row_number}: transaction_id="
                + f"{row.transaction_journal_id!r} belongs to a group with "
                + f"{len(splits)} splits; this script only handles single-split "
                + "transactions (matching firefly-iii-guess-categories)"
            )
            continue
        split = splits[0]
        if split.category_id is not None:
            label = repr(split.category_name) if split.category_name else f"id {split.category_id}"
            state_errors.append(
                f"row {row.csv_row_number}: transaction_id="
                + f"{row.transaction_journal_id!r} already has category {label}"
            )
            continue
        valid.append(_Validated(row=row, group_id=group_id))

    errors = csv_errors + category_errors + state_errors
    if errors:
        return [], errors
    return valid, []


def _print_errors(errors: list[str], console: Console) -> None:
    console.print(
        f"\nValidation failed with {len(errors)} error(s); "
        + "refusing to update any transactions:",
        style="red",
        highlight=False,
    )
    for err in errors:
        console.print(f"  - {err}", style="red", highlight=False)


def _print_dry_run(valid: list[_Validated], console: Console) -> None:
    console.print(
        f"\n[dry run] Would update {len(valid)} transaction(s):",
        highlight=False,
    )
    for v in valid:
        console.print(
            f"  - row {v.row.csv_row_number}: "
            + f"transaction_id={v.row.transaction_journal_id} "
            + f"(group {v.group_id}) -> category {v.row.category!r}",
            highlight=False,
        )


def _apply_updates(valid: list[_Validated], console: Console) -> int:
    """PUT each update; return the count of failures.

    Per-row failures are logged as they happen so the user can re-run
    against a stripped-down CSV. The function still attempts every
    remaining update after a failure rather than aborting at the first
    error.
    """
    failures = 0
    console.print(f"\nUpdating {len(valid)} transaction(s)\u2026", highlight=False)
    for v in valid:
        try:
            update_transaction_category(
                v.group_id,
                v.row.transaction_journal_id,
                v.row.category,
            )
        except HTTPError as exc:
            failures += 1
            console.print(
                f"  FAIL row {v.row.csv_row_number}: transaction_id="
                + f"{v.row.transaction_journal_id} -> {v.row.category!r}: {exc}",
                style="red",
                highlight=False,
            )
            continue
        console.print(
            f"  OK   row {v.row.csv_row_number}: transaction_id="
            + f"{v.row.transaction_journal_id} -> {v.row.category!r}",
            style="green",
            highlight=False,
        )
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Import categories into Firefly III from a CSV produced by "
            "firefly-iii-guess-categories. For every row, the `category` cell "
            "is written to the transaction identified by `transaction_id` "
            "(which is actually a transaction_journal_id - the per-split id "
            "the export script writes under that column for readability). "
            "Validates every row upfront: rows with an empty category, "
            "categories that do not exist in Firefly III, duplicated "
            "transaction ids, or transactions that already have a category "
            "all cause the script to abort with a complete list of failures "
            "before any update is made."
        ),
    )
    _ = parser.add_argument(
        "path",
        help="Path to the CSV file to import (in firefly-iii-guess-categories' format).",
    )
    _ = parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help=(
            "Run every validation step (CSV parse, category existence, current "
            "transaction state) but skip the PUT calls."
        ),
    )
    _ = parser.add_argument(
        "-N",
        "--no-color",
        action="store_true",
        help=(
            "Disable colored output. Colors are also disabled automatically when "
            "stderr is not a terminal or when the NO_COLOR environment variable is set."
        ),
    )
    args = ImportCategoriesArgs.model_validate(vars(parser.parse_args()))

    _ = load_dotenv()

    console = Console(stderr=True, no_color=args.no_color)

    csv_path = Path(args.path)
    rows = _read_csv(csv_path, parser)
    if not rows:
        console.print(f"No data rows in {csv_path}; nothing to do.", highlight=False)
        return
    console.print(f"Parsed {len(rows)} row(s) from {csv_path}", highlight=False)

    console.print("Looking up existing categories\u2026", highlight=False)
    existing_categories = set(iter_categories())
    console.print(
        f"Found {len(existing_categories)} existing categor(y/ies)",
        highlight=False,
    )

    valid, errors = _validate_rows(rows, existing_categories, console)
    if errors:
        _print_errors(errors, console)
        sys.exit(1)

    if args.dry_run:
        _print_dry_run(valid, console)
        return

    failures = _apply_updates(valid, console)
    summary = f"\nUpdated {len(valid) - failures}/{len(valid)} transaction(s)."
    console.print(
        summary,
        style="green" if failures == 0 else "yellow",
        highlight=False,
    )
    if failures > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
