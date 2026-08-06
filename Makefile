.PHONY: help build up down restart logs ps migrate test test-postgres smoke status backup restore-test readiness

help:
	@echo "Herald Operational Commands:"
	@echo "  make build          Build all Docker images"
	@echo "  make up             Start entire stack in detached mode"
	@echo "  make down           Stop stack and remove containers"
	@echo "  make restart        Restart services"
	@echo "  make migrate        Run Alembic database migrations"
	@echo "  make test           Run unit and integration test suite inside container"
	@echo "  make status         Show system job queue metrics and database health"
	@echo "  make readiness      Check API readiness endpoint"
	@echo "  make smoke          Run audio pipeline smoke test"
	@echo "  make backup         Execute full system backup script"
	@echo "  make restore-test   Execute backup restore test"

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

restart:
	docker compose restart

logs:
	docker compose logs -f --tail=100

ps:
	docker compose ps

migrate:
	docker compose run --rm herald-migration

test:
	docker compose run --rm herald-api python -m pytest -v tests/

test-postgres:
	docker compose run --rm herald-api python -m pytest -v tests/unit/test_postgres_concurrency.py

readiness:
	curl -f http://127.0.0.1:8000/readiness

smoke:
	docker compose run --rm herald-worker python scripts/smoke_test.py

status:
	docker compose run --rm herald-api python scripts/status.py

backup:
	bash scripts/backup.sh

restore-test:
	bash scripts/restore.sh
