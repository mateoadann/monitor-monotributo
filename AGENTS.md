# Dashboard Monitor de Monotributo

## Contexto y objetivo
Este proyecto crea una herramienta para monitorear el nivel de facturación anual de una persona dentro del régimen argentino de "Monotributo". Este régimen unifica impuestos en un solo pago y aplica a personas físicas, que no tributan IVA como los "Responsables Inscriptos".

Las categorías de Monotributo se determinan por el tope de facturación anual. Ejemplo:
- Categoría A: $8.500.000 al año.
- Si se supera ese tope, corresponde recategorizar a la categoría B (tope $11.000.000).

Las facturas y notas de crédito afectan el total anual:
- Facturas: suman.
- Notas de crédito: restan.

Cada comprobante incluye "Fecha Desde" y "Fecha Hasta", que indican el rango del servicio facturado. El cálculo de facturación anual se realiza sobre los 12 meses previos.

Ejemplo práctico:
- Si hoy es 31/12/2025, se suman los totales desde 01/01/2025 a 31/12/2025.

Caso con rangos que cruzan meses:
- Si "Fecha Desde" y "Fecha Hasta" abarcan más de un mes, el importe debe prorratearse por días y asignarse a cada mes correspondiente.

## Componentes del sistema
1) Proceso RPA: usa Playwright para ingresar a ARCA con usuario y contraseña y descargar documentos (facturas y notas de crédito). Se encola 1 job por monotributista y se procesa en forma secuencial (RPA1 -> PDFs1 -> RPA2) para evitar fallos en lote.
2) Análisis de PDFs: extracción de datos y cálculo de métricas según los campos detectados.
3) Dashboard: UI para gestionar monotributistas, límites por categoría, usuarios y consultas.

## Stack tecnológico
- Backend: Python 3.x, Flask, Flask-Login, Flask-Session, Flask-WTF (CSRF).
- Base de datos: PostgreSQL, SQLAlchemy ORM.
- Jobs y cola: Redis, RQ.
- RPA: Playwright (Chromium).
- Procesamiento de PDFs: pdfplumber (extract_text, tables), lógica de extracción (OCR si no hay texto).
- Frontend: HTML + Jinja2, CSS propio, JS vanilla (fetch, modals, toasts, actualizaciones parciales).
- Infraestructura: Docker Compose (web, worker, db, redis), Nginx reverse proxy, SSL con Certbot, `.env` para variables sensibles.
- Seguridad: hash de passwords (Werkzeug), CSRF en formularios, rate-limit de login con Redis, sesiones server-side, secrets fuera del repo, cookies seguras en producción.

## Paleta de color
- Azul: #242c4f
- Celeste: #53afc0
- Blanco: #ffffff

## Alcance de la primera fase
- Login: pantalla de inicio de sesión con usuario y contraseña.
- Home: pestañas principales.

### 1) Monotributista
Listado de monotributistas con:
- Razón social: nombre impositivo del monotributista.
- CUIT: Clave Única de Identificación Tributaria.
- Clave fiscal: contraseña para el login de RPA en ARCA.
- Categoría actual: categoría asignada.
- Categoría sugerida: resultado del cálculo.
  - Subir categoría: color rojizo.
  - Mantener categoría: color verde.
  - Bajar categoría: color amarillo.

#### Criterios de aceptación
- Se puede listar, crear, editar y eliminar monotributistas.
- Validaciones mínimas: CUIT único, razón social obligatoria, clave fiscal obligatoria.
- La categoría sugerida se calcula y muestra para cada monotributista.
- Los colores de estado se aplican según la comparación entre categoría actual y sugerida.

### 2) Facturas
Listado de datos extraídos de facturas y notas de crédito:
- Facturador: monotributista asociado.
- Fecha: formato DD/MM/AAAA.
- Tipo de comp.: "C", "E", "NCC", "NCE", "NDC", "NDE", "RC".
- Número de comp.: formato "0002-00000147" (punto de venta + número).
- CUIT receptor.
- Razón social receptor.
- Importe total: pesos argentinos, con `.` como separador de miles y `,` como separador decimal.
- Fecha desde: formato DD/MM/AAAA.
- Fecha hasta: formato DD/MM/AAAA.
- Concepto: concepto informado en la factura.

#### Criterios de aceptación
- Se listan facturas y notas de crédito con todos los campos requeridos.
- El formato de fecha es DD/MM/AAAA y el número de comprobante respeta el formato "0002-00000147".
- El importe total se muestra con `.` como separador de miles y `,` como decimal.
- Las notas de crédito se identifican con "NCC" o "NCE" y afectan el cálculo como valores negativos.

### 3) Cálculo
Tabla interactiva para:
- seleccionar un monotributista;
- ver la facturación mensual;
- comparar mes a mes y visualizar información.

#### Criterios de aceptación
- Se puede seleccionar un monotributista y ver su facturación mensual de los últimos 12 meses.
- Se muestran totales mensuales y el total anual acumulado.
- Se identifica la categoría sugerida según el total anual.

### 4) Configuración
CRUD de categorías y otras funcionalidades futuras.

#### Criterios de aceptación
- Se puede crear, editar y eliminar categorías.
- Cada categoría tiene nombre, código y tope anual.
- Las categorías se usan en los cálculos del dashboard.

## Glosario técnico
- Monotributista: persona física inscripta en el régimen de Monotributo.
- CUIT: Clave Única de Identificación Tributaria.
- Clave fiscal: credencial usada para autenticación en ARCA.
- Factura: comprobante que suma a la facturación anual.
- Nota de crédito: comprobante que resta a la facturación anual.
- Fecha desde/hasta: rango de prestación del servicio facturado.
- Categoría actual: categoría asignada al monotributista.
- Categoría sugerida: categoría calculada según facturación anual.
- Tope anual: límite de facturación de una categoría.
- Prorrateo: distribución del importe total de una factura entre meses según cantidad de días.

## Reglas de cálculo detalladas
1) Ventana de cálculo
   - Se consideran los últimos 12 meses completos hacia atrás desde la fecha de referencia.
   - Ejemplo: si la fecha de referencia es 31/12/2025, se considera del 01/01/2025 al 31/12/2025.
2) Suma de comprobantes
   - Facturas (C/E) suman al total anual.
   - Notas de crédito (NCC/NCE) restan al total anual.
   - Notas de débito (NDC/NDE) y Recibo (RC) suman.
3) Prorrateo por rango de fechas
   - Si "Fecha Desde" y "Fecha Hasta" están dentro del mismo mes, el importe se asigna completo a ese mes.
   - Si el rango abarca más de un mes, el importe total se divide por la cantidad de días del rango (incluyendo ambos extremos).
   - El importe diario se multiplica por la cantidad de días que caen en cada mes, asignando el resultado a cada mes.
4) Categoría sugerida
   - Se toma el total anual acumulado y se compara contra los topes de categorías vigentes.
   - La categoría sugerida es la más alta cuyo tope anual sea mayor o igual al total anual.
5) Indicador visual
   - Subir categoría: color rojizo.
   - Mantener categoría: color verde.
   - Bajar categoría: color amarillo.

## Ejemplos de cálculo
### Ejemplo 1: factura dentro del mes
- Fecha desde: 10/03/2025
- Fecha hasta: 20/03/2025
- Importe total: $310.000,00
- Resultado: se asigna $310.000,00 a marzo 2025 (no hay prorrateo entre meses).

### Ejemplo 2: factura que cruza meses (prorrateo)
- Fecha desde: 20/03/2025
- Fecha hasta: 10/04/2025
- Importe total: $220.000,00
- Días totales: 22 (incluye ambos extremos)
- Importe diario: $10.000,00
- Días en marzo: 12 (20 al 31)
- Días en abril: 10 (1 al 10)
- Resultado:
  - Marzo 2025: $120.000,00
  - Abril 2025: $100.000,00

### Ejemplo 3: factura + nota de crédito en ventana anual
- Facturas en 12 meses: $9.000.000,00
- Nota de crédito en 12 meses: $500.000,00
- Total anual: $8.500.000,00
- Resultado: categoría sugerida según el tope vigente.

## Ruta del proyecto
`/Users/mateo/Documents/monitor_monotributo/`

## Docker
### Requisitos
- Docker y Docker Compose instalados.

### Levantar el proyecto
- Construir y levantar:
  - `make up-build`
- Acceso:
  - `http://localhost:5000`

### Notas
- La base de datos SQLite se monta desde `./monitor.db` para persistencia local.
- Variables relevantes en `docker-compose.yml`: `DATABASE_URL`, `SECRET_KEY`.
