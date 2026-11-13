.PHONY: setup run test lint format typecheck audit docker-build docker-up backup help

PYTHON ?= python3
VENV ?= .venv
BIN = $(VENV)/bin

# Detect Windows virtualenv path
ifeq ($(OS),Windows_NT)
    BIN = $(VENV)/Scripts
endif

help:
	@echo "Available commands:"
	@echo "  make setup        - Create virtualenv and install all dependencies"
	@echo "  make run          - Run NudgeBot locally"
	@echo "  make test         - Run pytest test suite"
	@echo "  make lint         - Run ruff linter check"
	@echo "  make format       - Format code with ruff"
	@echo "  make typecheck    - Run mypy type checks"
	@echo "  make audit        - Run pip-audit security vulnerability scanner"
	@echo "  make docker-build - Build production Docker image"
	@echo "  make docker-up    - Start container via docker compose"
	@echo "  make backup       - Run SQLite online backup"

setup:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements-dev.txt

run:
	$(BIN)/python main.py

test:
	$(BIN)/pytest -q

lint:
	$(BIN)/ruff check .

format:
	$(BIN)/ruff format .

typecheck:
	$(BIN)/mypy .

audit:
	$(BIN)/pip-audit -r requirements.txt

docker-build:
	docker compose build

docker-up:
	docker compose up -d

backup:
	$(BIN)/python scripts/backup_db.py

