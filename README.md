# finances

Scripts for working with [Firefly III](https://www.firefly-iii.org/).

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

| Variable             | Purpose                                                                             |
| -------------------- | ----------------------------------------------------------------------------------- |
| `FIREFLY_III_URL`    | Base URL of the Firefly III instance. Used by `--dry-run` to look up account names. |
| `FIREFLY_III_PAT`    | Personal Access Token for Firefly III's API (paired with `FIREFLY_III_URL`).        |
| `DATA_IMPORTER_URL`  | Base URL of the Firefly III **Data Importer** instance (a separate URL).            |
| `AUTO_IMPORT_SECRET` | Shared secret matching the importer's `AUTO_IMPORT_SECRET` env var.                 |

### Account mappings

`configs/account_mappings.json` is your **personal configuration** that
maps each per-account lookup key to a Firefly III `account_id` and
short tag suffix. A single importer template can be shared across
multiple accounts at the same bank (e.g. several Chase credit cards
using one CSV format), so this file is what tells the script which
specific account a given CSV belongs to.

The file is gitignored because the ids are private, and every importer
template under `configs/` ships with `"default_account": 0` as a
deliberate sentinel — Firefly III account ids start at 1, so the
script refuses to upload until you supply a real id for the account
that owns the CSV.

Schema:

```json
{
  "<template_name>": {
    "<lookup_key>": { "account_id": 1, "abbreviation": "aa" }
  },
  "<other_template>": {
    "<lookup_key>": { "account_id": 2, "abbreviation": "bb" }
  }
}
```

For each upload, the script:

1. Resolves the template's detection rule (the `filename_pattern` or
   `csv_column_header` set on its `TemplateInfo` in
   `src/firefly_iii_utils/paths.py`; see the "Template detection rules"
   subsection under Usage for how those fields produce lookup keys) to
   a key — or set of keys for `csv_column_header`.
2. Looks the key(s) up under the template's entry in
   `account_mappings.json` and uses the matched `account_id` as the
   template's `default_account` before sending to the importer.
3. Appends the entry's `abbreviation` to `custom_tag`, so a base tag
   of `"%datetime%: <template_name>"` becomes
   `"%datetime%: <template_name> <abbreviation>"`. The `abbreviation`
   field is optional and may be `""` (or omitted entirely) — in that
   case the tag is left unchanged. This is useful for single-card
   banks where there is nothing to disambiguate.

If the lookup fails — filename doesn't match, captured key isn't in
the template's per-account dict, a CSV row has an unknown key,
multiple rows disagree on the account, or `default_account` is still
`< 1` after the lookup — the script refuses to upload and prints an
error explaining what went wrong.

### Firefly III sign-flip rules

Firefly III expects credits to be positive and debits negative. Some
banks use the inverted convention, so transactions for those accounts
need to be flipped by a pair of rules in Firefly III.

Set this up as **two rules total** — one for the withdrawal direction
and one for the deposit direction. Each rule has one trigger per
affected account id; with **strict mode unchecked**, the rule fires
when any trigger matches. **Stop processing must be checked**
so that it doesn't keep bouncing between the two rules.

| Rule | Trigger (one per affected account id) | Action                                  |
| ---- | ------------------------------------- | --------------------------------------- |
| A    | Destination account ID is exactly..   | Convert the transaction to a withdrawal |
| B    | Source account ID is exactly..        | Convert the transaction to a deposit    |

Set each affected account's Firefly III account id
as the trigger's **Trigger on value**; with three affected accounts
each rule will have three triggers.

Of the templates in this repository, these ones use the inverted convention:

- `cap1_cc`
- `citi_cc`
- `bilt_cc`

## Usage

### `firefly-iii-import-transactions` — upload a bank CSV via the Data Importer

Uploads a CSV plus a JSON template from `configs/` to the importer's
`/autoupload` endpoint, replicating the manual file-upload wizard.

```sh
# Template is auto-detected from the filename_pattern / csv_column_header
# fields on each TemplateInfo in TEMPLATES (src/firefly_iii_utils/paths.py):
uv run firefly-iii-import-transactions path/to/Chase1234_Activity.CSV
uv run firefly-iii-import-transactions path/to/2026-06-06_transaction_download.csv

# Explicit template (overrides auto-detection; must match a key in
# TEMPLATES inside src/firefly_iii_utils/paths.py):
uv run firefly-iii-import-transactions --template chase_cc path/to/Chase1234_Activity.CSV

# Dry run: validate inputs and print what would be sent, without making the request:
uv run firefly-iii-import-transactions --dry-run path/to/Chase1234_Activity.CSV

# Directory: process every *.csv / *.CSV file directly under the
# directory (no recursion) in sorted order. Each file's template is
# auto-detected per file, so --template is not allowed with a
# directory. Processing stops on the first failure.
uv run firefly-iii-import-transactions path/to/transactions/
uv run firefly-iii-import-transactions --dry-run path/to/transactions/

# Disable colored output (also auto-disabled when stdout isn't a TTY
# or when the NO_COLOR environment variable is set):
uv run firefly-iii-import-transactions --no-color path/to/transactions/
```

Auto-detection iterates the templates registered in
`src/firefly_iii_utils/paths.py`'s `TEMPLATES` dict and, for each one
that has a `filename_pattern` or `csv_column_header` set on its
`TemplateInfo`, checks whether the pattern matches the CSV filename or
the column header is present in the CSV's header row. If zero
templates match — or more than one — the script errors out and asks
you to pass `-t/--template`.

#### Template detection rules (`filename_pattern` / `csv_column_header`)

Each `TemplateInfo` in `TEMPLATES` (in `src/firefly_iii_utils/paths.py`)
may set **at most one** of two optional fields to identify which
account a CSV belongs to. The same field drives both auto-detection
(which template to use) and account-mapping lookup (which Firefly III
account id to post to):

- `filename_pattern` — a regex with one capture group, applied to the CSV
  filename. The captured value is the lookup key into
  `configs/account_mappings.json`. Used by `chase_cc`, where the filename
  embeds the last 4 digits of the card.
- `csv_column_header` — the header name of a column in the CSV body. Every
  data row's value in that column is treated as a lookup key into
  `configs/account_mappings.json`. All rows must resolve to the **same**
  Firefly III `account_id`, otherwise the script refuses to upload (this
  guards against mixed-account exports). Used by `cap1_cc`, where the
  filename has no identifier but the `Card No.` column does — and multiple
  card numbers may legitimately point to the same account (e.g. a primary
  card plus an authorized user).

Example:

```python
TEMPLATES: dict[str, TemplateInfo] = {
    "<template_name>": TemplateInfo(
        path=CONFIGS_DIR / "<template_name>.json",
        filename_pattern=r"Bank(\d{4})_",
    ),
    "<other_template>": TemplateInfo(
        path=CONFIGS_DIR / "<other_template>.json",
        csv_column_header="Card No.",
    ),
}
```

#### Per-template CSV preprocessing

Some banks emit CSVs that don't match the importer template's column
shape and need a small transformation before upload. Each `TemplateInfo`
in `TEMPLATES` (in `src/firefly_iii_utils/paths.py`) has an optional
`preprocessor` field; when it's set, the CSV is parsed, rewritten in
memory, and the transformed bytes are uploaded (the original file on
disk is never modified). The preprocessor functions themselves live in
`src/firefly_iii_utils/preprocessors.py`.

Currently registered:

- **`cap1_cc`** — Capital One puts charges in `Debit` (positive) and
  payments / refunds in `Credit` (positive), but the importer template
  only points its `amount` role at `Debit`. For every row with a value
  in `Credit`, the preprocessor moves it into `Debit` with a leading
  minus sign, so charges stay positive and payments become negative
  within the merged column. Rows where both `Debit` and `Credit` are
  populated cause the upload to be refused.
- **`wf_acct`** — Wealthfront's cash-account CSV records internal
  transfers between the user's own Wealthfront accounts as rows where
  `Type` is `Transfer`. The preprocessor drops every such row before
  upload so they aren't imported as standalone deposits / withdrawals.
- **`citi_cc`** — Citi splits its amount across two columns: `Debit`
  for charges (positive) and `Credit` for payments / refunds (already
  negative). The importer template only points its `amount` role at
  `Debit`, so the preprocessor moves each `Credit` value into `Debit`
  **as-is** (Citi already minus-prefixed it). Rows where both `Debit`
  and `Credit` are populated cause the upload to be refused.

To add a new bank, drop its JSON template into `configs/`, register
it in the `TEMPLATES` dict in `src/firefly_iii_utils/paths.py` (the
path, a `filename_pattern` or `csv_column_header`, and optionally a
preprocessor), add the per-account `account_id` and `abbreviation`
entries under that template's key in `configs/account_mappings.json`,
and (if the CSV format needs reshaping) add a preprocessor function
to `src/firefly_iii_utils/preprocessors.py` and wire it through the
new entry's `preprocessor` field.

## Development

This project is managed with [uv](https://docs.astral.sh/uv/). Sync the
environment, including dev tools and an editable install of the
`firefly-iii-utils` package itself, with:

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
