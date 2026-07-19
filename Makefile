UV ?= uv

.DEFAULT_GOAL := help

.PHONY: help setup sync format lint typecheck check hooks

help: ## Show available development commands.
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: sync ## Sync dependencies and install the Git hook.
	$(UV) run prek install

sync: ## Sync the locked development environment.
	$(UV) sync

format: ## Format Python and apply Ruff auto-fixes.
	$(UV) run ruff format
	$(UV) run ruff check --fix

lint: ## Check Python formatting and lint rules without changing files.
	$(UV) run ruff format --check
	$(UV) run ruff check

typecheck: ## Run both configured Python type checkers.
	$(UV) run ty check
	$(UV) run pyrefly check

check: lint typecheck ## Run all non-mutating code quality checks.

hooks: ## Run all prek hooks; configured hooks may apply fixes.
	$(UV) run prek run --all-files
