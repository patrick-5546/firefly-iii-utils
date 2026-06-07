import argparse
import asyncio
from decimal import Decimal
from typing import NamedTuple

from dotenv import load_dotenv
from rich.console import Console

from . import categorization
from .api import iter_categories, iter_tags, iter_transactions_for_tag
from .csv_output import emit_csv
from .models import ExportArgs, TransactionSplit
from .parsing import format_date, parse_amount

CSV_HEADER = (
    "transaction_id",
    "description",
    "amount",
    "date",
    "source_account",
    "destination_account",
    "category",
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


class Row(NamedTuple):
    """One CSV row keyed by ``transaction_journal_id``.

    Field order matches :data:`CSV_HEADER` so :meth:`visible` can
    emit the columns directly.
    """

    transaction_journal_id: str
    description: str
    amount: str
    date: str
    source_account: str
    destination_account: str
    category: str = ""

    def visible(self) -> tuple[str, str, str, str, str, str, str]:
        return (
            self.transaction_journal_id,
            self.description,
            self.amount,
            self.date,
            self.source_account,
            self.destination_account,
            self.category,
        )


def _matching_tags(prefix: str) -> list[str]:
    return [tag for tag in iter_tags() if tag.startswith(prefix)]


def _collect_rows(
    matching: list[str],
    parser: argparse.ArgumentParser,
    console: Console,
) -> list[Row]:
    """Walk every matching tag and return rows for uncategorized splits.

    Enforces two invariants and calls :meth:`parser.error` on violation:

    * each transaction must contain exactly one split, and
    * a given transaction journal id must not appear under more than
      one matching tag.
    """
    seen_journals: dict[str, str] = {}
    rows: list[Row] = []
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
) -> Row:
    try:
        date_str = format_date(split.date)
    except ValueError as exc:
        parser.error(
            f"Transaction {transaction_id!r} under tag {tag!r} has unparseable date "
            + f"{split.date!r}: {exc}"
        )
    return Row(
        transaction_journal_id=split.transaction_journal_id,
        description=split.description,
        amount=split.amount,
        date=date_str,
        source_account=split.source_name or "",
        destination_account=split.destination_name or "",
    )


def _sort_key(row: Row) -> tuple[str, Decimal]:
    return row.date, parse_amount(row.amount)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export uncategorized Firefly III transactions in tags whose name starts with "
            "PREFIX to a CSV, with a guessed category from GitHub Copilot for each row. "
            "Errors out if any matching tag contains a split transaction or if the same "
            "transaction appears under more than one matching tag."
        ),
    )
    _ = parser.add_argument(
        "prefix",
        help="Case-sensitive tag-name prefix; every tag whose name starts with this is included.",
    )
    _ = parser.add_argument(
        "-m",
        "--model",
        default=categorization.DEFAULT_MODEL,
        help=(
            "Copilot model to use for category guessing (default: "
            f"{categorization.DEFAULT_MODEL}). Examples: gpt-5, gpt-5-mini, "
            "claude-sonnet-4.5."
        ),
    )
    _ = parser.add_argument(
        "-n",
        "--no-guess",
        action="store_true",
        help=(
            "Skip the GitHub Copilot call. The CSV is still written with a `category` "
            "column, but every row's value is left blank. Useful for a quick uncategorized "
            "export without burning model calls."
        ),
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

    if args.no_guess:
        console.print(
            "Skipping category guessing (--no-guess); `category` column will be blank.",
            highlight=False,
        )
    else:
        console.print("Looking up existing categories\u2026", highlight=False)
        categories = list(iter_categories())
        console.print(f"Found {len(categories)} existing categor(y/ies)", highlight=False)

        raw_guesses = asyncio.run(
            categorization.guess(rows, categories, model=args.model, console=console)
        )
        guesses = categorization.validate(
            raw_guesses,
            set(categories),
            {row.transaction_journal_id for row in rows},
            console,
        )
        rows = [row._replace(category=guesses.get(row.transaction_journal_id, "")) for row in rows]

    rows.sort(key=_sort_key)
    console.print(f"Writing {len(rows)} row(s)", highlight=False)

    emit_csv(
        CSV_HEADER,
        [row.visible() for row in rows],
        COLUMN_STYLES,
        no_color=args.no_color,
    )


if __name__ == "__main__":
    main()
