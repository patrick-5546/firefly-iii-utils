# finances

Scripts to process financial transaction CSVs and import them into a finance
tracker via its API.

## Development

This project is managed with [uv](https://docs.astral.sh/uv/). Sync the
environment, including dev tools, with:

```sh
uv sync
```

Run the dev tools via `uv run`:

```sh
uv run ruff check     # lint (add --fix to auto-fix)
uv run ruff format    # format
uv run basedpyright   # type-check
```

Git hooks are managed with [prek](https://github.com/j178/prek), a drop-in
pre-commit alternative. Install the hook once per clone:

```sh
uv run prek install
```

The hooks (ruff check, ruff format, basedpyright) then run automatically on
`git commit`. To run them against all files on demand:

```sh
uv run prek run --all-files
```
