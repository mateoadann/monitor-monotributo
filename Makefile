.PHONY: help up up-build down down-v logs-web logs-worker shell test test-quiet test-file
.PHONY: psql redis rpa migrate-concepto storage-check test-db test-views-clean

help:
	@echo "Targets disponibles:"
	@echo "  up                Levanta servicios (sin build)"
	@echo "  up-build          Levanta servicios con build"
	@echo "  down              Baja servicios"
	@echo "  down-v            Baja servicios y borra volumenes"
	@echo "  logs-web          Logs del contenedor web"
	@echo "  logs-worker       Logs del worker"
	@echo "  shell             Shell en el contenedor web"
	@echo "  test              Ejecuta pytest"
	@echo "  test-quiet        Ejecuta pytest -q"
	@echo "  test-file FILE=   Ejecuta pytest en un archivo"
	@echo "  test-views-clean  Build sin cache y corre test_views"
	@echo "  test-db           Crea la DB de tests"
	@echo "  psql              Abre psql en Postgres"
	@echo "  redis             Abre redis-cli"
	@echo "  rpa               Ejecuta Playwright (headless)"
	@echo "  migrate-concepto  Migra factura.concepto a TEXT"
	@echo "  storage-check     Verifica uploads/downloads vacios"

up:
	docker compose up -d

up-build:
	docker compose up -d --build

down:
	docker compose down

down-v:
	docker compose down -v

logs-web:
	docker compose logs -f web

logs-worker:
	docker compose logs -f worker

shell:
	docker compose exec web bash

test:
	docker compose exec web python -m pytest

test-quiet:
	docker compose exec web python -m pytest -q

test-file:
	@test -n "$(FILE)" || (echo "Falta FILE=tests/test_views.py" && exit 1)
	docker compose exec web python -m pytest $(FILE)

test-views-clean:
	docker compose build --no-cache web
	docker compose up -d
	docker compose exec web python -m pytest tests/test_views.py -q

test-db:
	docker compose exec postgres psql -U monitor -c "CREATE DATABASE monitor_test;" || true

psql:
	docker compose exec postgres psql -U monitor -d monitor

redis:
	docker compose exec redis redis-cli

rpa:
	docker compose exec -e PLAYWRIGHT_HEADLESS=1 -e PLAYWRIGHT_TYPE_DELAY=80 web python playwright/descargar-pdf.py

migrate-concepto:
	docker compose exec web python scripts/migrate_factura_concepto_text.py

storage-check:
	docker compose exec web python check_storage_empty.py
