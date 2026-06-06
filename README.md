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
# Defaults to the chase_cc template:
uv run python import_csv.py path/to/Chase1234_Activity.CSV

# Explicit template (must match a key in TEMPLATES inside import_csv.py):
uv run python import_csv.py --template chase_cc path/to/Chase1234_Activity.CSV

# Dry run: validate inputs and print what would be sent, without making the request:
uv run python import_csv.py --dry-run path/to/Chase1234_Activity.CSV
```

#### Per-account overrides (`configs/account_mappings.json`)

A single importer template can be shared across multiple accounts at the
same bank (e.g. several Chase credit cards using one CSV format). The
mapping file `configs/account_mappings.json` (gitignored, since account
ids are private) lets the script pick the right Firefly III account id
and tag suffix based on the CSV filename. Schema:

```json
{
  "<template_name>": {
    "filename_pattern": "Bank(\\d{4})_",
    "accounts": {
      "<lookup_key>": { "account_id": 1, "abbreviation": "aa" }
    }
  }
}
```

For each upload, when the selected template has an entry in this file:

1. `filename_pattern` is applied to the CSV filename. It must contain
   exactly one capture group; the captured value is the lookup key
   into `accounts`.
2. The matching entry's `account_id` overrides `default_account` in the
   template before it is sent to the importer.
3. The entry's `abbreviation` is appended to `custom_tag`, so a base
   tag of `"%datetime%: <template_name>"` becomes
   `"%datetime%: <template_name> <abbreviation>"`.

If the filename doesn't match the pattern, or the captured key isn't in
`accounts`, or the post-override `default_account` is still `< 1`, the
script refuses to upload and prints an error explaining what went wrong.

To add a new bank, drop its JSON template into `configs/`, add an entry
to the `TEMPLATES` dict in `import_csv.py`, and (if the bank has
multiple accounts sharing one template) add a matching entry to
`configs/account_mappings.json`.

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
