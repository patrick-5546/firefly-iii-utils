import argparse
import csv
import io
import re
from pathlib import Path

from .models import AccountMappingsAdapter, CardAccount, TemplateMapping
from .paths import ACCOUNT_MAPPINGS_PATH, TEMPLATES


def load_account_mappings() -> dict[str, TemplateMapping]:
    return AccountMappingsAdapter.validate_json(ACCOUNT_MAPPINGS_PATH.read_text(encoding="utf-8"))


def detect_template(
    csv_path: Path,
    csv_bytes: bytes,
    mappings: dict[str, TemplateMapping],
) -> list[str]:
    """Return all template names whose mapping rule matches the CSV.

    Reuses the per-template ``filename_pattern`` / ``csv_column_header``
    rules from ``configs/account_mappings.json``: a template matches when
    its filename pattern is found in ``csv_path.name`` or when its
    configured column header is present in the CSV's header row.
    Templates without a mapping entry cannot be auto-detected and are
    skipped.
    """
    header: list[str] | None = None
    matches: list[str] = []
    for name in TEMPLATES:
        mapping = mappings.get(name)
        if mapping is None:
            continue
        if mapping.filename_pattern is not None:
            if re.search(mapping.filename_pattern, csv_path.name) is not None:
                matches.append(name)
            continue
        assert mapping.csv_column_header is not None
        if header is None:
            reader = csv.reader(io.StringIO(csv_bytes.decode("utf-8-sig")))
            empty: list[str] = []
            header = next(reader, empty)
        if mapping.csv_column_header in header:
            matches.append(name)
    return matches


def _resolve_account_from_filename(
    mapping: TemplateMapping,
    csv_path: Path,
    template_name: str,
    parser: argparse.ArgumentParser,
) -> tuple[CardAccount, str]:
    assert mapping.filename_pattern is not None
    match = re.search(mapping.filename_pattern, csv_path.name)
    if match is None:
        parser.error(
            f"CSV filename {csv_path.name!r} does not match the filename_pattern "
            + f"{mapping.filename_pattern!r} configured for template "
            + f"{template_name!r} in configs/account_mappings.json"
        )
    key = match.group(1)
    account = mapping.accounts.get(key)
    if account is None:
        known = ", ".join(sorted(mapping.accounts)) or "<none>"
        parser.error(
            f"CSV filename matched {key!r} but template {template_name!r} has no entry "
            + f"for that key in configs/account_mappings.json (known keys: {known})"
        )
    summary = (
        f"filename matched {key!r} -> account id {account.account_id}, "
        + f"abbreviation {account.abbreviation!r}"
    )
    return account, summary


def _resolve_account_from_csv(
    mapping: TemplateMapping,
    csv_path: Path,
    csv_bytes: bytes,
    template_name: str,
    parser: argparse.ArgumentParser,
) -> tuple[CardAccount, str]:
    assert mapping.csv_column_header is not None
    header_name = mapping.csv_column_header
    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig")))
    fieldnames = reader.fieldnames
    if fieldnames is None or header_name not in fieldnames:
        available = ", ".join(fieldnames or []) or "<none>"
        parser.error(
            f"CSV {csv_path.name!r} has no column named {header_name!r} (configured for "
            + f"template {template_name!r} in configs/account_mappings.json; "
            + f"available columns: {available})"
        )
    seen: dict[str, CardAccount] = {}
    for row_index, row in enumerate(reader, start=2):
        raw = row.get(header_name)
        key = raw.strip() if raw is not None else ""
        if not key or key in seen:
            continue
        account = mapping.accounts.get(key)
        if account is None:
            known = ", ".join(sorted(mapping.accounts)) or "<none>"
            parser.error(
                f"CSV {csv_path.name!r} row {row_index} has {header_name} = {key!r}, but "
                + f"template {template_name!r} has no entry for that key in "
                + f"configs/account_mappings.json (known keys: {known})"
            )
        seen[key] = account
    if not seen:
        parser.error(
            f"CSV {csv_path.name!r} has no data rows with a {header_name!r} value; "
            + "cannot resolve a Firefly III account."
        )
    account_ids = {a.account_id for a in seen.values()}
    if len(account_ids) > 1:
        details = ", ".join(f"{k!r} -> {a.account_id}" for k, a in sorted(seen.items()))
        parser.error(
            f"CSV {csv_path.name!r} maps to multiple Firefly III accounts via column "
            + f"{header_name!r}: {details}. Refusing to upload; split the file by account."
        )
    chosen = next(iter(seen.values()))
    keys_repr = ", ".join(sorted(seen))
    summary = (
        f"csv column {header_name!r} keys [{keys_repr}] -> account id "
        + f"{chosen.account_id}, abbreviation {chosen.abbreviation!r}"
    )
    return chosen, summary


def apply_template_overrides(
    template: dict[str, object],
    template_name: str,
    csv_path: Path,
    csv_bytes: bytes,
    mappings: dict[str, TemplateMapping],
    parser: argparse.ArgumentParser,
) -> str | None:
    """Apply mapping overrides to ``template`` in place.

    Returns a short human-readable description of the rule that matched,
    or ``None`` if no mapping is configured for this template.
    """
    mapping = mappings.get(template_name)
    if mapping is None:
        return None
    if mapping.filename_pattern is not None:
        account, summary = _resolve_account_from_filename(mapping, csv_path, template_name, parser)
    else:
        account, summary = _resolve_account_from_csv(
            mapping, csv_path, csv_bytes, template_name, parser
        )
    template["default_account"] = account.account_id
    current_tag = template.get("custom_tag", "")
    template["custom_tag"] = f"{current_tag} {account.abbreviation}"
    return summary
