"""Find unmatched transactions in the Firefly III ``Transfers`` category.

Manually-imported transfers between two of the user's own accounts
are recorded as **two** transactions in Firefly III (a withdrawal
from one asset account and a deposit into another), both tagged
under the ``Transfers`` category. When only one of the two bank
CSVs has been imported, the orphan transaction sits in the category
with no counterpart. This script lists those orphans for a given
month (or month range), so they can be investigated by hand.

The matching rule is intentionally simple, matching the user's
description: within the requested date range, group all
``Transfers``-category withdrawals and deposits by their absolute
amount; for each amount, pair withdrawals against deposits greedily
in date order (earliest first); whatever's left over is unmatched.
Anything that isn't a withdrawal or deposit (``transfer``,
``reconciliation``, ``opening balance``) is treated as already
two-sided and excluded from both the matching and the unmatched
CSV.

The CSV is sorted by ``(date, signed_amount)`` — where ``+amount``
is a deposit and ``-amount`` a withdrawal — and emitted colored on a
TTY / plain when redirected, sharing
:mod:`firefly_iii_utils.csv_output` with
:mod:`firefly_iii_utils.guess_categories`.
"""

import argparse
import calendar
import csv
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

from dotenv import load_dotenv
from rich.console import Console

from .api import iter_transactions_for_category, lookup_category_id
from .csv_output import emit_csv, write_csv_colored
from .models import FindUnmatchedTransfersArgs, TransactionSplit
from .parsing import format_date, parse_amount

CATEGORY_NAME = "Transfers"

CSV_HEADER = (
    "transaction_id",
    "description",
    "amount",
    "date",
    "source_account",
    "destination_account",
    "type",
)

COLUMN_STYLES = (
    "bright_black",
    "cyan",
    "green",
    "yellow",
    "magenta",
    "blue",
    "red",
)

_TRANSACTION_ID_COL = CSV_HEADER.index("transaction_id")


class Row(NamedTuple):
    """One unmatched-transaction row keyed by ``transaction_journal_id``.

    Field order matches :data:`CSV_HEADER` so :meth:`visible` can emit
    the columns directly.
    """

    transaction_journal_id: str
    description: str
    amount: str
    date: str
    source_account: str
    destination_account: str
    type: str

    def visible(self) -> tuple[str, str, str, str, str, str, str]:
        return (
            self.transaction_journal_id,
            self.description,
            self.amount,
            self.date,
            self.source_account,
            self.destination_account,
            self.type,
        )


def _parse_month(value: str) -> str:
    """argparse ``type=`` validator for ``YYYY-MM`` month strings."""
    try:
        _ = datetime.strptime(value, "%Y-%m")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid month {value!r}; expected YYYY-MM (e.g. 2026-06)"
        ) from exc
    return value


def _month_bounds(month: str) -> tuple[date, date]:
    """Return ``(first_day, last_day)`` for a validated ``YYYY-MM`` string."""
    year, mo = (int(part) for part in month.split("-"))
    last_day = calendar.monthrange(year, mo)[1]
    return date(year, mo, 1), date(year, mo, last_day)


def _row_for(
    split: TransactionSplit,
    transaction_id: str,
    parser: argparse.ArgumentParser,
) -> Row:
    try:
        date_str = format_date(split.date)
    except ValueError as exc:
        parser.error(f"Transaction {transaction_id!r} has unparseable date {split.date!r}: {exc}")
    return Row(
        transaction_journal_id=split.transaction_journal_id,
        description=split.description,
        amount=split.amount,
        date=date_str,
        source_account=split.source_name or "",
        destination_account=split.destination_name or "",
        type=split.type,
    )


def _collect_rows(
    category_id: str,
    *,
    start: str,
    end: str,
    parser: argparse.ArgumentParser,
    console: Console,
) -> tuple[list[Row], list[Row], int]:
    """Fetch transactions and bucket them into ``(withdrawals, deposits, skipped)``.

    Enforces the same single-split invariant as
    :mod:`firefly_iii_utils.guess_categories`: a transaction with more
    than one split causes the script to abort via ``parser.error``.

    Anything that isn't a ``withdrawal`` or ``deposit`` (e.g.
    ``transfer``, ``reconciliation``, ``opening balance``) is counted
    as skipped and returned so the caller can include the count in
    the stderr summary.
    """
    withdrawals: list[Row] = []
    deposits: list[Row] = []
    skipped = 0
    total = 0
    for transaction_id, splits in iter_transactions_for_category(category_id, start=start, end=end):
        total += 1
        if len(splits) != 1:
            parser.error(
                f"Transaction {transaction_id!r} in the {CATEGORY_NAME!r} category has "
                + f"{len(splits)} splits but this script only handles single-split "
                + "transactions; un-split the transaction in Firefly III before re-running."
            )
        split = splits[0]
        row = _row_for(split, transaction_id, parser)
        if split.type == "withdrawal":
            withdrawals.append(row)
        elif split.type == "deposit":
            deposits.append(row)
        else:
            skipped += 1
    console.print(
        f"Fetched {total} transaction(s) in the {CATEGORY_NAME!r} category "
        + f"({len(withdrawals)} withdrawal(s), {len(deposits)} deposit(s), "
        + f"{skipped} other).",
        highlight=False,
    )
    return withdrawals, deposits, skipped


def _match(
    withdrawals: list[Row],
    deposits: list[Row],
) -> tuple[list[tuple[Row, Row]], list[Row]]:
    """Pair withdrawals with deposits greedily by date per amount bucket.

    Returns ``(matched_pairs, unmatched_rows)``. Within each absolute
    amount, both sides are sorted by date and zipped together; the
    longer side's tail becomes part of the unmatched list, along with
    every row whose amount only appears on one side.
    """
    w_by_amount: dict[Decimal, list[Row]] = defaultdict(list)
    d_by_amount: dict[Decimal, list[Row]] = defaultdict(list)
    for row in withdrawals:
        w_by_amount[parse_amount(row.amount)].append(row)
    for row in deposits:
        d_by_amount[parse_amount(row.amount)].append(row)

    matched: list[tuple[Row, Row]] = []
    unmatched: list[Row] = []
    for amount in w_by_amount.keys() | d_by_amount.keys():
        ws = sorted(w_by_amount.get(amount, []), key=lambda r: r.date)
        ds = sorted(d_by_amount.get(amount, []), key=lambda r: r.date)
        pair_count = min(len(ws), len(ds))
        for w, d in zip(ws[:pair_count], ds[:pair_count], strict=True):
            matched.append((w, d))
        unmatched.extend(ws[pair_count:])
        unmatched.extend(ds[pair_count:])
    return matched, unmatched


def _signed_amount(row: Row) -> Decimal:
    """Return ``+amount`` for deposits, ``-amount`` for withdrawals.

    Used as a sort key so credits and debits at the same date group
    naturally (debits before credits because their signed amount is
    smaller). Any type other than withdrawal / deposit is treated as
    positive — those rows shouldn't reach the unmatched list under
    the current filter, but defaulting positive keeps the sort total.
    """
    amount = parse_amount(row.amount)
    return -amount if row.type == "withdrawal" else amount


def _signed_balance(rows: list[Row]) -> Decimal:
    """Sum :func:`_signed_amount` across ``rows``.

    Deposits contribute positively (money landed without a matching
    outgoing record), withdrawals negatively (money left without a
    matching incoming record), so the result tells the user the net
    direction of the unmatched set: positive means more arrived than
    left, negative means the opposite, zero means individual pairs
    don't line up but the totals do.
    """
    return sum((_signed_amount(row) for row in rows), Decimal(0))


def _sort_key(row: Row) -> tuple[str, Decimal]:
    return row.date, _signed_amount(row)


def _print_matched(
    matched: list[tuple[Row, Row]],
    console: Console,
) -> None:
    """Emit matched pairs to stderr as a CSV with the same per-column colors as stdout.

    Pairs are sorted by ``(date, amount)`` of the withdrawal side and
    emitted with the withdrawal row first and the deposit row second,
    so the two halves of each match sit on adjacent lines. The CSV
    header is re-printed at the top of the section for orientation;
    color stripping (TTY detection, ``NO_COLOR``, ``--no-color``) is
    handled by the underlying :class:`rich.console.Console`.
    """
    if not matched:
        return
    console.print("\nMatched pairs:", style="bold", highlight=False)
    rows: list[tuple[str, ...]] = []
    for withdrawal, deposit in sorted(
        matched,
        key=lambda pair: (pair[0].date, parse_amount(pair[0].amount)),
    ):
        rows.append(withdrawal.visible())
        rows.append(deposit.visible())
    write_csv_colored(CSV_HEADER, rows, COLUMN_STYLES, console)


def _read_reviewed(
    path: Path,
    parser: argparse.ArgumentParser,
    console: Console,
) -> set[str]:
    """Load a previously-emitted unmatched-transfers CSV and return its ids.

    The reviewed file must use the **same header** as this script's own
    output so the user's workflow is just "save stdout, delete rows
    you want to re-investigate, pass the file back in". Validation
    mirrors :func:`firefly_iii_utils.import_categories._read_csv`:
    file-existence / read errors and any structural mismatch (missing
    header, wrong column count, empty ``transaction_id``) all abort
    via ``parser.error`` before any Firefly III API calls run.

    Duplicate ``transaction_id`` values inside the file emit a stderr
    warning but are deduped into a single membership entry, since a
    set is what the strip step needs anyway.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        parser.error(f"Reviewed CSV file not found: {path}")
    except OSError as exc:
        parser.error(f"Could not read reviewed CSV {path}: {exc}")

    reader = csv.reader(text.splitlines())
    try:
        header = tuple(next(reader))
    except StopIteration:
        parser.error(f"Reviewed CSV file is empty: {path}")
    if header != CSV_HEADER:
        expected = ",".join(CSV_HEADER)
        got = ",".join(header)
        parser.error(
            f"Reviewed CSV header in {path.name} does not match the format produced by "
            + "firefly-iii-find-unmatched-transfers."
            + f"\n  expected: {expected}\n  got:      {got}"
        )

    ids: set[str] = set()
    for row_number, raw in enumerate(reader, start=2):
        if not raw or not any(cell.strip() for cell in raw):
            continue
        if len(raw) != len(CSV_HEADER):
            parser.error(
                f"Reviewed CSV row {row_number} in {path.name} has {len(raw)} column(s); "
                + f"expected {len(CSV_HEADER)}."
            )
        tx_id = raw[_TRANSACTION_ID_COL].strip()
        if not tx_id:
            parser.error(
                f"Reviewed CSV row {row_number} in {path.name} has an empty transaction_id."
            )
        if tx_id in ids:
            console.print(
                f"warning: reviewed CSV row {row_number} repeats transaction_id "
                + f"{tx_id!r}; treating as a single entry.",
                style="yellow",
                highlight=False,
            )
            continue
        ids.add(tx_id)
    return ids


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            f"Find unmatched transactions in the {CATEGORY_NAME!r} category for a given "
            "month or month range. Each withdrawal is paired with a deposit of the same "
            "absolute amount (greedy by earliest date); whatever is left over is written "
            "to stdout as a CSV sorted by (date, signed amount). Transactions of any "
            "other type (transfer, reconciliation, opening balance) are skipped because "
            "Firefly III records them atomically and they don't need a counterpart."
        ),
    )
    _ = parser.add_argument(
        "start",
        type=_parse_month,
        help="Start month (inclusive), formatted YYYY-MM, e.g. 2026-06.",
    )
    _ = parser.add_argument(
        "end",
        nargs="?",
        type=_parse_month,
        default=None,
        help=(
            "Optional end month (inclusive), formatted YYYY-MM. Defaults to START so "
            "that a single positional argument selects exactly that month. Must be "
            ">= START."
        ),
    )
    _ = parser.add_argument(
        "-r",
        "--reviewed",
        default=None,
        help=(
            "Path to a CSV produced by a previous run of this script. Any row whose "
            "transaction_id is in that file is stripped from the unmatched output. The "
            "header must match the script's own output exactly; stripping happens after "
            "pairing, so a previously-orphan transaction that now has a counterpart "
            "pairs off as normal and never reaches the strip step. Reviewed ids that "
            "aren't in the current unmatched output emit a one-line stderr note."
        ),
    )
    _ = parser.add_argument(
        "-N",
        "--no-color",
        action="store_true",
        help=(
            "Disable colored output for the matched-pairs CSV on stderr and the "
            "unmatched-transactions CSV on stdout. Colors are also disabled "
            "automatically when the respective stream is not a terminal or when the "
            "NO_COLOR environment variable is set."
        ),
    )
    args = FindUnmatchedTransfersArgs.model_validate(vars(parser.parse_args()))

    end_month = args.end if args.end is not None else args.start
    if end_month < args.start:
        parser.error(
            f"end month {end_month!r} is before start month {args.start!r}; "
            + "swap the arguments or pass a single month."
        )

    first_day, _ = _month_bounds(args.start)
    _, last_day = _month_bounds(end_month)
    start_date = first_day.isoformat()
    end_date = last_day.isoformat()

    _ = load_dotenv()

    console = Console(stderr=True, no_color=args.no_color)

    console.print(
        f"Looking up category id for {CATEGORY_NAME!r}\u2026",
        highlight=False,
    )
    category_id = lookup_category_id(CATEGORY_NAME)
    if category_id is None:
        parser.error(
            f"No category named {CATEGORY_NAME!r} found in Firefly III; "
            + "create it (and tag a transaction with it) before re-running."
        )
    console.print(
        f"Found category {CATEGORY_NAME!r} with id {category_id!r}.",
        highlight=False,
    )

    console.print(
        f"Fetching transactions from {start_date} to {end_date}\u2026",
        highlight=False,
    )
    withdrawals, deposits, _skipped = _collect_rows(
        category_id,
        start=start_date,
        end=end_date,
        parser=parser,
        console=console,
    )

    matched, unmatched = _match(withdrawals, deposits)
    console.print(
        f"Matched {len(matched)} pair(s); {len(unmatched)} unmatched transaction(s).",
        highlight=False,
    )
    _print_matched(matched, console)

    if args.reviewed is not None:
        reviewed_ids = _read_reviewed(Path(args.reviewed), parser, console)
        console.print(
            f"\nLoaded {len(reviewed_ids)} reviewed transaction id(s) from "
            + f"{args.reviewed!r}.",
            highlight=False,
        )
        current_ids = {row.transaction_journal_id for row in unmatched}
        for stale in sorted(reviewed_ids - current_ids):
            console.print(
                f"reviewed id {stale!r} no longer in unmatched output",
                style="yellow",
                highlight=False,
            )
        before = len(unmatched)
        unmatched = [row for row in unmatched if row.transaction_journal_id not in reviewed_ids]
        console.print(
            f"Stripped {before - len(unmatched)} reviewed row(s); "
            + f"{len(unmatched)} remaining.",
            highlight=False,
        )

    unmatched.sort(key=_sort_key)
    balance = _signed_balance(unmatched).quantize(Decimal("0.01"))
    console.print(
        f"Unmatched balance: {balance:+} (deposits − withdrawals).",
        highlight=False,
    )
    console.print(f"Writing {len(unmatched)} row(s)", highlight=False)

    emit_csv(
        CSV_HEADER,
        [row.visible() for row in unmatched],
        COLUMN_STYLES,
        no_color=args.no_color,
    )


if __name__ == "__main__":
    main()
