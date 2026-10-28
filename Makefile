PYTHON ?= python3
VENV   := .venv
PY     := $(VENV)/bin/python
PIP    := $(VENV)/bin/pip

.DEFAULT_GOAL := help
.PHONY: help venv install install-optional run run-dry run-offline test lint fmt clean clean-data init-db launchd-install launchd-uninstall

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv
	@test -d $(VENV) || $(PYTHON) -m venv $(VENV)

install: venv ## Install core dependencies
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

install-optional: venv ## Install optional extras (chroma, sentence-transformers, LLM SDKs)
	$(PIP) install -r requirements-optional.txt

run: ## Full pipeline (network collectors + configured LLM provider)
	$(PY) -m src.pipeline.run

run-dry: ## Dry-run: sample data, mock LLM, no network, no DB/vector writes
	$(PY) -m src.pipeline.run --dry-run

run-offline: ## Real pipeline against bundled sample data (writes DB + drafts)
	$(PY) -m src.pipeline.run --offline

init-db: ## Create the SQLite schema only
	$(PY) -m src.storage.db --init

test: ## Run the test suite
	$(PY) -m pytest

lint: ## Ruff lint (requires: pip install ruff)
	$(PY) -m ruff check src tests

fmt: ## Ruff format
	$(PY) -m ruff format src tests

clean: ## Remove caches
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache

clean-data: ## DESTRUCTIVE: wipe local DB, vector store and collected files
	@echo "This deletes data/content.db, data/raw/*, data/processed/*, data/vectorstore/*"
	@read -p "Type 'yes' to continue: " ans && [ "$$ans" = "yes" ]
	rm -f data/content.db
	rm -rf data/raw/* data/processed/* data/vectorstore/*

launchd-install: ## Register the Mon/Fri launchd schedule
	./scripts/install_launchd.sh

launchd-uninstall: ## Remove the launchd schedule
	./scripts/uninstall_launchd.sh
