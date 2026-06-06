import argparse
import csv
import io
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Self

import requests
from dotenv import load_dotenv
from pydantic import (
    BaseModel,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

REPO_ROOT = Path(__file__).resolve().parent
CONFIGS_DIR = REPO_ROOT / "configs"
ACCOUNT_MAPPINGS_PATH = CONFIGS_DIR / "account_mappings.json"

TEMPLATES: dict[str, Path] = {
    "chase_cc": CONFIGS_DIR / "chase_cc.json",
    "cap1_cc": CONFIGS_DIR / "cap1_cc.json",
}


class _AccountAttrs(BaseModel):
    name: str


class _AccountData(BaseModel):
    attributes: _AccountAttrs


class _AccountResponse(BaseModel):
    data: _AccountData


class _ImporterTemplate(BaseModel):
    default_account: int = Field(ge=1)
    custom_tag: str
    roles: list[str]


class _CardAccount(BaseModel):
    account_id: int = Field(ge=1)
    abbreviation: str


class _TemplateMapping(BaseModel):
    filename_pattern: str | None = None
    csv_column_header: str | None = None
    accounts: dict[str, _CardAccount]

    @field_validator("filename_pattern")
    @classmethod
    def _must_have_capture_group(cls, value: str | None) -> str | None:
        if value is not None and re.compile(value).groups < 1:
            raise ValueError("filename_pattern must contain at least one capture group")
        return value

    @model_validator(mode="after")
    def _exactly_one_lookup_source(self) -> Self:
        has_filename = self.filename_pattern is not None
        has_csv_column = self.csv_column_header is not None
        if has_filename == has_csv_column:
            raise ValueError("exactly one of filename_pattern or csv_column_header must be set")
        return self


_AccountMappingsAdapter: TypeAdapter[dict[str, _TemplateMapping]] = TypeAdapter(
    dict[str, _TemplateMapping]
)
_TemplateDictAdapter: TypeAdapter[dict[str, object]] = TypeAdapter(dict[str, object])


class Args(BaseModel):
    csv_path: str
    template: str | None
    dry_run: bool


def _load_account_mappings() -> dict[str, _TemplateMapping]:
    return _AccountMappingsAdapter.validate_json(ACCOUNT_MAPPINGS_PATH.read_text(encoding="utf-8"))


def _detect_template(
    csv_path: Path,
    csv_bytes: bytes,
    mappings: dict[str, _TemplateMapping],
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
    mapping: _TemplateMapping,
    csv_path: Path,
    template_name: str,
    parser: argparse.ArgumentParser,
) -> tuple[_CardAccount, str]:
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
    mapping: _TemplateMapping,
    csv_path: Path,
    csv_bytes: bytes,
    template_name: str,
    parser: argparse.ArgumentParser,
) -> tuple[_CardAccount, str]:
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
    seen: dict[str, _CardAccount] = {}
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


def _apply_template_overrides(
    template: dict[str, object],
    template_name: str,
    csv_path: Path,
    csv_bytes: bytes,
    mappings: dict[str, _TemplateMapping],
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


def _preprocess_cap1_cc(csv_bytes: bytes) -> tuple[bytes, int]:
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


PREPROCESSORS: dict[str, Callable[[bytes], tuple[bytes, int]]] = {
    "cap1_cc": _preprocess_cap1_cc,
}


def _lookup_account_name(account_id: int) -> str | None:
    """Best-effort lookup of a Firefly III asset account's display name."""
    try:
        url = os.environ["FIREFLY_III_URL"].rstrip("/")
        token = os.environ["FIREFLY_III_PAT"]
    except KeyError:
        return None
    try:
        response = requests.get(
            f"{url}/api/v1/accounts/{account_id}",
            headers={
                "accept": "application/vnd.api+json",
                "Authorization": f"Bearer {token}",
            },
            timeout=10,
        )
        response.raise_for_status()
        return _AccountResponse.model_validate(response.json()).data.attributes.name
    except (requests.RequestException, ValidationError):
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Upload a bank CSV to the Firefly III Data Importer's /autoupload endpoint.",
    )
    _ = parser.add_argument("csv_path", help="Path to the bank CSV to import.")
    _ = parser.add_argument(
        "-t",
        "--template",
        choices=sorted(TEMPLATES),
        help=(
            "Which JSON template under configs/ to use. If omitted, the template is "
            "auto-detected from the CSV using the rules in configs/account_mappings.json."
        ),
    )
    _ = parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Validate inputs and print what would be sent without making the request.",
    )
    args = Args.model_validate(vars(parser.parse_args()))

    csv_path = Path(args.csv_path)
    if not csv_path.is_file():
        parser.error(f"CSV file not found: {csv_path}")

    csv_bytes = csv_path.read_bytes()
    mappings = _load_account_mappings()

    auto_detected = False
    if args.template is None:
        matches = _detect_template(csv_path, csv_bytes, mappings)
        known = ", ".join(sorted(TEMPLATES)) or "<none>"
        if not matches:
            parser.error(
                f"Could not auto-detect a template for {csv_path.name!r}: no template's "
                + "filename_pattern or csv_column_header in configs/account_mappings.json "
                + f"matched. Re-run with -t/--template (known templates: {known})."
            )
        if len(matches) > 1:
            candidates = ", ".join(matches)
            parser.error(
                f"Auto-detection for {csv_path.name!r} is ambiguous: matched {candidates}. "
                + "Re-run with -t/--template to pick one."
            )
        template_name = matches[0]
        auto_detected = True
    else:
        template_name = args.template

    template_path = TEMPLATES[template_name]
    if not template_path.is_file():
        parser.error(f"Template file not found: {template_path}")

    _ = load_dotenv()
    importer_url = os.environ["DATA_IMPORTER_URL"].rstrip("/")
    secret = os.environ["AUTO_IMPORT_SECRET"]

    try:
        template_dict = _TemplateDictAdapter.validate_json(
            template_path.read_text(encoding="utf-8")
        )
    except ValidationError as exc:
        parser.error(f"Template {template_path.name} is not a valid JSON object:\n{exc}")
    mapping_summary = _apply_template_overrides(
        template_dict, template_name, csv_path, csv_bytes, mappings, parser
    )
    try:
        template = _ImporterTemplate.model_validate(template_dict)
    except ValidationError as exc:
        parser.error(
            f"Template {template_path.name} failed validation after applying overrides "
            + f"for {template_name!r} and {csv_path.name!r}:\n{exc}"
        )
    payload = json.dumps(template_dict).encode("utf-8")

    preprocessor = PREPROCESSORS.get(template_name)
    preprocessing_summary: str | None = None
    if preprocessor is not None:
        try:
            csv_bytes, rewritten = preprocessor(csv_bytes)
        except ValueError as exc:
            parser.error(f"Preprocessing {csv_path.name} for {template_name!r} failed: {exc}")
        preprocessing_summary = (
            f"{template_name} moved {rewritten} credit row(s) into debit (negated)"
        )

    template_label = f"{template_name}{' (auto-detected)' if auto_detected else ''}"

    if args.dry_run:
        with io.BytesIO(csv_bytes) as buf:
            row_count = max(sum(1 for _ in buf) - 1, 0)
        account_label = str(template.default_account)
        name = _lookup_account_name(template.default_account)
        if name is not None:
            account_label = f"{template.default_account} ({name})"
        print("[dry run] No request will be made. Would POST:")
        print(f"  template:   {template_label}")
        print(f"  URL:        {importer_url}/autoupload")
        print(f"  secret:     <{len(secret)} chars>")
        print(f"  json:       {template_path} ({len(payload)} bytes, mutated)")
        if mapping_summary is not None:
            print(f"    mapping:         {mapping_summary}")
        print(f"    default_account: {account_label}")
        print(f"    custom_tag:      {template.custom_tag!r}")
        print(f"    roles:           {template.roles}")
        size_label = f"{len(csv_bytes)} bytes"
        if preprocessor is not None:
            size_label += f", preprocessed from {csv_path.stat().st_size} on-disk bytes"
        print(f"  importable: {csv_path} ({size_label}, {row_count} data rows)")
        if preprocessing_summary is not None:
            print(f"    preprocessing:   {preprocessing_summary}")
        return

    print(f"template: {template_label}")
    response = requests.post(
        f"{importer_url}/autoupload",
        data={"secret": secret},
        files={
            "json": (template_path.name, io.BytesIO(payload), "application/json"),
            "importable": (csv_path.name, io.BytesIO(csv_bytes), "text/csv"),
        },
        timeout=120,
    )

    try:
        print(json.dumps(response.json(), indent=2))
    except ValueError:
        print(response.text)
    response.raise_for_status()


if __name__ == "__main__":
    main()
