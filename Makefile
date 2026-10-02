# BDA — developer and pipeline entrypoints.
# Every target here is also reachable as `uv run bda <command>` so that
# the pipeline never depends on `make` being installed.
SHELL := bash
.DEFAULT_GOAL := help

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- Environment -----------------------------------------------------------
bootstrap: ## Create .env, sync deps, verify toolchain
	uv run bda bootstrap

floci-up: ## Start the Floci local AWS emulator
	uv run bda floci up

floci-down: ## Stop the Floci emulator
	uv run bda floci down

floci-status: ## Show emulator health and provisioned resources
	uv run bda floci status

# --- Data platform ---------------------------------------------------------
data: ## Generate synthetic banking source data into data/landing
	uv run bda data generate

pipeline: ## Run bronze -> silver -> gold on Floci (or AWS with AWS_ENDPOINT_URL unset)
	uv run bda pipeline run

dq: ## Run data quality rules and print the result
	uv run bda pipeline dq

catalog: ## Print the published data product catalog
	uv run bda catalog list

ask: ## Ask the Copilot a question. Use: make ask Q="..."
	uv run bda ask "$(Q)"

# --- Quality ---------------------------------------------------------------
fmt: ## Autoformat
	uv run ruff format . && uv run ruff check --fix .

lint: ## Lint
	uv run ruff check . && uv run ruff format --check .

typecheck: ## Static types
	uv run mypy

test: ## Fast unit tests (no emulator required)
	uv run pytest -m "not integration and not slow"

test-all: ## Full suite including Floci integration tests
	uv run pytest

eval: ## Run the agent evaluation harness
	uv run bda eval run

# --- Serving ---------------------------------------------------------------
api: ## Run the Copilot HTTP API
	uv run bda serve --port 8000

ui: ## Run the analyst console
	uv run bda ui

# --- Infrastructure --------------------------------------------------------
synth: ## Synthesise CloudFormation (no AWS call)
	uv run bda infra synth

deploy: ## Deploy via CDK (the pipeline does this, not humans)
	uv run bda infra deploy --env $(ENV)

destroy: ## Tear down a deployed environment
	uv run bda infra destroy --env $(ENV)

demo: ## End-to-end demonstration against Floci
	uv run bda demo

clean: ## Remove caches and generated data
	uv run bda clean

.PHONY: help bootstrap floci-up floci-down floci-status data pipeline dq catalog ask \
        fmt lint typecheck test test-all eval api ui synth deploy destroy demo clean
