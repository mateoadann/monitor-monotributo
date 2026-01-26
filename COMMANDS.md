# Comandos del proyecto

## Docker
- Construir imagenes y levantar servicios:
```bash
docker compose up --build
```

- Levantar servicios sin reconstruir:
```bash
docker compose up
```

- Detener servicios:
```bash
docker compose down
```

- Detener servicios y borrar volumenes (borra datos de Postgres):
```bash
docker compose down -v
```

- Ver logs de `web` en tiempo real:
```bash
docker compose logs -f web
```

- Ver logs de `worker` en tiempo real:
```bash
docker compose logs -f worker
```

- Abrir una shell en el contenedor `web`:
```bash
docker compose exec web bash
```

- Ejecutar tests dentro del contenedor `web`:
```bash
docker compose exec web python -m pytest
```

- Abrir consola `psql` en Postgres:
```bash
docker compose exec postgres psql -U monitor -d monitor
```

- Abrir `redis-cli` en Redis:
```bash
docker compose exec redis redis-cli
```

## Sin Docker (local)
### Entorno virtual
- Crear entorno virtual:
```bash
python3 -m venv .venv
```

- Activar entorno virtual:
```bash
source .venv/bin/activate
```

### Dependencias
- Instalar dependencias base:
```bash
pip install -r requirements.txt
```

- Instalar dependencias de desarrollo (incluye pytest):
```bash
pip install -r requirements-dev.txt
```

### Variables de entorno
- Configurar variables para la app y tests locales:
```bash
export DATABASE_URL=postgresql+psycopg2://monitor:monitor@localhost:5432/monitor
export DATABASE_URL_TEST=postgresql+psycopg2://monitor:monitor@localhost:5432/monitor_test
export SECRET_KEY=change-me
export REDIS_URL=redis://localhost:6379/0
export UPLOAD_FOLDER=./uploads
```

### App y worker
- Ejecutar la app Flask:
```bash
python main.py
```

- Ejecutar el worker de RQ:
```bash
python worker.py
```

### Tests
- Correr todos los tests:
```bash
pytest
```

- Correr tests en modo quiet:
```bash
pytest -q
```

- Correr un archivo especifico:
```bash
pytest tests/test_views.py -q
```

### Playwright
- Instalar Chromium para Playwright:
```bash
python -m playwright install chromium
```

- Ejecutar el script de descarga de PDFs:
```bash
python playwright/descargar-pdf.py
```

### Base de datos
- Abrir `psql` contra la DB local:
```bash
psql -U monitor -d monitor
```
