import logging
import os
import re
import sys
import uuid
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
CLAVE_FISCAL = "vokqu0-qamqet-Jy1jar"
CUIT_LUCIANO = "20278955770"
CLAVE_LUCIANO = "Luciano2208"
FECHA_DESDE = "01/12/2025"
FECHA_HASTA = "31/01/2026"
TIPOS_COMPROBANTE = ["11", "12", "13", "15", "19", "20", "21"]
TIPO_COMP_MAP = {
    "11": "C",
    "12": "NDC",
    "13": "NCC",
    "15": "RC",
    "19": "E",
    "20": "NDE",
    "21": "NCE",
}
MAX_DESCARGAS = None
HEADLESS = os.environ.get("PLAYWRIGHT_HEADLESS", "1") != "0"


def parse_numero_comp(row_text: str) -> str | None:
    match = re.search(r"\b(\d{4,5})-(\d{8})\b", row_text)
    if not match:
        return None
    return build_numero_comp(match.group(1), match.group(2))


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


def run(playwright: Playwright) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger(__name__)

    app = create_app(init_db=False)
    upload_root = Path(app.config["UPLOAD_FOLDER"])
    upload_root.mkdir(parents=True, exist_ok=True)
    base_dir = upload_root / "facturas"
    base_dir.mkdir(parents=True, exist_ok=True)
    redis_url = app.config.get("REDIS_URL", "redis://localhost:6379/0")
    queue = Queue("facturas", connection=Redis.from_url(redis_url))

    browser = playwright.chromium.launch(headless=HEADLESS)
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()

    with app.app_context():
        monotributista = Monotributista.query.filter_by(cuit=CUIT_LUCIANO).first()
        monotributista_id = monotributista.id if monotributista else None
        if not monotributista_id:
            logger.warning(
                "No se encontro monotributista en la DB para CUIT %s.", CUIT_LUCIANO
            )

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
            page1.get_by_role("spinbutton").fill(CUIT_LUCIANO)
            page1.get_by_role("button", name="Siguiente").click()

            # Clave fiscal del monotributista a consultar:
            logger.info("Ingresando clave fiscal.")
            page1.get_by_role("textbox", name="TU CLAVE").fill(CLAVE_LUCIANO)
            page1.get_by_role("button", name="Ingresar").click()

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
            page2.get_by_role("button").click()
            page2.get_by_role("button", name="Consultas").click()

            remaining_downloads = MAX_DESCARGAS
            for tipo_code in TIPOS_COMPROBANTE:
                # Rango de fechas a consultar:
                logger.info("Cargando rango de fechas.")
                page2.get_by_role("textbox", name="Desde").fill(FECHA_DESDE)
                page2.get_by_role("textbox", name="Hasta").fill(FECHA_HASTA)

                # Tipo de comprobante a consultar (11: Facturas C, 13: Notas de crédito C)
                logger.info("Seleccionando tipo de comprobante %s.", tipo_code)
                page2.locator("select[name=\"idTipoComprobante\"]").select_option(tipo_code)

                # Botón para buscar según los parámetros ingresados
                logger.info("Ejecutando búsqueda.")
                page2.get_by_role("button", name="Buscar").click()

                logger.info("Esperando resultados.")
                table_rows = page2.locator("table tbody tr")
                if table_rows.count() == 0:
                    table_rows = page2.locator("tr").filter(has=page2.locator("td"))

                total = table_rows.count()
                logger.info("Filas encontradas: %s.", total)
                if total == 0:
                    continue

                tipo_comp_db = TIPO_COMP_MAP.get(tipo_code)
                rows_with_numero = []
                for idx in range(total):
                    row = table_rows.nth(idx)
                    cells = row.locator("td").all_text_contents()
                    row_text = " ".join(item.strip() for item in cells if item.strip())
                    numero_comp = parse_numero_comp(row_text)
                    if not numero_comp:
                        logger.debug("Fila sin numero de comprobante: %s", row_text)
                        continue
                    rows_with_numero.append((row, numero_comp))

                logger.info("Comprobantes detectados: %s.", len(rows_with_numero))
                for row, numero_comp in rows_with_numero:
                    ver_button = row.get_by_role("button", name="Ver")
                    if ver_button.count() == 0:
                        logger.warning("Sin boton Ver para comprobante %s.", numero_comp)
                        continue

                    exists = False
                    if monotributista_id and tipo_comp_db:
                        exists = (
                            Factura.query.filter_by(
                                monotributista_id=monotributista_id,
                                tipo_comp=tipo_comp_db,
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
                        logger.info("Ya existe en DB, se omite: %s.", numero_comp)
                        continue

                    if remaining_downloads is not None and remaining_downloads <= 0:
                        logger.info("Se alcanzo el limite de descargas.")
                        return

                    logger.info("Descargando comprobante %s.", numero_comp)
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

                    if remaining_downloads is not None:
                        remaining_downloads -= 1

                page2.get_by_role("button", name="< Volver").click()
        finally:
            context.close()
            browser.close()


with sync_playwright() as playwright:
    run(playwright)
