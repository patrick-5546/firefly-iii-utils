import argparse
import io
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv
from pydantic import ValidationError
from rich.console import Console

from .api import lookup_account_name
from .mappings import (
    apply_template_overrides,
    detect_template,
    load_account_mappings,
)
from .models import Args, CardAccount, ImporterTemplate, TemplateDictAdapter
from .output import (
    Counts,
    make_console,
    make_progress,
    print_aggregate,
    print_header,
    print_response,
)
from .paths import TEMPLATES


def _process_one(
    csv_path: Path,
    template_arg: str | None,
    mappings: dict[str, dict[str, CardAccount]],
    importer_url: str,
    secret: str,
    dry_run: bool,
    parser: argparse.ArgumentParser,
    console: Console,
) -> Counts | None:
    csv_bytes = csv_path.read_bytes()

    auto_detected = False
    if template_arg is None:
        matches = detect_template(csv_path, csv_bytes)
        known = ", ".join(sorted(TEMPLATES)) or "<none>"
        if not matches:
            parser.error(
                f"Could not auto-detect a template for {csv_path.name!r}: no template's "
                + "filename_pattern or csv_column_header on its TemplateInfo in "
                + "src/firefly_iii_utils/paths.py matched. "
                + f"Re-run with -t/--template (known templates: {known})."
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
        template_name = template_arg

    template_path = TEMPLATES[template_name].path
    if not template_path.is_file():
        parser.error(f"Template file not found: {template_path}")

    try:
        template_dict = TemplateDictAdapter.validate_json(template_path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        parser.error(f"Template {template_path.name} is not a valid JSON object:\n{exc}")
    mapping_summary = apply_template_overrides(
        template_dict, template_name, csv_path, csv_bytes, mappings, parser
    )
    try:
        template = ImporterTemplate.model_validate(template_dict)
    except ValidationError as exc:
        parser.error(
            f"Template {template_path.name} failed validation after applying overrides "
            + f"for {template_name!r} and {csv_path.name!r}:\n{exc}"
        )
    payload = json.dumps(template_dict).encode("utf-8")

    preprocessor = TEMPLATES[template_name].preprocessor
    preprocessing_summary: str | None = None
    if preprocessor is not None:
        try:
            csv_bytes, summary = preprocessor(csv_bytes)
        except ValueError as exc:
            parser.error(f"Preprocessing {csv_path.name} for {template_name!r} failed: {exc}")
        preprocessing_summary = f"{template_name} {summary}"

    template_label = f"{template_name}{' (auto-detected)' if auto_detected else ''}"

    if dry_run:
        with io.BytesIO(csv_bytes) as buf:
            row_count = max(sum(1 for _ in buf) - 1, 0)
        account_label = str(template.default_account)
        name = lookup_account_name(template.default_account)
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
        return None

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

    counts: Counts | None = None
    try:
        body_json = json.dumps(response.json())
    except ValueError:
        counts = print_response(console, response.text)
    else:
        console.print_json(body_json)
    response.raise_for_status()
    return counts


def _collect_csv_files(directory: Path, parser: argparse.ArgumentParser) -> list[Path]:
    files = sorted(
        child for child in directory.iterdir() if child.is_file() and child.suffix.lower() == ".csv"
    )
    if not files:
        parser.error(f"No .csv files found in directory: {directory}")
    return files


def main():
    parser = argparse.ArgumentParser(
        description="Upload a bank CSV to the Firefly III Data Importer's /autoupload endpoint.",
    )
    _ = parser.add_argument(
        "path",
        help="Path to a bank CSV file, or a directory of CSV files to import.",
    )
    _ = parser.add_argument(
        "-t",
        "--template",
        choices=sorted(TEMPLATES),
        help=(
            "Which JSON template under configs/ to use. If omitted, the template is "
            "auto-detected from the CSV using the detection rules in TEMPLATES in "
            "src/firefly_iii_utils/paths.py. Not allowed when path is a directory."
        ),
    )
    _ = parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Validate inputs and print what would be sent without making the request.",
    )
    _ = parser.add_argument(
        "-N",
        "--no-color",
        action="store_true",
        help=(
            "Disable colored output. Colors are also disabled automatically when stdout "
            "is not a terminal or when the NO_COLOR environment variable is set."
        ),
    )
    args = Args.model_validate(vars(parser.parse_args()))

    input_path = Path(args.path)
    if input_path.is_file():
        csv_files = [input_path]
    elif input_path.is_dir():
        if args.template is not None:
            parser.error(
                "-t/--template is not allowed when path is a directory; "
                + "templates are auto-detected per file."
            )
        csv_files = _collect_csv_files(input_path, parser)
    else:
        parser.error(f"Path not found: {input_path}")

    mappings = load_account_mappings()

    _ = load_dotenv()
    importer_url = os.environ["DATA_IMPORTER_URL"].rstrip("/")
    secret = os.environ["AUTO_IMPORT_SECRET"]

    console = make_console(args.no_color)

    show_progress = input_path.is_dir() and not args.dry_run
    totals = Counts()
    processed_files = 0

    progress = make_progress(console, disable=not show_progress)
    with progress:
        task = progress.add_task("Importing", total=len(csv_files))
        for csv_path in csv_files:
            progress.update(task, description=f"Importing {csv_path.name}")
            print_header(console, csv_path.name)
            per_file = _process_one(
                csv_path,
                args.template,
                mappings,
                importer_url,
                secret,
                args.dry_run,
                parser,
                console,
            )
            processed_files += 1
            if per_file is not None:
                totals += per_file
            progress.advance(task)

    if show_progress and processed_files > 0:
        print_aggregate(console, processed_files, totals)


if __name__ == "__main__":
    main()
