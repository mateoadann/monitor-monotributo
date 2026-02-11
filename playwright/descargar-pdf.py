import argparse
import logging
import os
import re
import sys
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Playwright, TimeoutError as PlaywrightTimeoutError, sync_playwright
from redis import Redis
from rq import Queue, Retry

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from website import create_app
from website.models import Factura, FacturaImport, Monotributista, db
from website.pdf_jobs import (
    build_numero_comp,
    cleanup_pdf_path,
    handle_import_failure,
    process_factura_import,
)

CUIT = "20442030147"
CLAVE_FISCAL = "vokqu0-qamqet"
CUIT_LUCIANO = "20278955770"
CLAVE_LUCIANO = "Luciano2208"
FECHA_DESDE = "01/01/2025"
FECHA_HASTA = "31/12/2025"
TIPOS_COMPROBANTE = ["11", "12", "13", "15", "19", "20", "21"]
SELECCIONAR_TIPO_COMPROBANTE = False
TIPO_COMP_MAP = {
    "11": "C",
    "12": "NDC",
    "13": "NCC",
    "15": "RC",
    "19": "E",
    "20": "NDE",
    "21": "NCE",
}
TIPO_LABEL_MAP = {
    "factura c": "C",
    "factura e": "E",
    "nota de credito c": "NCC",
    "nota de credito e": "NCE",
    "nota de debito c": "NDC",
    "nota de debito e": "NDE",
    "recibo c": "RC",
    "recibo": "RC",
}
RESULTS_TABLE_HEADER_MARKERS = (
    "fecha emision",
    "tipo comprobante",
    "nro comprobante",
    "importe total",
    "ver",
)
MAX_DESCARGAS = None
HEADLESS = os.environ.get("PLAYWRIGHT_HEADLESS", "1") != "0"
RECORDING = os.environ.get("recording") or os.environ.get("RECORDING")
RECORDING = str(RECORDING).lower() == "true"


def parse_numero_comp(row_text: str) -> str | None:
    match = re.search(r"\b(\d{4,5})-(\d{8})\b", row_text)
    if not match:
        return None
    return build_numero_comp(match.group(1), match.group(2))


def extract_header_name(page) -> str | None:
    candidates = [
        page.locator("#encabezado_usuario_container"),
        page.locator("#encabezado_usuario"),
        page.locator("text=Usuario:"),
    ]
    for locator in candidates:
        if locator.count() == 0:
            continue
        text = (locator.first.text_content() or "").strip()
        if not text:
            continue
        match = re.search(r"Usuario:\s*\d+\s*-\s*(.+)", text)
        if match:
            return match.group(1).strip()
    return None


def normalize_label(label: str) -> str:
    return " ".join(label.split())


def normalize_key(label: str) -> str:
    cleaned = normalize_label(label)
    cleaned = unicodedata.normalize("NFKD", cleaned)
    cleaned = "".join(ch for ch in cleaned if not unicodedata.combining(ch))
    cleaned = cleaned.casefold()
    cleaned = re.sub(r"[^\w\s]", "", cleaned)
    return " ".join(cleaned.split())


def collect_action_labels(scope):
    locator = scope.locator(
        "button, [role='button'], input[type='submit'], input[type='button'], a"
    )
    labels = []
    count = locator.count()
    for idx in range(count):
        node = locator.nth(idx)
        try:
            tag = (node.evaluate("el => el.tagName") or "").lower()
        except Exception:
            tag = ""
        text = (node.text_content() or "").strip()
        if not text:
            text = (node.get_attribute("value") or "").strip()
        if not text:
            text = (node.get_attribute("aria-label") or "").strip()
        if not text:
            text = (node.get_attribute("title") or "").strip()
        if not text:
            continue
        display = normalize_label(text)
        labels.append((display, normalize_key(display), idx, tag))
    return locator, labels


def find_empresa_form(page):
    form_selector = 'form[name="seleccionaEmpresaForm"]'
    main_form = page.locator(form_selector)
    if main_form.count() > 0:
        return page, main_form, "main"
    for frame in page.frames:
        frame_form = frame.locator(form_selector)
        if frame_form.count() > 0:
            return frame, frame_form, frame.url or "frame"
    return page, main_form, "not-found"


def enqueue_import(
    queue: Queue,
    monotributista_id: int | None,
    pdf_path: str,
    filename: str,
    upload_root: str,
) -> None:
    factura_import = FacturaImport(
        monotributista_id=monotributista_id,
        status="pending",
        pdf_path=pdf_path,
        filename=filename,
        source="rpa",
    )
    db.session.add(factura_import)
    db.session.flush()
    try:
        queue.enqueue(
            process_factura_import,
            factura_import.id,
            job_timeout=300,
            retry=Retry(max=3, interval=[10, 30, 60]),
            on_failure=handle_import_failure,
        )
        factura_import.result_message = "En cola para procesamiento."
    except Exception as exc:
        factura_import.status = "failed"
        factura_import.error = f"No se pudo encolar: {exc}"
        factura_import.result_message = factura_import.error
        cleanup_pdf_path(pdf_path, upload_root)
    db.session.commit()


def record_rpa_failure(
    monotributista_id: int | None,
    message: str,
) -> None:
    db.session.add(
        FacturaImport(
            monotributista_id=monotributista_id,
            status="failed",
            pdf_path="",
            source="rpa",
            result_message=message,
            error=message,
            processed_at=datetime.now(timezone.utc),
        )
    )
    db.session.commit()


def wait_for_login_result(page, timeout_ms: int = 10000) -> tuple[str, str | None]:
    error_locator = page.locator('form[name="F1"]').filter(
        has_text=re.compile(r"Clave o usuario incorrecto", re.I)
    )
    buscador = page.get_by_role("combobox", name="Buscador")
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            if error_locator.count() > 0 and error_locator.first.is_visible():
                error_text = (error_locator.first.inner_text() or "").strip()
                return "error", error_text or "Clave o usuario incorrecto"
        except Exception:
            pass
        try:
            if buscador.count() > 0 and buscador.first.is_visible():
                return "ok", None
        except Exception:
            pass
        page.wait_for_timeout(250)
    return "timeout", None


def format_comp_id(tipo_comp: str | None, numero_comp: str) -> str:
    if tipo_comp:
        return f"{tipo_comp} {numero_comp}"
    return numero_comp


def parse_tipo_comp(label: str | None) -> str | None:
    if not label:
        return None
    key = normalize_key(label)
    if not key:
        return None

    if key in TIPO_LABEL_MAP:
        return TIPO_LABEL_MAP[key]
    if key.startswith("factura"):
        if key.endswith(" e"):
            return "E"
        if key.endswith(" c"):
            return "C"
    if "nota de credito" in key:
        if key.endswith(" e"):
            return "NCE"
        if key.endswith(" c"):
            return "NCC"
    if "nota de debito" in key:
        if key.endswith(" e"):
            return "NDE"
        if key.endswith(" c"):
            return "NDC"
    if key.startswith("recibo"):
        return "RC"
    return None


def parse_result_row(row):
    cells_raw = row.locator("td").all_text_contents()
    cells = [normalize_label(item) for item in cells_raw if item and item.strip()]
    row_text = " ".join(cells)
    numero_comp = parse_numero_comp(row_text)
    tipo_label = cells[1] if len(cells) > 1 else ""
    tipo_comp = parse_tipo_comp(tipo_label) or parse_tipo_comp(row_text)
    return {
        "row": row,
        "row_text": row_text,
        "numero_comp": numero_comp,
        "tipo_comp": tipo_comp,
    }


def find_results_table(page):
    tables = page.locator("table")
    table_count = tables.count()
    if table_count <= 0:
        return None, "sin-tablas"

    best_idx = None
    best_score = -1
    for idx in range(table_count):
        table = tables.nth(idx)
        headers = [text.strip() for text in table.locator("th").all_text_contents() if text.strip()]
        if not headers:
            continue
        header_key = normalize_key(" ".join(headers))
        score = sum(1 for marker in RESULTS_TABLE_HEADER_MARKERS if marker in header_key)
        if score > best_score:
            best_score = score
            best_idx = idx

    if best_idx is not None and best_score >= 2:
        return tables.nth(best_idx), f"headers-score={best_score}"
    if table_count == 1:
        return tables.first, "single-table-fallback"
    return None, "no-header-match"


def get_results_rows(page):
    table, source = find_results_table(page)
    if table:
        rows = table.locator("tbody tr")
        if rows.count() == 0:
            rows = table.locator("tr:has(td)")
        return rows, source

    rows = page.locator("tr:has(td)")
    return rows, f"global-fallback:{source}"


def get_results_signature(table_rows) -> tuple[int, int, str | None, str | None]:
    total_rows = table_rows.count()
    if total_rows <= 0:
        return 0, 0, None, None

    keys = []
    for idx in range(total_rows):
        parsed = parse_result_row(table_rows.nth(idx))
        numero_comp = parsed["numero_comp"]
        if not numero_comp:
            continue
        keys.append(format_comp_id(parsed["tipo_comp"], numero_comp))

    if not keys:
        return total_rows, 0, None, None
    return total_rows, len(keys), keys[0], keys[-1]


def has_no_results_indicator(page) -> bool:
    patterns = [
        r"No se encontraron",
        r"No existen comprobantes",
        r"No hay comprobantes",
        r"Sin resultados",
    ]
    for pattern in patterns:
        try:
            if page.get_by_text(re.compile(pattern, re.I)).count() > 0:
                return True
        except Exception:
            continue
    return False


def wait_for_results_stable(
    page,
    logger,
    *,
    baseline_signature: tuple[int, int, str | None, str | None] | None = None,
    timeout_ms: int = 20000,
    poll_ms: int = 300,
    stable_window_ms: int = 1800,
    min_wait_ms: int = 1200,
):
    started_at = time.monotonic()
    last_change_at = started_at
    changed_from_baseline = baseline_signature is None
    last_signature = baseline_signature
    table_rows, rows_source = get_results_rows(page)

    while True:
        table_rows, rows_source = get_results_rows(page)
        signature = get_results_signature(table_rows)
        now = time.monotonic()
        elapsed_ms = (now - started_at) * 1000

        if baseline_signature is not None and signature != baseline_signature:
            changed_from_baseline = True
        has_no_results = has_no_results_indicator(page)
        if signature[0] == 0 and has_no_results:
            changed_from_baseline = True

        if signature != last_signature:
            last_signature = signature
            last_change_at = now
        else:
            stable_for_ms = (now - last_change_at) * 1000
            has_rows_or_empty = signature[0] > 0 or has_no_results
            has_min_wait = changed_from_baseline or elapsed_ms >= min_wait_ms
            if has_rows_or_empty and has_min_wait and stable_for_ms >= stable_window_ms:
                total_rows, parseable_count, first_item, last_item = signature
                logger.info(
                    "Resultados estables detectados (source=%s): filas=%s, parseables=%s, primer=%s, ultimo=%s.",
                    rows_source,
                    total_rows,
                    parseable_count,
                    first_item or "-",
                    last_item or "-",
                )
                return table_rows, rows_source

        if elapsed_ms >= timeout_ms:
            total_rows, parseable_count, first_item, last_item = signature
            logger.warning(
                "Timeout esperando estabilidad de resultados (%sms, source=%s). Ultimo estado: filas=%s, parseables=%s, primer=%s, ultimo=%s.",
                timeout_ms,
                rows_source,
                total_rows,
                parseable_count,
                first_item or "-",
                last_item or "-",
            )
            return table_rows, rows_source

        page.wait_for_timeout(poll_ms)


def run(
    playwright: Playwright,
    *,
    monotributista_id: int | None = None,
    cuit: str | None = None,
    clave_fiscal: str | None = None,
    fecha_desde: str | None = None,
    fecha_hasta: str | None = None,
    tipos_comprobante: list[str] | None = None,
    seleccionar_tipo: bool | None = None,
) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger(__name__)
    if RECORDING:
        logger.info("Recording RPA habilitado: se guardan videos y trace.")
    else:
        logger.info("Recording RPA deshabilitado.")

    fecha_desde_value = fecha_desde or FECHA_DESDE
    fecha_hasta_value = fecha_hasta or FECHA_HASTA
    tipos_value = list(tipos_comprobante or TIPOS_COMPROBANTE)
    if seleccionar_tipo is None:
        seleccionar_tipo = SELECCIONAR_TIPO_COMPROBANTE

    app = create_app(init_db=False)
    upload_root = Path(app.config["UPLOAD_FOLDER"])
    upload_root.mkdir(parents=True, exist_ok=True)
    base_dir = upload_root / "facturas"
    base_dir.mkdir(parents=True, exist_ok=True)
    redis_url = app.config.get("REDIS_URL", "redis://localhost:6379/0")
    queue = Queue("facturas", connection=Redis.from_url(redis_url))

    browser = playwright.chromium.launch(headless=HEADLESS)
    artifacts_dir = PROJECT_ROOT / "rpa_artifacts"
    if RECORDING:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Artefactos RPA: %s", artifacts_dir)
        record_video_dir = str(artifacts_dir / "videos")
        context = browser.new_context(
            accept_downloads=True,
            record_video_dir=record_video_dir,
            record_video_size={"width": 1280, "height": 720},
        )
    else:
        context = browser.new_context(accept_downloads=True)
    if RECORDING:
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
    page = context.new_page()

    with app.app_context():
        cuit_value = cuit or CUIT
        clave_value = clave_fiscal or CLAVE_FISCAL
        monotributista = None
        if monotributista_id:
            monotributista = db.session.get(Monotributista, monotributista_id)
            if not monotributista:
                logger.warning(
                    "No se encontro monotributista en la DB para ID %s.",
                    monotributista_id,
                )
                return
            else:
                cuit_value = monotributista.cuit
                clave_value = monotributista.clave_fiscal
        elif cuit_value:
            monotributista = Monotributista.query.filter_by(cuit=cuit_value).first()

        monotributista_id = monotributista.id if monotributista else None
        monotributista_label = (
            monotributista.razon_social if monotributista else cuit_value
        )
        if not monotributista_id:
            logger.warning(
                "No se encontro monotributista en la DB para CUIT %s.", cuit_value
            )

        page1 = None
        page2 = None
        monotributista_nombre = None
        try:
            logger.info("Abriendo landing de AFIP.")
            page.goto("https://www.afip.gob.ar/landing/default.asp", wait_until="domcontentloaded")
            with page.expect_popup() as page1_info:
                page.get_by_role("link", name="Iniciar sesión").click()
            page1 = page1_info.value
            page1.wait_for_load_state("domcontentloaded")
            page1.get_by_role("spinbutton").click()

            # CUIT del monotributista a consultar:
            logger.info("Ingresando CUIT.")
            page1.get_by_role("spinbutton").fill(cuit_value)
            page1.get_by_role("button", name="Siguiente").click()

            # Clave fiscal del monotributista a consultar:
            logger.info("Ingresando clave fiscal.")
            page1.get_by_role("textbox", name="TU CLAVE").fill(clave_value)
            page1.get_by_role("button", name="Ingresar").click()

            login_status, login_error = wait_for_login_result(page1)
            if login_status != "ok":
                if login_status == "error":
                    message = "CUIT/CUIL Clave o usuario incorrecto"
                elif login_status == "timeout":
                    message = "No se pudo validar login (buscador no visible)."
                else:
                    message = login_error or "No se pudo validar login."
                logger.error("Login fallido para CUIT %s", cuit_value)
                record_rpa_failure(monotributista_id, message)
                return

            # Ingreso al servicio "Comprobantes en línea"
            logger.info("Buscando servicio Comprobantes en línea.")
            search_input = page1.get_by_role("combobox", name="Buscador")
            search_input.click()
            service_link = page1.get_by_role(
                "link",
                name=re.compile(r"Comprobantes en l[ií]nea", re.I),
            ).first

            def wait_for_service(timeout: int) -> bool:
                try:
                    service_link.wait_for(timeout=timeout)
                    return True
                except PlaywrightTimeoutError:
                    return False

            delay = int(os.environ.get("PLAYWRIGHT_TYPE_DELAY", "80"))
            search_input.type("Comprobantes", delay=delay)
            if not wait_for_service(8000):
                search_input.type(" en", delay=delay)
            if not wait_for_service(8000):
                search_input.type(" linea", delay=delay)
            if not wait_for_service(8000):
                logger.warning("No aparece sugerencia, reintentando con Enter.")
                search_input.press("Enter")
                service_link.wait_for(timeout=10000)
            with page1.expect_popup() as page2_info:
                service_link.click()

            page2 = page2_info.value
            page2.wait_for_load_state("domcontentloaded")

            logger.info("Esperando selector de empresa.")
            selector_text = "Seleccione la Empresa a representar"
            base_page, empresa_form, scope_label = find_empresa_form(page2)
            if scope_label == "not-found":
                logger.warning("No se encontro el form en el documento principal ni en frames.")
                for idx, frame in enumerate(page2.frames):
                    logger.info("Frame %s url=%s", idx, frame.url or "(sin url)")
            else:
                logger.info("Form encontrado en scope: %s", scope_label)
            empresa_form.wait_for(timeout=15000)
            try:
                base_page.get_by_text(selector_text, exact=False).wait_for(timeout=15000)
            except Exception:
                pass

            header_name = extract_header_name(page2)
            if header_name:
                monotributista_nombre = header_name
                logger.info("Usuario detectado: %s", header_name)
            else:
                logger.warning("No se pudo extraer el usuario del encabezado.")

            logger.info("Extrayendo botones disponibles.")
            try:
                form_text = empresa_form.inner_text().strip()
            except Exception:
                form_text = ""
            if form_text:
                logger.info("Texto form: %s", form_text)

            empresa_table = empresa_form.get_by_role("table")
            cells = empresa_table.get_by_role("cell")
            cell_texts = [text.strip() for text in cells.all_text_contents() if text.strip()]
            if cell_texts:
                for idx, label in enumerate(cell_texts, start=1):
                    logger.info("Celda %s: %s", idx, label)

            base_scope = empresa_form
            buttons, labels = collect_action_labels(base_scope)
            if not labels:
                logger.warning("No se encontraron botones en el form, usando fallback tabla.")
                base_scope = empresa_table
                buttons, labels = collect_action_labels(base_scope)
            if not labels:
                logger.warning("No se encontraron botones en la tabla, usando fallback global.")
                base_scope = base_page
                buttons, labels = collect_action_labels(base_scope)
            if not labels:
                logger.warning("No se encontraron botones en la pagina.")
            else:
                logger.info("Elementos detectados: %s", len(labels))
                for idx, (label, _, _, tag) in enumerate(labels, start=1):
                    logger.info("Boton %s: %s (tag=%s)", idx, label, tag)

            if header_name and labels:
                target_key = normalize_key(header_name)
                selected = next((item for item in labels if item[1] == target_key), None)
                if not selected:
                    selected = next((item for item in labels if target_key in item[1]), None)
                if selected:
                    label, _, idx, _ = selected
                    logger.info("Boton del monotributista: %s", label)
                    buttons.nth(idx).click()
                else:
                    logger.warning("No se pudo identificar boton para %s", header_name)
            elif labels:
                logger.warning("No se pudo seleccionar boton: falta usuario del encabezado.")

            page2.get_by_role("button", name="Consultas").click()

            remaining_downloads = MAX_DESCARGAS
            tipos_a_consultar = tipos_value if seleccionar_tipo else [None]
            for tipo_code in tipos_a_consultar:
                # Rango de fechas a consultar:
                logger.info("Cargando rango de fechas.")
                desde_input = page2.get_by_role("textbox", name="Desde")
                hasta_input = page2.get_by_role("textbox", name="Hasta")
                desde_input.fill(fecha_desde_value)
                hasta_input.fill(fecha_hasta_value)
                fecha_desde_ui = (desde_input.input_value() or "").strip()
                fecha_hasta_ui = (hasta_input.input_value() or "").strip()
                logger.info(
                    "Rango de fechas solicitado: desde=%s hasta=%s | cargado en UI: desde=%s hasta=%s.",
                    fecha_desde_value,
                    fecha_hasta_value,
                    fecha_desde_ui,
                    fecha_hasta_ui,
                )
                if (
                    fecha_desde_ui != fecha_desde_value
                    or fecha_hasta_ui != fecha_hasta_value
                ):
                    logger.warning(
                        "El rango de fechas visible en UI difiere del solicitado."
                    )

                # Tipo de comprobante a consultar (11: Facturas C, 13: Notas de crédito C)
                if seleccionar_tipo:
                    logger.info("Seleccionando tipo de comprobante %s.", tipo_code)
                    page2.locator("select[name=\"idTipoComprobante\"]").select_option(tipo_code)
                else:
                    logger.info("Omitiendo seleccion de tipo de comprobante (consulta general).")

                # Botón para buscar según los parámetros ingresados
                logger.info("Ejecutando búsqueda.")
                baseline_rows, _ = get_results_rows(page2)
                baseline_signature = get_results_signature(baseline_rows)
                page2.get_by_role("button", name="Buscar").click()

                logger.info("Esperando resultados estables.")
                table_rows, rows_source = wait_for_results_stable(
                    page2,
                    logger,
                    baseline_signature=baseline_signature,
                )
                if rows_source.startswith("global-fallback"):
                    logger.warning(
                        "Tabla de resultados no identificada por encabezados, usando selector global (%s).",
                        rows_source,
                    )

                total = table_rows.count()
                logger.info("Filas encontradas: %s.", total)
                if total == 0:
                    logger.info(
                        "Resumen busqueda (source=%s): filas=0, parseables=0, unicos=0, duplicados_grilla=0, omitidos_db=0, descargados=0, tipo_no_inferido=0.",
                        rows_source,
                    )
                    continue

                tipo_comp_db = TIPO_COMP_MAP.get(tipo_code) if tipo_code else None
                rows_to_process = []
                seen_in_search = set()
                parseable_count = 0
                duplicate_rows = 0
                unknown_type_count = 0
                for idx in range(total):
                    row = table_rows.nth(idx)
                    parsed = parse_result_row(row)
                    numero_comp = parsed["numero_comp"]
                    if not numero_comp:
                        logger.debug("Fila sin numero de comprobante: %s", parsed["row_text"])
                        continue
                    parseable_count += 1
                    tipo_comp = parsed["tipo_comp"] or tipo_comp_db
                    if not tipo_comp:
                        unknown_type_count += 1
                    dedupe_key = (tipo_comp or "", numero_comp)
                    if dedupe_key in seen_in_search:
                        duplicate_rows += 1
                        logger.info(
                            "Duplicado en grilla, se omite: %s.",
                            format_comp_id(tipo_comp, numero_comp),
                        )
                        continue
                    seen_in_search.add(dedupe_key)
                    rows_to_process.append((row, tipo_comp, numero_comp))

                logger.info("Comprobantes detectados: %s.", len(rows_to_process))
                skipped_existing = 0
                downloaded_count = 0
                for row, tipo_comp, numero_comp in rows_to_process:
                    comp_id = format_comp_id(tipo_comp, numero_comp)
                    ver_button = row.get_by_role("button", name="Ver")
                    if ver_button.count() == 0:
                        logger.warning("Sin boton Ver para comprobante %s.", comp_id)
                        continue

                    exists = False
                    if monotributista_id and tipo_comp:
                        exists = (
                            Factura.query.filter_by(
                                monotributista_id=monotributista_id,
                                tipo_comp=tipo_comp,
                                numero_comp=numero_comp,
                            ).first()
                            is not None
                        )
                    elif monotributista_id:
                        exists = (
                            Factura.query.filter_by(
                                monotributista_id=monotributista_id,
                                numero_comp=numero_comp,
                            ).first()
                            is not None
                        )

                    if exists:
                        skipped_existing += 1
                        logger.info("Ya existe en DB, se omite: %s.", comp_id)
                        continue

                    if remaining_downloads is not None and remaining_downloads <= 0:
                        logger.info("Se alcanzo el limite de descargas.")
                        return

                    logger.info("Descargando comprobante %s.", comp_id)
                    with page2.expect_download() as download_info:
                        ver_button.click()
                    download = download_info.value
                    upload_dir = base_dir / uuid.uuid4().hex
                    upload_dir.mkdir(parents=True, exist_ok=True)
                    target_path = upload_dir / download.suggested_filename
                    download.save_as(target_path)
                    logger.info("PDF guardado en %s", target_path)
                    enqueue_import(
                        queue,
                        monotributista_id,
                        str(target_path),
                        download.suggested_filename,
                        str(upload_root),
                    )
                    downloaded_count += 1

                    if remaining_downloads is not None:
                        remaining_downloads -= 1

                logger.info(
                    "Resumen busqueda (source=%s): filas=%s, parseables=%s, unicos=%s, duplicados_grilla=%s, omitidos_db=%s, descargados=%s, tipo_no_inferido=%s.",
                    rows_source,
                    total,
                    parseable_count,
                    len(rows_to_process),
                    duplicate_rows,
                    skipped_existing,
                    downloaded_count,
                    unknown_type_count,
                )

                page2.get_by_role("button", name="< Volver").click()
        finally:
            logger.info(
                "Cerrando sesión del monotributista %s.",
                monotributista_nombre or monotributista_label,
            )
            if page2:
                try:
                    page2.once("dialog", lambda dialog: dialog.dismiss())
                    page2.get_by_role("button", name="< Volver").click()
                    page2.get_by_role("link", name="Salir").click()
                    page2.close()
                except Exception:
                    pass
            if page1:
                try:
                    page1.locator("#userIconoChico").click()
                    page1.get_by_role("button", name=" Cerrar sesión").click()
                    page1.close()
                except Exception:
                    pass
            if RECORDING:
                trace_path = artifacts_dir / f"trace-{uuid.uuid4().hex}.zip"
                try:
                    context.tracing.stop(path=str(trace_path))
                except Exception:
                    pass
            context.close()
            browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RPA ARCA - Comprobantes en linea")
    parser.add_argument("--monotributista-id", type=int)
    parser.add_argument("--cuit")
    parser.add_argument("--clave")
    parser.add_argument("--fecha-desde")
    parser.add_argument("--fecha-hasta")
    parser.add_argument("--tipos")
    parser.add_argument("--seleccionar-tipo", action="store_true", default=None)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    tipos = [item.strip() for item in args.tipos.split(",") if item.strip()] if args.tipos else None
    with sync_playwright() as playwright:
        run(
            playwright,
            monotributista_id=args.monotributista_id,
            cuit=args.cuit,
            clave_fiscal=args.clave,
            fecha_desde=args.fecha_desde,
            fecha_hasta=args.fecha_hasta,
            tipos_comprobante=tipos,
            seleccionar_tipo=args.seleccionar_tipo,
        )
