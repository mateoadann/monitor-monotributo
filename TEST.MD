# Tests

## Como funcionan
Los tests usan pytest y un set de fixtures que levantan una app Flask conectada a PostgreSQL para cada test.

### Estructura base
- `tests/conftest.py` define el fixture `app`: lee `DATABASE_URL_TEST`/`DATABASE_URL`, exige PostgreSQL, setea `UPLOAD_FOLDER` a un tmp, crea la app con `create_app(init_db=False)`, crea tablas y un usuario admin, y al final hace `drop_all` para dejar la DB limpia.
- `tests/conftest.py` tambien define `client`, que es el `app.test_client()` de Flask para hacer requests sin servidor real.

### Que prueba cada archivo
- `tests/test_utils.py`: unit tests puros de helpers en `website.views` (`parse_decimal`, `format_currency`, `split_numero_comp`), sin DB.
- `tests/test_calculos.py`: logica de categorias y prorrateo (`categoria_por_total`, `estado_categoria`, `calcular_totales`). Inserta datos en DB y verifica total mensual/anual.
- `tests/test_views.py`: endpoints con `client` (login, creacion de monotributistas y facturas). Verifica respuesta HTML y estado real en DB.

## Ejecutar tests con Docker (recomendado)
### Requisitos
- Docker + Docker Compose instalados.

### Pasos
1) Levantar Postgres:
```bash
docker compose up -d postgres
```

2) Crear la base de tests (solo la primera vez):
```bash
docker compose exec postgres psql -U monitor -c "CREATE DATABASE monitor_test;"
```

3) Reconstruir la imagen `web` (asegura `pytest` disponible):
```bash
docker compose build --no-cache web
```

4) Correr los tests dentro del contenedor `web`:
```bash
docker compose run --rm -e DATABASE_URL_TEST=postgresql+psycopg2://monitor:monitor@postgres:5432/monitor_test web python -m pytest
```

### Notas utiles
- Esto evita el problema de `localhost` porque dentro del contenedor el host es `postgres`.
- Los tests requieren PostgreSQL (se valida en `tests/conftest.py`).

## Automatizacion
### CI (GitHub Actions)
- Workflow: `.github/workflows/tests.yml`.
- Se ejecuta en cada push y pull request.
- Usa Docker Compose, crea la base de tests y corre `pytest`.

### Hook local (pre-push)
1) Dar permisos al script:
```bash
chmod +x scripts/pre-push-tests.sh
```

2) Instalar el hook (desde la raiz del repo):
```bash
ln -s ../../scripts/pre-push-tests.sh .git/hooks/pre-push
```

Alternativa si no queres symlink:
```bash
cp scripts/pre-push-tests.sh .git/hooks/pre-push
```
