"""Compare per-model category guesses against a golden CSV.

Auto-discovers ``guessed.golden.csv`` and every other ``guessed.*.csv``
file in the given directory (cwd by default), validates that they share
the canonical header and row order produced by
:mod:`firefly_iii_utils.guess_categories`, and prints multi-section
accuracy stats to stdout:

* overall accuracy per model,
* per-category accuracy (golden category vs each model's pick),
* top-N confusions per model (golden -> predicted),
* pairwise inter-model agreement and a unanimous-agreement breakdown.

The script is pure CSV analysis — no LLM / Copilot SDK calls, no
Firefly III API calls.
"""

import argparse
import csv
from collections import Counter
from pathlib import Path
from typing import NamedTuple

from rich.console import Console
from rich.table import Table

from .models import CompareGuessesArgs

CSV_HEADER = (
    "transaction_id",
    "description",
    "amount",
    "date",
    "source_account",
    "destination_account",
    "category",
)

GOLDEN_FILENAME = "guessed.golden.csv"
MODEL_GLOB = "guessed.*.csv"
DEFAULT_TOP_CONFUSIONS = 8
BLANK_LABEL = "<blank>"


class GuessRow(NamedTuple):
    """One CSV row's key columns plus its predicted category.

    Field order matches :data:`CSV_HEADER` so a row can be constructed
    directly from a ``csv.reader`` row via ``GuessRow(*raw)``.
    """

    transaction_id: str
    description: str
    amount: str
    date: str
    source_account: str
    destination_account: str
    category: str

    def key(self) -> tuple[str, str, str, str, str, str]:
        """Return the columns used for cross-file row alignment (everything except category)."""
        return (
            self.transaction_id,
            self.description,
            self.amount,
            self.date,
            self.source_account,
            self.destination_account,
        )


class _ModelTotals(NamedTuple):
    correct: int
    wrong: int
    blank_pred: int
    judged: int


def _model_name_for(path: Path) -> str:
    """Strip the leading ``guessed.`` and trailing ``.csv`` from ``path.name``."""
    name = path.name
    if not (name.startswith("guessed.") and name.endswith(".csv")):
        raise ValueError(f"Unexpected guessed CSV filename: {name!r}")
    return name[len("guessed.") : -len(".csv")]


def _discover(
    directory: Path,
    parser: argparse.ArgumentParser,
) -> tuple[Path, list[Path]]:
    """Find the golden CSV and every other ``guessed.*.csv`` under ``directory``."""
    if not directory.is_dir():
        parser.error(f"Not a directory: {directory}")
    golden_path = directory / GOLDEN_FILENAME
    if not golden_path.is_file():
        parser.error(
            f"Golden file not found: {golden_path}. Expected a CSV named "
            + f"{GOLDEN_FILENAME!r} in the target directory."
        )
    model_paths = sorted(p for p in directory.glob(MODEL_GLOB) if p.is_file() and p != golden_path)
    if not model_paths:
        parser.error(
            f"No model files found in {directory}. Expected one or more files "
            + f"matching {MODEL_GLOB!r} besides {GOLDEN_FILENAME!r}."
        )
    return golden_path, model_paths


def _read_csv(path: Path, parser: argparse.ArgumentParser) -> list[GuessRow]:
    """Read ``path`` and return its body rows, after validating the header.

    Calls :meth:`parser.error` (which exits) on any header mismatch or
    malformed row, mirroring the strict input handling of the other
    scripts in this package.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        parser.error(f"Could not read {path}: {exc}")
    reader = csv.reader(text.splitlines())
    try:
        header = tuple(next(reader))
    except StopIteration:
        parser.error(f"CSV file is empty: {path}")
    if header != CSV_HEADER:
        parser.error(
            f"Unexpected header in {path.name}:\n"
            + f"  expected: {','.join(CSV_HEADER)}\n"
            + f"  got:      {','.join(header)}"
        )
    rows: list[GuessRow] = []
    for row_number, raw in enumerate(reader, start=2):
        if not raw or not any(cell.strip() for cell in raw):
            continue
        if len(raw) != len(CSV_HEADER):
            parser.error(
                f"{path.name} row {row_number} has {len(raw)} column(s); "
                + f"expected {len(CSV_HEADER)}."
            )
        rows.append(GuessRow(*raw))
    return rows


def _validate_alignment(
    name: str,
    golden: list[GuessRow],
    rows: list[GuessRow],
    parser: argparse.ArgumentParser,
) -> None:
    """Confirm ``rows`` aligns with ``golden`` row-by-row on every key column.

    The user explicitly assumes the model CSVs share the golden file's
    header and row order; this guard catches an out-of-order or
    drifted file before any stats are computed (an unnoticed mismatch
    would silently inflate the wrong / right counts).
    """
    if len(rows) != len(golden):
        parser.error(f"{name}: row count {len(rows)} does not match golden's {len(golden)}.")
    for index, (g, r) in enumerate(zip(golden, rows, strict=True)):
        if g.key() != r.key():
            parser.error(
                f"{name}: row {index + 2} key columns do not match golden:\n"
                + f"  golden: {g.key()}\n"
                + f"  {name}: {r.key()}"
            )


def _overall_totals(golden: list[GuessRow], rows: list[GuessRow]) -> _ModelTotals:
    """Compute correct / wrong / blank-pred / judged counts.

    Rows where golden's category is empty are excluded from every
    bucket: the script has no ground truth to grade them against. A
    blank model prediction is counted separately from a wrong one so
    callers can distinguish "model abstained" from "model guessed
    wrong".
    """
    correct = wrong = blank_pred = judged = 0
    for g, r in zip(golden, rows, strict=True):
        if g.category == "":
            continue
        judged += 1
        if r.category == "":
            blank_pred += 1
        elif r.category == g.category:
            correct += 1
        else:
            wrong += 1
    return _ModelTotals(correct, wrong, blank_pred, judged)


def _golden_category_totals(golden: list[GuessRow]) -> dict[str, int]:
    """Count labeled golden rows per category."""
    out: Counter[str] = Counter()
    for r in golden:
        if r.category != "":
            out[r.category] += 1
    return dict(out)


def _per_category_correct(golden: list[GuessRow], rows: list[GuessRow]) -> Counter[str]:
    """Count rows the model predicted correctly, keyed by golden category."""
    out: Counter[str] = Counter()
    for g, r in zip(golden, rows, strict=True):
        if g.category != "" and r.category == g.category:
            out[g.category] += 1
    return out


def _confusions(golden: list[GuessRow], rows: list[GuessRow]) -> Counter[tuple[str, str]]:
    """Return ``Counter[(golden_cat, predicted_cat)]`` over labeled-row mismatches."""
    confusion: Counter[tuple[str, str]] = Counter()
    for g, r in zip(golden, rows, strict=True):
        if g.category == "" or r.category == g.category:
            continue
        pred = r.category if r.category != "" else BLANK_LABEL
        confusion[(g.category, pred)] += 1
    return confusion


def _print_summary(
    golden_path: Path,
    model_paths: list[Path],
    golden: list[GuessRow],
    console: Console,
) -> None:
    n_blank = sum(1 for r in golden if r.category == "")
    console.print(f"Golden: {golden_path} ({len(golden)} row(s))", highlight=False)
    if n_blank:
        console.print(
            f"  {n_blank} row(s) have a blank category in golden and are "
            + "excluded from accuracy stats.",
            highlight=False,
        )
    console.print(
        f"Models ({len(model_paths)}): " + ", ".join(_model_name_for(p) for p in model_paths),
        highlight=False,
    )


def _print_overall(
    model_names: list[str],
    model_totals: dict[str, _ModelTotals],
    console: Console,
) -> None:
    table = Table(title="Overall accuracy", title_style="bold")
    table.add_column("Model", style="cyan")
    table.add_column("Correct", justify="right")
    table.add_column("Wrong", justify="right", style="red")
    table.add_column("Blank pred", justify="right", style="yellow")
    table.add_column("Judged", justify="right")
    table.add_column("Accuracy", justify="right", style="green")
    for name in model_names:
        t = model_totals[name]
        acc = (t.correct / t.judged * 100) if t.judged else 0.0
        table.add_row(
            name,
            str(t.correct),
            str(t.wrong),
            str(t.blank_pred),
            str(t.judged),
            f"{acc:.1f}%",
        )
    console.print(table)


def _print_per_category(
    model_names: list[str],
    golden_totals: dict[str, int],
    per_model_correct: dict[str, Counter[str]],
    console: Console,
) -> None:
    table = Table(
        title="Per-category accuracy (golden vs each model)",
        title_style="bold",
    )
    table.add_column("Golden category", style="cyan")
    table.add_column("Total", justify="right")
    for name in model_names:
        table.add_column(name, justify="right")
    for cat in sorted(golden_totals):
        total = golden_totals[cat]
        cells = [cat, str(total)]
        for name in model_names:
            correct = per_model_correct[name].get(cat, 0)
            cells.append(f"{correct}/{total} ({correct / total * 100:.0f}%)")
        table.add_row(*cells)
    console.print(table)


def _print_confusions(
    model_names: list[str],
    confusions: dict[str, Counter[tuple[str, str]]],
    top_n: int,
    console: Console,
) -> None:
    console.print(
        f"\nTop {top_n} confusions per model",
        style="bold",
        highlight=False,
    )
    for name in model_names:
        table = Table(title=name, title_style="bold cyan")
        table.add_column("Count", justify="right")
        table.add_column("Golden", style="green")
        table.add_column("Predicted", style="red")
        items = confusions[name].most_common(top_n)
        if not items:
            table.add_row("-", "-", "no misses")
        for (golden_cat, pred), n in items:
            table.add_row(str(n), golden_cat, pred)
        console.print(table)


def _print_agreement(
    model_names: list[str],
    rows_by_model: dict[str, list[GuessRow]],
    golden: list[GuessRow],
    top_n: int,
    console: Console,
) -> None:
    n = len(golden)
    pair_table = Table(title="Pairwise inter-model agreement", title_style="bold")
    pair_table.add_column("Model A", style="cyan")
    pair_table.add_column("Model B", style="cyan")
    pair_table.add_column("Agree", justify="right")
    pair_table.add_column("Of", justify="right")
    pair_table.add_column("Pct", justify="right", style="green")
    for i, a in enumerate(model_names):
        for b in model_names[i + 1 :]:
            agree = sum(
                1
                for ra, rb in zip(rows_by_model[a], rows_by_model[b], strict=True)
                if ra.category == rb.category
            )
            pair_table.add_row(a, b, str(agree), str(n), f"{agree / n * 100:.1f}%")
    console.print(pair_table)

    all_agree = 0
    unanimous_correct = 0
    unanimous_wrong: Counter[tuple[str, str]] = Counter()
    for index, g in enumerate(golden):
        preds = [rows_by_model[m][index].category for m in model_names]
        if len(set(preds)) != 1:
            continue
        all_agree += 1
        if g.category == "":
            continue
        pred = preds[0]
        if pred == g.category:
            unanimous_correct += 1
        else:
            label = pred if pred != "" else BLANK_LABEL
            unanimous_wrong[(g.category, label)] += 1

    unanimous_wrong_count = sum(unanimous_wrong.values())
    pct = (all_agree / n * 100) if n else 0.0
    summary = f"\nAll {len(model_names)} models agree on {all_agree}/{n} row(s) ({pct:.1f}%)."
    if all_agree:
        summary += (
            f"\n  Unanimously correct: {unanimous_correct}"
            + f"\n  Unanimously wrong:   {unanimous_wrong_count}"
        )
    console.print(summary, highlight=False)
    if unanimous_wrong_count:
        wrong_table = Table(
            title=f"Top {top_n} unanimous-wrong patterns",
            title_style="bold",
        )
        wrong_table.add_column("Count", justify="right")
        wrong_table.add_column("Golden", style="green")
        wrong_table.add_column("All models said", style="red")
        for (golden_cat, pred), count in unanimous_wrong.most_common(top_n):
            wrong_table.add_row(str(count), golden_cat, pred)
        console.print(wrong_table)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            f"Compare per-model category guesses against {GOLDEN_FILENAME!r}. "
            f"Auto-discovers the golden CSV and every other {MODEL_GLOB!r} file in "
            "the given directory (cwd by default), validates that they share the "
            "canonical header and row order, then prints overall accuracy, "
            "per-category accuracy, top-N confusions per model, and pairwise "
            "inter-model agreement to stdout. No LLM / API calls."
        ),
    )
    _ = parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help=(
            f"Directory containing {GOLDEN_FILENAME!r} and one or more "
            f"{MODEL_GLOB!r} files (default: current directory)."
        ),
    )
    _ = parser.add_argument(
        "-t",
        "--top-confusions",
        type=int,
        default=DEFAULT_TOP_CONFUSIONS,
        help=(
            "How many top (golden -> predicted) miss patterns to show per "
            f"model (default: {DEFAULT_TOP_CONFUSIONS})."
        ),
    )
    _ = parser.add_argument(
        "-N",
        "--no-color",
        action="store_true",
        help=(
            "Disable colored output. Colors are also disabled automatically "
            "when stdout is not a terminal or when the NO_COLOR environment "
            "variable is set."
        ),
    )
    args = CompareGuessesArgs.model_validate(vars(parser.parse_args()))

    console = Console(no_color=args.no_color)

    directory = Path(args.path)
    golden_path, model_paths = _discover(directory, parser)

    golden = _read_csv(golden_path, parser)
    if not golden:
        parser.error(f"Golden file {golden_path} has no data rows.")

    rows_by_model: dict[str, list[GuessRow]] = {}
    for path in model_paths:
        name = _model_name_for(path)
        rows = _read_csv(path, parser)
        _validate_alignment(path.name, golden, rows, parser)
        rows_by_model[name] = rows

    model_names = sorted(rows_by_model)

    _print_summary(golden_path, model_paths, golden, console)

    model_totals = {name: _overall_totals(golden, rows_by_model[name]) for name in model_names}
    golden_totals = _golden_category_totals(golden)
    per_model_correct = {
        name: _per_category_correct(golden, rows_by_model[name]) for name in model_names
    }
    confusions = {name: _confusions(golden, rows_by_model[name]) for name in model_names}

    _print_overall(model_names, model_totals, console)
    _print_per_category(model_names, golden_totals, per_model_correct, console)
    _print_confusions(model_names, confusions, args.top_confusions, console)
    _print_agreement(model_names, rows_by_model, golden, args.top_confusions, console)


if __name__ == "__main__":
    main()
