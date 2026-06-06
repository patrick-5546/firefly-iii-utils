import argparse
import io
import json
import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, field_validator

REPO_ROOT = Path(__file__).resolve().parent
CONFIGS_DIR = REPO_ROOT / "configs"
ACCOUNT_MAPPINGS_PATH = CONFIGS_DIR / "account_mappings.json"

TEMPLATES: dict[str, Path] = {
    "chase_cc": CONFIGS_DIR / "chase_cc.json",
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
    filename_pattern: str
    accounts: dict[str, _CardAccount]

    @field_validator("filename_pattern")
    @classmethod
    def _must_have_capture_group(cls, value: str) -> str:
        if re.compile(value).groups < 1:
            raise ValueError("filename_pattern must contain at least one capture group")
        return value


_AccountMappingsAdapter: TypeAdapter[dict[str, _TemplateMapping]] = TypeAdapter(
    dict[str, _TemplateMapping]
)
_TemplateDictAdapter: TypeAdapter[dict[str, object]] = TypeAdapter(dict[str, object])


class Args(BaseModel):
    csv_path: str
    template: str
    dry_run: bool


def _load_account_mappings() -> dict[str, _TemplateMapping]:
    return _AccountMappingsAdapter.validate_json(ACCOUNT_MAPPINGS_PATH.read_text(encoding="utf-8"))


def _apply_template_overrides(
    template: dict[str, object],
    template_name: str,
    csv_path: Path,
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
    template["default_account"] = account.account_id
    current_tag = template.get("custom_tag", "")
    template["custom_tag"] = f"{current_tag} {account.abbreviation}"
    return (
        f"matched {key!r} -> account id {account.account_id}, abbreviation {account.abbreviation!r}"
    )


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
        default="chase_cc",
        help="Which JSON template under configs/ to use (default: chase_cc).",
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
    template_path = TEMPLATES[args.template]
    if not template_path.is_file():
        parser.error(f"Template file not found: {template_path}")

    _ = load_dotenv()
    importer_url = os.environ["DATA_IMPORTER_URL"].rstrip("/")
    secret = os.environ["AUTO_IMPORT_SECRET"]

    mappings = _load_account_mappings()
    try:
        template_dict = _TemplateDictAdapter.validate_json(
            template_path.read_text(encoding="utf-8")
        )
    except ValidationError as exc:
        parser.error(f"Template {template_path.name} is not a valid JSON object:\n{exc}")
    mapping_summary = _apply_template_overrides(
        template_dict, args.template, csv_path, mappings, parser
    )
    try:
        template = _ImporterTemplate.model_validate(template_dict)
    except ValidationError as exc:
        parser.error(
            f"Template {template_path.name} failed validation after applying overrides "
            + f"for {args.template!r} and {csv_path.name!r}:\n{exc}"
        )
    payload = json.dumps(template_dict).encode("utf-8")

    if args.dry_run:
        with csv_path.open(encoding="utf-8", newline="") as csv_file:
            row_count = max(sum(1 for _ in csv_file) - 1, 0)
        account_label = str(template.default_account)
        name = _lookup_account_name(template.default_account)
        if name is not None:
            account_label = f"{template.default_account} ({name})"
        print("[dry run] No request will be made. Would POST:")
        print(f"  URL:        {importer_url}/autoupload")
        print(f"  secret:     <{len(secret)} chars>")
        print(f"  json:       {template_path} ({len(payload)} bytes, mutated)")
        if mapping_summary is not None:
            print(f"    mapping:         {mapping_summary}")
        print(f"    default_account: {account_label}")
        print(f"    custom_tag:      {template.custom_tag!r}")
        print(f"    roles:           {template.roles}")
        print(f"  importable: {csv_path} ({csv_path.stat().st_size} bytes, {row_count} data rows)")
        return

    with csv_path.open("rb") as csv_file:
        response = requests.post(
            f"{importer_url}/autoupload",
            data={"secret": secret},
            files={
                "json": (template_path.name, io.BytesIO(payload), "application/json"),
                "importable": (csv_path.name, csv_file, "text/csv"),
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
