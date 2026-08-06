.PHONY: setup build up down logs migrate test lint format smoke-test status backup restore help

PYTHON ?= python
PIP ?= python -m pip

help:
	@echo "Herald Automation System Commands:"
	@echo "  make setup       Install dependencies"
	@echo "  make build       Build Docker images"
	@echo "  make up          Start all services"
	@echo "  make down        Stop all services"
	@echo "  make logs        Tail service logs"
	@echo "  make migrate     Run database migrations"
	@echo "  make test        Run test suite"
	@echo "  make lint        Run code linters"
	@echo "  make format      Run code formatters"
	@echo "  make smoke-test  Run Kokoro TTS smoke test"
	@echo "  make status      Display system health & queue status"
	@echo "  make backup      Run database backup script"
	@echo "  make restore     Run database restore script"

setup:
	$(PIP) install -e .[dev]

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

migrate:
	alembic upgrade head

test:
	pytest -v tests/

lint:
	ruff check packages/ apps/ tests/

format:
	ruff format packages/ apps/ tests/

smoke-test:
	$(PYTHON) scripts/smoke_test.py

status:
	$(PYTHON) scripts/status.py

backup:
	bash scripts/backup.sh

restore:
	bash scripts/restore.sh
