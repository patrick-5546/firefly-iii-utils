import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

REPO_ROOT = Path(__file__).resolve().parent
CONFIGS_DIR = REPO_ROOT / "configs"

TEMPLATES: dict[str, Path] = {
    "chase_fu": CONFIGS_DIR / "chase_fu.json",
}


class _AccountAttrs(BaseModel):
    name: str


class _AccountData(BaseModel):
    attributes: _AccountAttrs


class _AccountResponse(BaseModel):
    data: _AccountData


class _ImporterTemplate(BaseModel):
    default_account: int
    custom_tag: str
    roles: list[str]


class Args(BaseModel):
    csv_path: str
    template: str
    dry_run: bool


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


def _print_response(response: requests.Response) -> None:
    """Print the importer's response in the most readable form available."""
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            print(json.dumps(response.json(), indent=2))
            return
        except ValueError:
            pass
    if "text/html" in content_type:
        body = response.text
        title_match = re.search(r"<title>([^<]+)</title>", body)
        error_match = re.search(r'<p class="text-danger">\s*(.*?)\s*</p>', body, flags=re.DOTALL)
        print(f"HTTP {response.status_code} from {response.url}", file=sys.stderr)
        if title_match:
            print(f"  title: {title_match.group(1).strip()}", file=sys.stderr)
        if error_match:
            print(f"  error: {error_match.group(1).strip()}", file=sys.stderr)
        return
    print(response.text)


def main():
    parser = argparse.ArgumentParser(
        description="Upload a bank CSV to the Firefly III Data Importer's /autoupload endpoint.",
    )
    _ = parser.add_argument("csv_path", help="Path to the bank CSV to import.")
    _ = parser.add_argument(
        "-t",
        "--template",
        choices=sorted(TEMPLATES),
        default="chase_fu",
        help="Which JSON template under configs/ to use (default: chase_fu).",
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

    if args.dry_run:
        with csv_path.open(encoding="utf-8", newline="") as csv_file:
            row_count = max(sum(1 for _ in csv_file) - 1, 0)
        template = _ImporterTemplate.model_validate_json(template_path.read_text(encoding="utf-8"))
        account_label = str(template.default_account)
        name = _lookup_account_name(template.default_account)
        if name is not None:
            account_label = f"{template.default_account} ({name})"
        print("[dry run] No request will be made. Would POST:")
        print(f"  URL:        {importer_url}/autoupload")
        print(f"  secret:     <{len(secret)} chars>")
        print(f"  json:       {template_path} ({template_path.stat().st_size} bytes)")
        print(f"    default_account: {account_label}")
        print(f"    custom_tag:      {template.custom_tag!r}")
        print(f"    roles:           {template.roles}")
        print(f"  importable: {csv_path} ({csv_path.stat().st_size} bytes, {row_count} data rows)")
        return

    with csv_path.open("rb") as csv_file, template_path.open("rb") as json_file:
        response = requests.post(
            f"{importer_url}/autoupload",
            data={"secret": secret},
            files={
                "json": (template_path.name, json_file, "application/json"),
                "importable": (csv_path.name, csv_file, "text/csv"),
            },
            timeout=120,
        )

    try:
        _print_response(response)
    finally:
        response.raise_for_status()


if __name__ == "__main__":
    main()
