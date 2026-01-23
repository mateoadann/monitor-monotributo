# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Descripcion del proyecto

Monitor de Monotributo es una aplicacion web para el seguimiento de facturacion anual de monotributistas argentinos. Centraliza facturas y notas de credito, calcula el prorrateo por rango de fechas y sugiere la categoria correspondiente segun los topes vigentes.

## Comandos de desarrollo

### Ejecutar con Docker (recomendado)
```bash
docker compose up --build
```
Acceso: http://localhost:5001

### Ejecutar localmente (sin Docker)
```bash
# Activar entorno virtual
source .venv/bin/activate

# Instalar dependencias
pip install -r requirements.txt

# Ejecutar servidor web
python main.py

# Ejecutar worker de procesamiento (en otra terminal)
python worker.py
```
El servidor local corre en http://localhost:5000

### Usuario demo
- Usuario: admin
- Contrasena: admin

## Arquitectura

### Stack
- Backend: Python 3.x + Flask + SQLAlchemy
- Frontend: Jinja2 + CSS + JavaScript vanilla
- Base de datos: SQLite (monitor.db)
- Cola de procesamiento: Redis + RQ

### Estructura principal

```
website/
├── __init__.py      # Factory de la app Flask (create_app)
├── models.py        # Modelos SQLAlchemy (User, Categoria, Monotributista, Factura, etc.)
├── views.py         # Blueprint principal con todas las rutas y logica de calculo
├── auth.py          # Blueprint de autenticacion (login/logout)
├── pdf_extractor.py # Extraccion de datos de facturas PDF con pdfplumber
├── pdf_jobs.py      # Jobs RQ para procesar PDFs en background
├── queue.py         # Conexion a la cola Redis
├── templates/       # Templates Jinja2
└── static/          # CSS y JS
```

### Flujo de datos clave

1. **Carga de facturas**: Se pueden crear manualmente o subir PDFs que se procesan en background via RQ
2. **Extraccion de PDF**: `pdf_extractor.py` parsea el texto del PDF y extrae campos como CUIT, fecha, importe, etc.
3. **Calculo de facturacion**: `calcular_totales()` en views.py implementa el prorrateo diario cuando una factura cruza meses
4. **Categoria sugerida**: Se compara el total anual contra los topes de cada categoria vigente

### Modelos principales

- **Monotributista**: Persona fisica con CUIT, categoria actual y facturas asociadas
- **Factura**: Comprobante con fecha_desde/fecha_hasta para prorrateo, tipo_comp (B, NCB, E)
- **Categoria**: A-K con orden y tope de facturacion
- **Vigencia/CategoriaTope**: Sistema de topes historicos por periodo

### Logica de negocio importante

- Las notas de credito (tipo_comp que empieza con "NC") se almacenan con importe negativo
- El prorrateo divide el importe diario y lo asigna a cada mes del rango fecha_desde a fecha_hasta
- Los estados de categoria son: "sube" (rojizo), "mantiene" (verde), "baja" (amarillo)
- La ventana de calculo siempre considera los ultimos 12 meses completos

## Paleta de colores UI
- Azul: #242c4f
- Celeste: #53afc0
- Blanco: #ffffff
