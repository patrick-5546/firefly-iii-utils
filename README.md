# finances

Scripts for working with my [Firefly III](https://www.firefly-iii.org/)
instance.

## Setup

### Data Importer setup

The `/autoupload` endpoint on the
[Firefly III Data Importer](https://github.com/firefly-iii/data-importer)
is disabled by default. On the importer instance, set:

```env
CAN_POST_FILES=true
AUTO_IMPORT_SECRET=<at least 16 characters>
```

Generate a secret with, for example:

```sh
openssl rand -hex 24
```

Restart the importer so the new env vars take effect, then put the same
URL and secret in this repo's `.env` as `DATA_IMPORTER_URL` and
`AUTO_IMPORT_SECRET`.

### Environment variables

Required environment variables (place in `.env`, which is gitignored):

| Variable             | Used by         | Purpose                                                                  |
| -------------------- | --------------- | ------------------------------------------------------------------------ |
| `FIREFLY_III_URL`    | `query.py`      | Base URL of the Firefly III instance (e.g. `https://firefly.example`).   |
| `FIREFLY_III_PAT`    | `query.py`      | Personal Access Token for Firefly III's API.                             |
| `DATA_IMPORTER_URL`  | `import_csv.py` | Base URL of the Firefly III **Data Importer** instance (a separate URL). |
| `AUTO_IMPORT_SECRET` | `import_csv.py` | Shared secret matching the importer's `AUTO_IMPORT_SECRET` env var.      |

## Usage

### `query.py` — quick Firefly III API queries

```sh
uv run python query.py about
uv run python query.py accounts
```

### `import_csv.py` — upload a bank CSV via the Data Importer

Uploads a CSV plus a JSON template from `configs/` to the importer's
`/autoupload` endpoint, replicating the manual file-upload wizard.

```sh
# Template is auto-detected from configs/account_mappings.json
# (filename pattern or CSV column header):
uv run python import_csv.py path/to/Chase1234_Activity.CSV
uv run python import_csv.py path/to/2026-06-06_transaction_download.csv

# Explicit template (overrides auto-detection; must match a key in
# TEMPLATES inside import_csv.py):
uv run python import_csv.py --template chase_cc path/to/Chase1234_Activity.CSV

# Dry run: validate inputs and print what would be sent, without making the request:
uv run python import_csv.py --dry-run path/to/Chase1234_Activity.CSV
```

Auto-detection iterates the templates registered in
`import_csv.py`'s `TEMPLATES` dict and, for each one that has an entry
in `configs/account_mappings.json`, checks whether its
`filename_pattern` matches the CSV filename or its `csv_column_header`
is present in the CSV's header row. If zero templates match — or more
than one — the script errors out and asks you to pass `-t/--template`.

#### Per-account overrides (`configs/account_mappings.json`)

A single importer template can be shared across multiple accounts at the
same bank (e.g. several Chase credit cards using one CSV format). The
mapping file `configs/account_mappings.json` (gitignored, since account
ids are private) lets the script pick the right Firefly III account id
and tag suffix.

Each template entry uses **exactly one** of two lookup sources:

- `filename_pattern` — a regex with one capture group, applied to the CSV
  filename. The captured value is the lookup key into `accounts`. Used by
  `chase_cc`, where the filename embeds the last 4 digits of the card.
- `csv_column_header` — the header name of a column in the CSV body. Every
  data row's value in that column is treated as a lookup key into `accounts`.
  All rows must resolve to the **same** Firefly III `account_id`, otherwise
  the script refuses to upload (this guards against mixed-account exports).
  Used by `cap1_cc`, where the filename has no identifier but the `Card No.`
  column does — and multiple card numbers may legitimately point to the
  same account (e.g. a primary card plus an authorized user).

Schema:

```json
{
  "<template_name>": {
    "filename_pattern": "Bank(\\d{4})_",
    "accounts": {
      "<lookup_key>": { "account_id": 1, "abbreviation": "aa" }
    }
  },
  "<other_template>": {
    "csv_column_header": "Card No.",
    "accounts": {
      "<lookup_key>": { "account_id": 2, "abbreviation": "bb" }
    }
  }
}
```

For each upload, when the selected template has an entry in this file:

1. The configured lookup source is resolved to a key (or set of keys, for
   `csv_column_header`).
2. The matching entry's `account_id` overrides `default_account` in the
   template before it is sent to the importer.
3. The entry's `abbreviation` is appended to `custom_tag`, so a base
   tag of `"%datetime%: <template_name>"` becomes
   `"%datetime%: <template_name> <abbreviation>"`.

If the lookup fails — filename doesn't match, captured key isn't in
`accounts`, a CSV row has an unknown key, multiple rows disagree on the
account, or the post-override `default_account` is still `< 1` — the
script refuses to upload and prints an error explaining what went wrong.

#### Per-template CSV preprocessing

Some banks emit CSVs that don't match the importer template's column
shape and need a small transformation before upload. `import_csv.py`
keeps a small registry of preprocessors keyed by template name; when one
is registered, the CSV is parsed, rewritten in memory, and the
transformed bytes are uploaded (the original file on disk is never
modified).

Currently registered:

- **`cap1_cc`** — Capital One puts charges in `Debit` (positive) and
  payments / refunds in `Credit` (positive), but the importer template
  only points its `amount` role at `Debit`. For every row with a value
  in `Credit`, the preprocessor moves it into `Debit` with a leading
  minus sign, so charges stay positive and payments become negative
  within the merged column. (Note: the cap1 and chase_cc sign
  conventions are opposite; rely on Firefly III's rule engine to flip
  signs for the cap1 account if needed.) Rows where both `Debit` and
  `Credit` are populated cause the upload to be refused.

To add a new bank, drop its JSON template into `configs/`, add an entry
to the `TEMPLATES` dict in `import_csv.py`, add a matching entry to
`configs/account_mappings.json` (its `filename_pattern` /
`csv_column_header` is what makes the template auto-detectable as well
as resolving the per-CSV account override), and (if the CSV format
needs reshaping) register a preprocessor in the `PREPROCESSORS` dict.

## Development

This project is managed with [uv](https://docs.astral.sh/uv/). Sync the
environment, including dev tools, with:

```sh
uv sync
```

Git hooks are managed with [prek](https://github.com/j178/prek), a drop-in
pre-commit alternative. Install the hook once per clone:

```sh
uv run prek install
```

The hooks then run automatically on `git commit`.
To run all hooks against all files on demand:

```sh
uv run prek run --all-files
```
