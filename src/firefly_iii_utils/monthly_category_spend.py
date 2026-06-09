"""Report per-category monthly spend across a Firefly III month range.

For a ``start`` / ``end`` month range, calls
``GET /api/v1/insight/expense/category`` and
``GET /api/v1/insight/expense/no-category`` once per month, sums each
category's expenses, and emits a wide CSV on stdout with one row per
category, one column per month, and a final ``average`` column. A
``total`` row across every kept category (plus the synthetic
``(no category)`` row) is appended at the bottom.

Single-currency only, defaulting to USD, mirroring
:mod:`firefly_iii_utils.sum_budget_diffs`. Any insight entry whose
``currency_code`` doesn't match ``--currency`` is silently skipped
so multi-currency Firefly III instances still produce a clean
single-currency report; the per-month progress line on stderr
includes the skip count whenever it's non-zero.

Spending values are displayed as positive "money out" by negating the
signed ``difference`` returned by the insight endpoints (Firefly III
reports expenses as negative numbers).
"""

import argparse
import calendar
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from dotenv import load_dotenv
from rich.console import Console

from .api import iter_insight_expense_categories, iter_insight_expense_no_category
from .csv_output import emit_csv
from .models import MonthlyCategorySpendArgs
from .parsing import parse_amount

DEFAULT_CURRENCY = "USD"

NO_CATEGORY_LABEL = "(no category)"
TOTAL_LABEL = "total"

CATEGORY_COLUMN_STYLE = "cyan"
MONTH_COLUMN_STYLE = "yellow"
AVERAGE_COLUMN_STYLE = "magenta"


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


def _months_in_range(start: str, end: str) -> list[str]:
    """Return every ``YYYY-MM`` label from ``start`` to ``end`` inclusive."""
    start_year, start_mo = (int(part) for part in start.split("-"))
    end_year, end_mo = (int(part) for part in end.split("-"))
    months: list[str] = []
    year, mo = start_year, start_mo
    while (year, mo) <= (end_year, end_mo):
        months.append(f"{year:04d}-{mo:02d}")
        mo += 1
        if mo > 12:
            mo = 1
            year += 1
    return months


def _format(value: Decimal) -> str:
    """Render ``value`` with exactly two decimal places."""
    return f"{value.quantize(Decimal('0.01'))}"


def _average(per_month: dict[str, Decimal], months: list[str]) -> Decimal:
    """Sum across ``months`` and divide by ``len(months)``.

    Months absent from ``per_month`` contribute 0 (callers pre-fill
    every month key), so the denominator stays constant across rows
    and 0-spend months pull the average down rather than being
    silently dropped.
    """
    return sum((per_month[m] for m in months), Decimal(0)) / Decimal(len(months))


def _collect_month(
    month: str,
    *,
    currency: str,
    parser: argparse.ArgumentParser,
    by_category: dict[str, dict[str, Decimal]],
    no_category: dict[str, Decimal],
) -> tuple[int, Decimal, int]:
    """Fetch one month's expense insight and accumulate into the running totals.

    Returns ``(n_categories_seen, month_total_spend, n_skipped)`` for
    the per-month stderr progress line. The signed ``difference``
    from the API is negated before accumulation so the in-memory
    values are positive "money out" matching what the CSV emits.
    Entries whose ``currency_code`` doesn't match ``currency`` are
    silently skipped and counted in ``n_skipped`` (covering both the
    per-category and uncategorized endpoints), so multi-currency
    instances still produce a clean single-currency report.
    """
    first_day, last_day = _month_bounds(month)
    start_iso = first_day.isoformat()
    end_iso = last_day.isoformat()

    n_categories = 0
    month_total = Decimal(0)
    n_skipped = 0

    for entry in iter_insight_expense_categories(start=start_iso, end=end_iso):
        if entry.currency_code != currency:
            n_skipped += 1
            continue
        try:
            spend = -parse_amount(entry.difference)
        except ValueError as exc:
            parser.error(
                f"Insight entry for category {entry.name!r} (id={entry.id!r}) in {month} "
                + f"has unparseable difference {entry.difference!r}: {exc}"
            )
        by_category[entry.name][month] += spend
        n_categories += 1
        month_total += spend

    for entry in iter_insight_expense_no_category(start=start_iso, end=end_iso):
        if entry.currency_code != currency:
            n_skipped += 1
            continue
        try:
            spend = -parse_amount(entry.difference)
        except ValueError as exc:
            parser.error(
                f"Uncategorized-insight entry for {month} has unparseable difference "
                + f"{entry.difference!r}: {exc}"
            )
        no_category[month] += spend
        month_total += spend

    return n_categories, month_total, n_skipped


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "For a START / END month range (both YYYY-MM, inclusive), call Firefly III's "
            "expense-insight endpoints once per month and emit a wide CSV on stdout with "
            "one row per category, one column per month, and a final 'average' column. "
            "A synthetic '(no category)' row covers uncategorized spending; a final "
            "'total' row sums every kept row column-by-column. Single-currency only "
            "(default USD); insight entries in a different currency are silently "
            "skipped (the per-month progress line on stderr surfaces the skip count "
            "when non-zero)."
        ),
    )
    _ = parser.add_argument(
        "start",
        type=_parse_month,
        help="Start month (inclusive), formatted YYYY-MM, e.g. 2026-01.",
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
        "-x",
        "--exclude",
        action="append",
        default=[],
        metavar="CATEGORY",
        help=(
            "Drop a category from the output and from the 'total' row. Repeatable "
            "(e.g. --exclude Transfers --exclude Salary). Names that never appear "
            "in the insight data emit a one-line stderr warning. Pass "
            f"'{NO_CATEGORY_LABEL}' to also drop the uncategorized-spend row."
        ),
    )
    _ = parser.add_argument(
        "-c",
        "--currency",
        default=DEFAULT_CURRENCY,
        help=(
            "Currency code to filter insight entries by "
            f"(default: {DEFAULT_CURRENCY}). Entries in any other currency are "
            "silently dropped from the totals; the per-month progress line on stderr "
            "surfaces the skip count when non-zero."
        ),
    )
    _ = parser.add_argument(
        "-N",
        "--no-color",
        action="store_true",
        help=(
            "Disable colored output for the CSV on stdout. Colors are also disabled "
            "automatically when stdout is not a terminal or when the NO_COLOR "
            "environment variable is set."
        ),
    )
    args = MonthlyCategorySpendArgs.model_validate(vars(parser.parse_args()))

    end_month = args.end if args.end is not None else args.start
    if end_month < args.start:
        parser.error(
            f"end month {end_month!r} is before start month {args.start!r}; "
            + "swap the arguments or pass a single month."
        )

    _ = load_dotenv()

    console = Console(stderr=True, no_color=args.no_color)

    months = _months_in_range(args.start, end_month)
    excludes = set(args.exclude)

    console.print(
        f"Fetching expense insight for {len(months)} month(s) "
        + f"({args.start} \u2192 {end_month}) in currency {args.currency!r}\u2026",
        highlight=False,
    )

    by_category: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: dict.fromkeys(months, Decimal(0))
    )
    no_category: dict[str, Decimal] = dict.fromkeys(months, Decimal(0))

    for month in months:
        n_categories, month_total, n_skipped = _collect_month(
            month,
            currency=args.currency,
            parser=parser,
            by_category=by_category,
            no_category=no_category,
        )
        skipped_suffix = f"; skipped {n_skipped} other-currency entr(y/ies)" if n_skipped else ""
        console.print(
            f"  {month}: {n_categories} category-entr(y/ies); "
            + f"month total={_format(month_total)}"
            + skipped_suffix,
            highlight=False,
        )

    seen_names = set(by_category.keys())
    if any(value != 0 for value in no_category.values()):
        seen_names.add(NO_CATEGORY_LABEL)
    for stale in sorted(excludes - seen_names):
        console.print(
            f"warning: --exclude {stale!r} did not match any category in the data",
            style="yellow",
            highlight=False,
        )

    kept_names = sorted(name for name in by_category if name not in excludes)
    include_no_category = NO_CATEGORY_LABEL not in excludes and any(
        value != 0 for value in no_category.values()
    )

    csv_rows: list[tuple[str, ...]] = []
    total_per_month: dict[str, Decimal] = dict.fromkeys(months, Decimal(0))

    for name in kept_names:
        per_month = by_category[name]
        avg = _average(per_month, months)
        csv_rows.append(
            (name, *(_format(per_month[m]) for m in months), _format(avg)),
        )
        for m in months:
            total_per_month[m] += per_month[m]

    if include_no_category:
        avg = _average(no_category, months)
        csv_rows.append(
            (NO_CATEGORY_LABEL, *(_format(no_category[m]) for m in months), _format(avg)),
        )
        for m in months:
            total_per_month[m] += no_category[m]

    total_avg = _average(total_per_month, months)
    csv_rows.append(
        (TOTAL_LABEL, *(_format(total_per_month[m]) for m in months), _format(total_avg)),
    )

    if not kept_names and not include_no_category:
        console.print(
            f"warning: no categories with spend found in {args.start}..{end_month} "
            + f"(currency {args.currency!r}); CSV will contain only the 'total' row.",
            style="yellow",
            highlight=False,
        )

    console.print(
        f"Kept {len(kept_names)} categor(y/ies)"
        + (" + (no category)" if include_no_category else "")
        + f"; total spend across range={_format(sum(total_per_month.values(), Decimal(0)))}, "
        + f"average per month={_format(total_avg)}",
        style="bold",
        highlight=False,
    )

    header = ("category", *months, "average")
    column_styles = (
        CATEGORY_COLUMN_STYLE,
        *(MONTH_COLUMN_STYLE for _ in months),
        AVERAGE_COLUMN_STYLE,
    )

    emit_csv(header, csv_rows, column_styles, no_color=args.no_color)


if __name__ == "__main__":
    main()
