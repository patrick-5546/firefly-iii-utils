import argparse
import csv
import io
import re
from pathlib import Path

from .models import (
    AccountMappingsAdapter,
    CardAccount,
    TemplateDetectionAdapter,
    TemplateDetectionRule,
)
from .paths import ACCOUNT_MAPPINGS_PATH, TEMPLATE_DETECTION_PATH, TEMPLATES


def load_account_mappings() -> dict[str, dict[str, CardAccount]]:
    return AccountMappingsAdapter.validate_json(ACCOUNT_MAPPINGS_PATH.read_text(encoding="utf-8"))


def load_template_detection() -> dict[str, TemplateDetectionRule]:
    return TemplateDetectionAdapter.validate_json(
        TEMPLATE_DETECTION_PATH.read_text(encoding="utf-8")
    )


def detect_template(
    csv_path: Path,
    csv_bytes: bytes,
    detection_rules: dict[str, TemplateDetectionRule],
) -> list[str]:
    """Return all template names whose detection rule matches the CSV.

    Iterates the templates registered in ``TEMPLATES`` and, for each one
    that has a rule in ``configs/template_detection.json``, checks
    whether its ``filename_pattern`` matches the CSV filename or its
    ``csv_column_header`` is present in the CSV's header row. Templates
    without a detection rule are silently skipped.
    """
    header: list[str] | None = None
    matches: list[str] = []
    for name in TEMPLATES:
        rule = detection_rules.get(name)
        if rule is None:
            continue
        if rule.filename_pattern is not None:
            if re.search(rule.filename_pattern, csv_path.name) is not None:
                matches.append(name)
            continue
        assert rule.csv_column_header is not None
        if header is None:
            reader = csv.reader(io.StringIO(csv_bytes.decode("utf-8-sig")))
            empty: list[str] = []
            header = next(reader, empty)
        if rule.csv_column_header in header:
            matches.append(name)
    return matches


def _resolve_account_from_filename(
    filename_pattern: str,
    accounts: dict[str, CardAccount],
    csv_path: Path,
    template_name: str,
    parser: argparse.ArgumentParser,
) -> tuple[CardAccount, str]:
    match = re.search(filename_pattern, csv_path.name)
    if match is None:
        parser.error(
            f"CSV filename {csv_path.name!r} does not match the filename_pattern "
            + f"{filename_pattern!r} configured for template {template_name!r} in "
            + "configs/template_detection.json"
        )
    key = match.group(1)
    account = accounts.get(key)
    if account is None:
        known = ", ".join(sorted(accounts)) or "<none>"
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
    csv_column_header: str,
    accounts: dict[str, CardAccount],
    csv_path: Path,
    csv_bytes: bytes,
    template_name: str,
    parser: argparse.ArgumentParser,
) -> tuple[CardAccount, str]:
    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig")))
    fieldnames = reader.fieldnames
    if fieldnames is None or csv_column_header not in fieldnames:
        available = ", ".join(fieldnames or []) or "<none>"
        parser.error(
            f"CSV {csv_path.name!r} has no column named {csv_column_header!r} (configured "
            + f"for template {template_name!r} in configs/template_detection.json; "
            + f"available columns: {available})"
        )
    seen: dict[str, CardAccount] = {}
    for row_index, row in enumerate(reader, start=2):
        raw = row.get(csv_column_header)
        key = raw.strip() if raw is not None else ""
        if not key or key in seen:
            continue
        account = accounts.get(key)
        if account is None:
            known = ", ".join(sorted(accounts)) or "<none>"
            parser.error(
                f"CSV {csv_path.name!r} row {row_index} has {csv_column_header} = {key!r}, "
                + f"but template {template_name!r} has no entry for that key in "
                + f"configs/account_mappings.json (known keys: {known})"
            )
        seen[key] = account
    if not seen:
        parser.error(
            f"CSV {csv_path.name!r} has no data rows with a {csv_column_header!r} value; "
            + "cannot resolve a Firefly III account."
        )
    account_ids = {a.account_id for a in seen.values()}
    if len(account_ids) > 1:
        details = ", ".join(f"{k!r} -> {a.account_id}" for k, a in sorted(seen.items()))
        parser.error(
            f"CSV {csv_path.name!r} maps to multiple Firefly III accounts via column "
            + f"{csv_column_header!r}: {details}. Refusing to upload; split the file by account."
        )
    chosen = next(iter(seen.values()))
    keys_repr = ", ".join(sorted(seen))
    summary = (
        f"csv column {csv_column_header!r} keys [{keys_repr}] -> account id "
        + f"{chosen.account_id}, abbreviation {chosen.abbreviation!r}"
    )
    return chosen, summary


def apply_template_overrides(
    template: dict[str, object],
    template_name: str,
    csv_path: Path,
    csv_bytes: bytes,
    mappings: dict[str, dict[str, CardAccount]],
    detection_rules: dict[str, TemplateDetectionRule],
    parser: argparse.ArgumentParser,
) -> str | None:
    """Apply mapping overrides to ``template`` in place.

    Returns a short human-readable description of the rule that matched,
    or ``None`` if no per-account mapping is configured for this template.
    """
    accounts = mappings.get(template_name)
    if accounts is None:
        return None
    rule = detection_rules.get(template_name)
    if rule is None:
        parser.error(
            f"Template {template_name!r} has per-account mappings in "
            + "configs/account_mappings.json but no detection rule in "
            + "configs/template_detection.json; cannot resolve account."
        )
    if rule.filename_pattern is not None:
        account, summary = _resolve_account_from_filename(
            rule.filename_pattern, accounts, csv_path, template_name, parser
        )
    else:
        assert rule.csv_column_header is not None
        account, summary = _resolve_account_from_csv(
            rule.csv_column_header, accounts, csv_path, csv_bytes, template_name, parser
        )
    template["default_account"] = account.account_id
    if account.abbreviation:
        current_tag = template.get("custom_tag", "")
        template["custom_tag"] = f"{current_tag} {account.abbreviation}"
    return summary
