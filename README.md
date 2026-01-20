# Monitor de Monotributo

Este proyecto resuelve el seguimiento del tope anual de facturacion para monotributistas. Centraliza facturas y notas de credito, prorratea importes por rango de fechas y sugiere la categoria correcta segun los topes vigentes.

## Que incluye
- Login de acceso.
- CRUD de monotributistas.
- CRUD de facturas y notas de credito.
- Calculo de facturacion mensual y total anual con prorrateo.
- CRUD de categorias con topes.
- Dashboard con filtros, estados y resalta si sube, mantiene o baja categoria.

## Stack
- Backend: Python + Flask + SQLAlchemy
- Frontend: Jinja2 + CSS + JS vanilla
- Base de datos: SQLite (archivo local)
- Contenedores: Docker Compose

## Ejecutar con Docker
Requisitos: Docker y Docker Compose.

```bash
docker compose up --build
```

Luego abrir:
```
http://localhost:5001
```

## Usuario demo
- Usuario: admin
- Contrasena: admin

## Notas
- `monitor.db` es la base local de SQLite donde se guardan usuarios, categorias, monotributistas y facturas.
- Las notas de credito se cargan con importes negativos.
- El rango "fecha desde/hasta" se prorratea por dia cuando cruza meses.
- En proximas iteraciones se migrara a PostgreSQL para mayor robustez.
