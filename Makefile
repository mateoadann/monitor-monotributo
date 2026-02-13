# ──────────────────────────────────────────────
# ENV=dev (default) | ENV=prod
# Uso: make up              → dev
#      make up ENV=prod      → prod
# ──────────────────────────────────────────────
ENV ?= dev
DC := docker compose -f docker-compose.yml -f docker-compose.$(ENV).yml
COMPOSE_BASE = docker compose -f docker-compose.yml
COMPOSE_DEV = docker compose -f docker-compose.yml -f docker-compose.dev.yml

.PHONY: help up up-build down down-v logs logs-web logs-worker shell
.PHONY: restart-web restart-all
.PHONY: test test-quiet test-file test-views-clean test-db
.PHONY: psql redis rpa
.PHONY: migrate-concepto migrate-import-batch storage-check
.PHONY: vigencias-bootstrap vigencias-bootstrap-dry
.PHONY: proxy-net config
.PHONY: prod prod-build prod-down prod-logs prod-ps prod-restart bootstrap-prod
.PHONY: prod-up prod-up-build
.PHONY: dev-up dev-up-build dev-down dev-logs-web dev-logs-worker dev-shell

help:
	@echo "Uso: make <target> [ENV=dev|prod]  (default: dev)"
	@echo ""
	@echo "  Servicios:"
	@echo "  up                Levanta servicios (sin build)"
	@echo "  up-build          Levanta servicios con build"
	@echo "  restart-web       Reinicia solo el servicio web"
	@echo "  restart-all       Reinicia todos los servicios"
	@echo "  down              Baja servicios"
	@echo "  down-v            Baja servicios y borra volumenes"
	@echo ""
	@echo "  dev-up            Levanta stack DEV (con puertos)"
	@echo "  dev-up-build      Levanta stack DEV con build"
	@echo "  dev-down          Baja stack DEV"
	@echo "  dev-logs-web      Logs web en DEV"
	@echo "  dev-logs-worker   Logs worker en DEV"
	@echo "  dev-shell         Shell en web (DEV)"
	@echo ""
	@echo "  Logs y shell:"
	@echo "  logs              Logs de todos los servicios"
	@echo "  logs-web          Logs del contenedor web"
	@echo "  logs-worker       Logs del worker"
	@echo "  shell             Shell en el contenedor web"
	@echo ""
	@echo "  Tests:"
	@echo "  test              Ejecuta pytest"
	@echo "  test-quiet        Ejecuta pytest -q"
	@echo "  test-file FILE=   Ejecuta pytest en un archivo"
	@echo "  test-views-clean  Build sin cache y corre test_views"
	@echo "  test-db           Crea la DB de tests"
	@echo ""
	@echo "  BD y herramientas:"
	@echo "  psql              Abre psql en Postgres"
	@echo "  redis             Abre redis-cli"
	@echo "  rpa               Ejecuta Playwright (headless)"
	@echo ""
	@echo "  Migraciones:"
	@echo "  migrate-concepto        Migra factura.concepto a TEXT"
	@echo "  migrate-import-batch    Migra factura_import.batch_id"
	@echo "  vigencias-bootstrap-dry Simula bootstrap vigencias"
	@echo "  vigencias-bootstrap     Aplica bootstrap vigencias"
	@echo ""
	@echo "  Produccion:"
	@echo "  prod              Levanta stack prod"
	@echo "  prod-up           Alias de prod"
	@echo "  prod-build        Build y levanta stack prod"
	@echo "  prod-up-build     Alias de prod-build"
	@echo "  prod-down         Baja stack prod"
	@echo "  prod-logs         Logs de prod"
	@echo "  prod-ps           Estado del stack prod"
	@echo "  prod-restart      Reinicia servicios prod"
	@echo "  bootstrap-prod    Setup inicial prod (build + migrate + seed)"
	@echo ""
	@echo "  Infra:"
	@echo "  config            Muestra la config resultante"
	@echo "  proxy-net         Crea la red proxy_net (una sola vez)"
	@echo "  storage-check     Verifica uploads/downloads vacios"

# ── Servicios ──

up:
	$(DC) up -d

up-build:
	$(DC) up -d --build

restart-web:
	$(DC) restart web

restart-all:
	$(DC) restart

down:
	$(DC) down

down-v:
	$(DC) down -v

# ── Logs y shell ──

logs:
	$(DC) logs -f

logs-web:
	$(DC) logs -f web

logs-worker:
	$(DC) logs -f worker

shell:
	$(DC) exec web bash

# ── Tests ──

test:
	$(MAKE) test-db ENV=$(ENV)
	$(DC) exec -T web python -m pytest

test-quiet:
	$(MAKE) test-db ENV=$(ENV)
	$(DC) exec -T web python -m pytest -q

test-file:
	@test -n "$(FILE)" || (echo "Falta FILE=tests/test_views.py" && exit 1)
	$(MAKE) test-db ENV=$(ENV)
	$(DC) exec -T web python -m pytest $(FILE)

test-views-clean:
	$(DC) build --no-cache web
	$(DC) up -d
	$(MAKE) test-db ENV=$(ENV)
	$(DC) exec -T web python -m pytest tests/test_views.py -q

test-db:
	$(DC) exec -T postgres psql -U monitor -d postgres -c "CREATE DATABASE monitor_test;" || true

# ── BD y herramientas ──

psql:
	$(DC) exec postgres psql -U monitor -d monitor

redis:
	$(DC) exec redis redis-cli

rpa:
	$(DC) exec -e PLAYWRIGHT_HEADLESS=1 -e PLAYWRIGHT_TYPE_DELAY=80 web python playwright/descargar-pdf.py

# ── Migraciones ──

migrate-concepto:
	$(DC) exec web python scripts/migrate_factura_concepto_text.py

migrate-import-batch:
	$(DC) exec web python scripts/migrate_factura_import_batch_id.py

vigencias-bootstrap-dry:
	$(DC) exec -T web python scripts/bootstrap_vigencias_prod.py

vigencias-bootstrap:
	$(DC) exec -T web python scripts/bootstrap_vigencias_prod.py --apply

# ── Infra ──

storage-check:
	$(DC) exec web python check_storage_empty.py

config:
	$(DC) config

proxy-net:
	docker network create proxy_net

# ── Produccion (atajos sin ENV=prod) ──

DC_PROD := docker compose -f docker-compose.yml -f docker-compose.prod.yml

prod:
	$(DC_PROD) up -d

prod-up:
	$(DC_PROD) up -d

prod-build:
	$(DC_PROD) up -d --build

prod-up-build:
	$(DC_PROD) up -d --build

prod-down:
	$(DC_PROD) down

prod-logs:
	$(DC_PROD) logs -f

prod-ps:
	$(DC_PROD) ps

prod-restart:
	$(DC_PROD) restart

bootstrap-prod:
	$(DC_PROD) up -d --build
	$(DC_PROD) exec -T postgres psql -U monitor -d postgres -c "CREATE DATABASE monitor_test;" || true
	$(DC_PROD) exec -T web python scripts/bootstrap_vigencias_prod.py --apply
	@echo ""
	@echo "Bootstrap completo. Stack prod corriendo."
	@echo "Configurar NPM apuntando a monitor_web:5000"

dev-up:
	$(COMPOSE_DEV) up -d
dev-up-build:
	$(COMPOSE_DEV) up -d --build
dev-down:
	$(COMPOSE_DEV) down
dev-logs-web:
	$(COMPOSE_DEV) logs -f web
dev-logs-worker:
	$(COMPOSE_DEV) logs -f worker
dev-shell:
	$(COMPOSE_DEV) exec web bash
