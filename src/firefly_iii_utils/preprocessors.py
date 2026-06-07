import csv
import io


def preprocess_cap1_cc(csv_bytes: bytes) -> tuple[bytes, int]:
    """Move every Credit value into Debit with a leading minus.

    Capital One uses two positive columns (Debit for charges, Credit for
    payments / refunds) but the importer template only points its ``amount``
    role at Debit. Negating while merging keeps charges and payments on
    opposite signs after the move. Returns the rewritten CSV bytes and the
    number of rows whose Credit was moved.
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
        row[debit_idx] = "-" + credit
        row[credit_idx] = ""
        rewritten += 1
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8"), rewritten
