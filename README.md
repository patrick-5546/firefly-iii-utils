# finances

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
They only **check** and never modify your files, so if a hook fails,
fix the issues with `uv run ruff check --fix` and `uv run ruff format`.
To run all hooks against all files on demand:

```sh
uv run prek run --all-files
```
