import csv
import io
from decimal import Decimal

from .parsing import parse_amount

_PAYPAL_REQUIRED_COLUMNS = ("Name", "Type", "Status", "Currency", "Total", "Balance")
_PAYPAL_HOLD_TYPES = frozenset(
    {
        "Account Hold for Open Authorization",
        "Reversal of General Account Hold",
    }
)
_PAYPAL_SAVINGS_TYPE = "General GI/Open wallet Transaction"


def _merge_credit_into_debit(csv_bytes: bytes, *, negate: bool) -> tuple[bytes, str]:
    """Move every Credit value into Debit, dropping the Credit cell.

    Shared logic for banks that split their amount across two columns
    (``Debit`` for charges, ``Credit`` for payments / refunds) while
    the importer template only points its ``amount`` role at ``Debit``.

    When ``negate`` is ``True`` the Credit value is prepended with a
    minus as it's moved. When ``negate`` is ``False`` the Credit value
    is moved verbatim. Rows that have both ``Debit`` and ``Credit``
    populated cause the upload to be refused. Returns the rewritten
    CSV bytes and a short summary fragment describing what was changed.
    """
    text = csv_bytes.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise ValueError("CSV is empty (no header row).")
    header = rows[0]
    try:
        debit_idx = header.index("Debit")
        credit_idx = header.index("Credit")
    except ValueError as exc:
        raise ValueError(
            f"CSV header missing required column: {exc}. Header was: {header!r}"
        ) from exc
    rewritten = 0
    for row_index, row in enumerate(rows[1:], start=2):
        if len(row) <= max(debit_idx, credit_idx):
            continue
        debit = row[debit_idx].strip()
        credit = row[credit_idx].strip()
        if not credit:
            continue
        if debit:
            raise ValueError(
                f"Row {row_index} has both Debit ({debit!r}) and Credit ({credit!r}) "
                + "populated; refusing to merge."
            )
        row[debit_idx] = "-" + credit if negate else credit
        row[credit_idx] = ""
        rewritten += 1
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerows(rows)
    suffix = "negated" if negate else "as-is"
    return out.getvalue().encode("utf-8"), f"moved {rewritten} credit row(s) into debit ({suffix})"


def preprocess_cap1_cc(csv_bytes: bytes) -> tuple[bytes, str]:
    """Move every Credit value into Debit with a leading minus.

    Capital One emits two non-negative columns (Debit for charges,
    Credit for payments / refunds), but the importer template only
    points its ``amount`` role at Debit.
    """
    return _merge_credit_into_debit(csv_bytes, negate=True)


def preprocess_citi_cc(csv_bytes: bytes) -> tuple[bytes, str]:
    """Move every Credit value into Debit verbatim.

    Citi emits Debit for charges and Credit (already minus-prefixed)
    for payments / refunds, but the importer template only points its
    ``amount`` role at Debit.
    """
    return _merge_credit_into_debit(csv_bytes, negate=False)


def preprocess_wf_acct(csv_bytes: bytes) -> tuple[bytes, str]:
    """Drop every row whose ``Type`` column is ``Transfer``.

    Wealthfront's cash-account CSV records internal transfers between the
    user's own Wealthfront accounts as ``Type == "Transfer"`` rows. The
    preprocessor removes them so they aren't imported as standalone
    deposits / withdrawals.
    """
    csv_bytes, removed = _drop_rows_where_column_equals(csv_bytes, column="Type", value="Transfer")
    return csv_bytes, f"removed {removed} transfer row(s)"


def preprocess_paypal_acct(csv_bytes: bytes) -> tuple[bytes, str]:
    """Keep only real money movement from a PayPal "Balance affecting" export.

    PayPal's activity downloads share one header, but only the "Balance
    affecting" report reconciles row by row (previous ``Balance`` plus
    ``Total`` equals ``Balance``); the others add authorization rows that
    move no money, so any file that fails this check is refused. Debit
    card hold / hold-reversal pairs are dropped because the settled card
    transaction carries the real amount, and ``General GI/Open wallet
    Transaction`` rows are dropped because they move money between the
    PayPal balance and PayPal Savings. Non-USD rows cause the upload to
    be refused. ``Pending`` rows are kept because PayPal leaves some
    instantly credited bank deposits Pending indefinitely and the balance
    check proves they moved money. Blank ``Name`` cells (e.g. bank
    deposits) are filled from ``Type`` so the imported description isn't
    empty.
    """
    text = csv_bytes.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError("CSV is empty (no header row).")
    header = rows[0]
    missing = [column for column in _PAYPAL_REQUIRED_COLUMNS if column not in header]
    if missing:
        raise ValueError(
            f"CSV header missing required column(s) {missing!r}. Header was: {header!r}"
        )
    index = {column: header.index(column) for column in _PAYPAL_REQUIRED_COLUMNS}

    kept: list[list[str]] = [header]
    previous_balance: Decimal | None = None
    verified = holds = savings = pending = filled = 0
    for row_number, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        if len(row) != len(header):
            raise ValueError(
                f"Row {row_number} has {len(row)} column(s) but the header has {len(header)}."
            )
        currency = row[index["Currency"]].strip()
        if currency != "USD":
            raise ValueError(f"Row {row_number} has Currency {currency!r}; only USD is supported.")
        total = _parse_paypal_amount(row[index["Total"]], row_number=row_number, column="Total")
        balance = _parse_paypal_amount(
            row[index["Balance"]], row_number=row_number, column="Balance"
        )
        if previous_balance is not None:
            if previous_balance + total != balance:
                raise ValueError(
                    f"Row {row_number}: previous Balance {previous_balance} + Total {total} "
                    + f"does not equal Balance {balance}. Only PayPal's 'Balance affecting' "
                    + "activity download reconciles row by row; re-download it with "
                    + "Transaction type set to 'Balance affecting'."
                )
            verified += 1
        previous_balance = balance

        transaction_type = row[index["Type"]].strip()
        if transaction_type in _PAYPAL_HOLD_TYPES:
            holds += 1
            continue
        if transaction_type == _PAYPAL_SAVINGS_TYPE:
            savings += 1
            continue
        if row[index["Status"]].strip() == "Pending":
            pending += 1
        if not row[index["Name"]].strip():
            row[index["Name"]] = transaction_type
            filled += 1
        kept.append(row)

    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerows(kept)
    summary = (
        f"verified running balance on {verified} row(s); dropped {holds} hold/reversal and "
        + f"{savings} GI/Open wallet row(s); kept {pending} Pending row(s); "
        + f"filled {filled} blank Name(s) from Type"
    )
    return out.getvalue().encode("utf-8"), summary


def _parse_paypal_amount(value: str, *, row_number: int, column: str) -> Decimal:
    try:
        return parse_amount(value.strip().replace(",", ""))
    except ValueError as exc:
        raise ValueError(f"Row {row_number} has unparseable {column} {value!r}.") from exc


def _drop_rows_where_column_equals(
    csv_bytes: bytes, *, column: str, value: str
) -> tuple[bytes, int]:
    """Drop every row whose ``column`` (after stripping) equals ``value``.

    Shared helper for ``preprocess_*`` functions that need to filter
    rows by an exact column-value match. Returns the rewritten CSV
    bytes and the number of rows dropped. Raises ``ValueError`` if the
    CSV is empty or missing the requested column.
    """
    text = csv_bytes.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise ValueError("CSV is empty (no header row).")
    header = rows[0]
    try:
        col_idx = header.index(column)
    except ValueError as exc:
        raise ValueError(
            f"CSV header missing required column: {exc}. Header was: {header!r}"
        ) from exc
    kept: list[list[str]] = [header]
    removed = 0
    for row in rows[1:]:
        if len(row) > col_idx and row[col_idx].strip() == value:
            removed += 1
            continue
        kept.append(row)
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerows(kept)
    return out.getvalue().encode("utf-8"), removed
