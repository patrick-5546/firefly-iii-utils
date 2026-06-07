import csv
import io


def _merge_credit_into_debit(csv_bytes: bytes, *, negate: bool) -> tuple[bytes, str]:
    """Move every Credit value into Debit, dropping the Credit cell.

    Shared logic for banks that split their amount across two columns
    (``Debit`` for charges, ``Credit`` for payments / refunds) while
    the importer template only points its ``amount`` role at ``Debit``.

    When ``negate`` is ``True`` (e.g. Capital One) the Credit value is
    moved with a leading minus sign because Credit is positive on the
    source side. When ``negate`` is ``False`` (e.g. Citi) the Credit
    value is moved as-is because the source already negates it. Rows
    that have both ``Debit`` and ``Credit`` populated cause the upload
    to be refused. Returns the rewritten CSV bytes and a short summary
    fragment describing what was changed.
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
    suffix = "negated" if negate else "preserving sign"
    return out.getvalue().encode("utf-8"), f"moved {rewritten} credit row(s) into debit ({suffix})"


def preprocess_cap1_cc(csv_bytes: bytes) -> tuple[bytes, str]:
    """Move every Credit value into Debit with a leading minus.

    Capital One uses two positive columns (Debit for charges, Credit for
    payments / refunds) but the importer template only points its ``amount``
    role at Debit. Negating while merging keeps charges and payments on
    opposite signs after the move. Returns the rewritten CSV bytes and a
    short summary fragment describing what was changed.
    """
    return _merge_credit_into_debit(csv_bytes, negate=True)


def preprocess_citi_cc(csv_bytes: bytes) -> tuple[bytes, str]:
    """Move every Credit value into Debit, preserving its (already negative) sign.

    Citi puts charges in Debit (positive) and payments / refunds in Credit
    (already negative), but the importer template only points its ``amount``
    role at Debit. Moving the value as-is keeps the sign that the source
    already supplied. Returns the rewritten CSV bytes and a short summary
    fragment describing what was changed.
    """
    return _merge_credit_into_debit(csv_bytes, negate=False)


def preprocess_wf_acct(csv_bytes: bytes) -> tuple[bytes, str]:
    """Drop every row whose ``Type`` column is ``Transfer``.

    Wealthfront's cash-account CSV records internal transfers between the
    user's own Wealthfront accounts as ``Type == "Transfer"`` rows. The
    preprocessor removes them so they aren't imported as standalone
    deposits / withdrawals. Returns the rewritten CSV bytes and a short
    summary fragment describing how many rows were dropped.
    """
    csv_bytes, removed = _drop_rows_where_column_equals(csv_bytes, column="Type", value="Transfer")
    return csv_bytes, f"removed {removed} transfer row(s)"


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
