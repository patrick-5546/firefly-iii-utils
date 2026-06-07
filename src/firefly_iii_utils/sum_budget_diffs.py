"""Sum monthly Firefly III budget differences across a calendar year.

For a given year, fetches every monthly budget limit and the spending
attributed to it, computes ``budgeted - spent`` for each month, and
emits a CSV on stdout with one row per kept month plus a grand-total
row. Months where either the budgeted total or the spent total is 0
are dropped (those are future months or months whose transactions
have not yet been imported, per the user's workflow).

The script makes one ``GET /api/v1/budget-limits`` call per month,
which:

- Makes spent-attribution unambiguous: Firefly III's ``spent`` array
  on a budget limit is keyed off the limit's own start / end, so a
  monthly limit returned from a monthly query already aggregates
  exactly the right transactions.
- Lets us enforce that every returned limit's date range matches the
  queried month exactly; any non-monthly limit (quarterly / yearly)
  causes a hard error so the per-month totals are never silently
  wrong.

Currency handling is single-currency, defaulting to USD. Any budget
limit ``amount`` or ``spent[]`` entry in a different currency is a
hard error (the user does not have multi-currency budgets).
"""

import argparse
import calendar
from datetime import date, datetime
from decimal import Decimal
from typing import NamedTuple

from dotenv import load_dotenv
from rich.console import Console

from .api import iter_budget_limits
from .csv_output import emit_csv
from .models import BudgetLimitData, CurrencySumEntry, SumBudgetDiffsArgs
from .parsing import parse_amount, parse_date

DEFAULT_CURRENCY = "USD"

CSV_HEADER = (
    "month",
    "budgeted",
    "spent",
    "difference",
)

COLUMN_STYLES = (
    "cyan",
    "green",
    "yellow",
    "magenta",
)

TOTAL_LABEL = "total"


class MonthRow(NamedTuple):
    """One CSV row: a single month's totals (or the grand-total summary).

    Amounts are pre-formatted strings (``Decimal.quantize`` already
    applied) so :meth:`visible` can emit the columns directly without
    further conversion. The ``total`` row uses ``month=TOTAL_LABEL``.
    """

    month: str
    budgeted: str
    spent: str
    difference: str

    def visible(self) -> tuple[str, str, str, str]:
        return (self.month, self.budgeted, self.spent, self.difference)


def _parse_year(value: str) -> int:
    """argparse ``type=`` validator for the YEAR positional."""
    try:
        year = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid year {value!r}; expected an integer like 2026"
        ) from exc
    if not (1900 <= year <= 9999):
        raise argparse.ArgumentTypeError(f"invalid year {value!r}; must be between 1900 and 9999")
    return year


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    """Return ``(first_day, last_day)`` for a ``year`` / 1..12 ``month``."""
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _spent_for_currency(spent: list[CurrencySumEntry], currency: str) -> Decimal:
    """Return the sum of every ``spent[]`` entry whose ``currency_code`` matches.

    There should normally be at most one entry per currency, but if the
    API returns the same currency more than once we sum them rather
    than dropping the duplicates silently.
    """
    total = Decimal(0)
    for entry in spent:
        if entry.currency_code == currency:
            total += parse_amount(entry.sum)
    return total


def _validate_limit(
    limit: BudgetLimitData,
    *,
    first_day: date,
    last_day: date,
    month_label: str,
    currency: str,
    parser: argparse.ArgumentParser,
) -> None:
    """Abort via ``parser.error`` if ``limit`` is non-monthly or wrong-currency.

    Single-currency only: the limit's own ``currency_code`` must match
    the user's choice, and every ``spent[]`` entry's ``currency_code``
    must too (no silent dropping of other-currency spent).
    """
    if limit.attributes.currency_code != currency:
        parser.error(
            f"Budget limit id={limit.id!r} for {month_label} has currency "
            + f"{limit.attributes.currency_code!r}, expected {currency!r}; pass "
            + "--currency to override or unify your budget limits."
        )
    for entry in limit.attributes.spent:
        if entry.currency_code != currency:
            parser.error(
                f"Budget limit id={limit.id!r} for {month_label} has a spent entry "
                + f"in currency {entry.currency_code!r}, expected {currency!r}; pass "
                + "--currency to override or unify your budget limits."
            )
    try:
        limit_start = parse_date(limit.attributes.start).date()
        limit_end = parse_date(limit.attributes.end).date()
    except ValueError as exc:
        parser.error(
            f"Budget limit id={limit.id!r} for {month_label} has an unparseable date: {exc}"
        )
    if limit_start != first_day or limit_end != last_day:
        parser.error(
            f"Budget limit id={limit.id!r} returned for {month_label} covers "
            + f"{limit_start.isoformat()}..{limit_end.isoformat()}, which is not a single "
            + "calendar month. This script only supports monthly budget limits; "
            + "split quarterly / yearly budgets into monthly limits before re-running."
        )


def _collect_month(
    year: int,
    month: int,
    *,
    currency: str,
    parser: argparse.ArgumentParser,
) -> tuple[Decimal, Decimal, int]:
    """Fetch one month's budget limits and return ``(budgeted, spent_signed, n_limits)``.

    ``spent_signed`` is the raw sum of ``spent[].sum`` values (negative
    in Firefly III's convention, since withdrawals are negative). The
    caller flips the sign for display so the CSV column reads as a
    positive "money out" figure.
    """
    first_day, last_day = _month_bounds(year, month)
    month_label = f"{year:04d}-{month:02d}"
    budgeted = Decimal(0)
    spent_signed = Decimal(0)
    n_limits = 0
    for limit in iter_budget_limits(
        start=first_day.isoformat(),
        end=last_day.isoformat(),
    ):
        _validate_limit(
            limit,
            first_day=first_day,
            last_day=last_day,
            month_label=month_label,
            currency=currency,
            parser=parser,
        )
        n_limits += 1
        try:
            budgeted += parse_amount(limit.attributes.amount)
        except ValueError as exc:
            parser.error(
                f"Budget limit id={limit.id!r} for {month_label} has unparseable "
                + f"amount {limit.attributes.amount!r}: {exc}"
            )
        spent_signed += _spent_for_currency(limit.attributes.spent, currency)
    return budgeted, spent_signed, n_limits


def _format(value: Decimal) -> str:
    """Render ``value`` with exactly two decimal places."""
    return f"{value.quantize(Decimal('0.01'))}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "For a calendar YEAR, fetch every monthly budget limit, sum the "
            "budgeted and spent totals per month, and emit a CSV on stdout "
            "with one row per month plus a final 'total' row. Months where "
            "either the budgeted or the spent total is 0 are skipped (those "
            "are future months or months whose transactions have not yet "
            "been imported). The script only supports monthly budget limits "
            "in a single currency; non-monthly limits or limits in a "
            "currency other than --currency cause a hard error."
        ),
    )
    _ = parser.add_argument(
        "year",
        nargs="?",
        type=_parse_year,
        default=datetime.now().year,
        help=(
            "Calendar year to summarize, e.g. 2026. Defaults to the current "
            "calendar year (%(default)s)."
        ),
    )
    _ = parser.add_argument(
        "-c",
        "--currency",
        default=DEFAULT_CURRENCY,
        help=(
            "Currency code to filter budget limits and spent totals by "
            f"(default: {DEFAULT_CURRENCY}). Any budget limit amount or spent "
            "entry in a different currency causes a hard error."
        ),
    )
    _ = parser.add_argument(
        "-N",
        "--no-color",
        action="store_true",
        help=(
            "Disable colored output for the CSV on stdout. Colors are also "
            "disabled automatically when stdout is not a terminal or when "
            "the NO_COLOR environment variable is set."
        ),
    )
    args = SumBudgetDiffsArgs.model_validate(vars(parser.parse_args()))

    _ = load_dotenv()

    console = Console(stderr=True, no_color=args.no_color)

    console.print(
        f"Summing budget differences for {args.year} in currency {args.currency!r}\u2026",
        highlight=False,
    )

    kept_rows: list[MonthRow] = []
    total_budgeted = Decimal(0)
    total_spent = Decimal(0)
    total_limits = 0
    n_kept = 0

    for month in range(1, 13):
        month_label = f"{args.year:04d}-{month:02d}"
        budgeted, spent_signed, n_limits = _collect_month(
            args.year,
            month,
            currency=args.currency,
            parser=parser,
        )
        total_limits += n_limits
        spent_displayed = -spent_signed
        difference = budgeted - spent_displayed
        if budgeted == 0 or spent_displayed == 0:
            console.print(
                f"  {month_label}: skipping ({n_limits} limit(s); "
                + f"budgeted={_format(budgeted)}, spent={_format(spent_displayed)})",
                style="yellow",
                highlight=False,
            )
            continue
        n_kept += 1
        console.print(
            f"  {month_label}: budgeted={_format(budgeted)}, "
            + f"spent={_format(spent_displayed)}, difference={_format(difference)} "
            + f"({n_limits} limit(s))",
            highlight=False,
        )
        kept_rows.append(
            MonthRow(
                month=month_label,
                budgeted=_format(budgeted),
                spent=_format(spent_displayed),
                difference=_format(difference),
            )
        )
        total_budgeted += budgeted
        total_spent += spent_displayed

    if total_limits == 0:
        parser.error(
            f"No budget limits found in Firefly III for {args.year}; "
            + "create monthly budget limits before re-running."
        )

    if n_kept == 0:
        console.print(
            f"warning: every month in {args.year} has either budgeted=0 or spent=0; "
            + "total will be 0.",
            style="yellow",
            highlight=False,
        )

    total_diff = total_budgeted - total_spent
    kept_rows.append(
        MonthRow(
            month=TOTAL_LABEL,
            budgeted=_format(total_budgeted),
            spent=_format(total_spent),
            difference=_format(total_diff),
        )
    )

    console.print(
        f"Kept {n_kept} month(s); "
        + f"total budgeted={_format(total_budgeted)}, "
        + f"total spent={_format(total_spent)}, "
        + f"total difference={_format(total_diff)}",
        style="bold",
        highlight=False,
    )

    emit_csv(
        CSV_HEADER,
        [row.visible() for row in kept_rows],
        COLUMN_STYLES,
        no_color=args.no_color,
    )


if __name__ == "__main__":
    main()
