import logging
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import Playwright, sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import sqlite3

CUIT = "20442030147"
CLAVE_FISCAL = "vokqu0-qamqet-Jy1jar"
CUIT_LUCIANO = "20278955770"
CLAVE_LUCIANO = "Luciano2208"
FECHA_DESDE = "01/12/2025"
FECHA_HASTA = "31/01/2026"
TIPOS_COMPROBANTE = ["11", "13"]
TIPO_COMP_MAP = {"11": "C", "13": "NCC"}
MAX_DESCARGAS = None


def run(playwright: Playwright) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger(__name__)
    download_dir = Path("downloads") / CUIT
    download_dir.mkdir(parents=True, exist_ok=True)

    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()

    db_url = os.environ.get("DATABASE_URL")
    db_path = PROJECT_ROOT / "monitor.db"
    if db_url and db_url.startswith("sqlite:///"):
        db_path = Path(db_url.replace("sqlite:///", "", 1))
    elif db_url and not db_url.startswith("sqlite:"):
        logger.warning("DATABASE_URL no es sqlite, se omite verificacion de duplicados.")
        db_path = None

    db_conn = None
    if db_path and db_path.exists():
        db_conn = sqlite3.connect(str(db_path))
        db_conn.row_factory = sqlite3.Row
    elif db_path:
        logger.warning("No se encontro la base sqlite en %s.", db_path)

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
        page1.get_by_role("combobox", name="Buscador").click()
        page1.get_by_role("combobox", name="Buscador").fill("comprobantes en linea")
        with page1.expect_popup() as page2_info:
            page1.get_by_role("link", name="Comprobantes en línea Sistema").click()

        page2 = page2_info.value
        page2.wait_for_load_state("domcontentloaded")
        page2.get_by_role("button").click()
        page2.get_by_role("button", name="Consultas").click()

        monotributista_id = None
        if db_conn:
            row = db_conn.execute(
                "SELECT id FROM monotributista WHERE cuit = ? LIMIT 1",
                (CUIT,),
            ).fetchone()
            monotributista_id = row["id"] if row else None
            if not monotributista_id:
                logger.warning(
                    "No se encontro monotributista en la DB para CUIT %s.", CUIT
                )

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
                match = re.search(r"\b\d{4}-\d{8}\b", row_text)
                if not match:
                    logger.debug("Fila sin numero de comprobante: %s", row_text)
                    continue
                rows_with_numero.append((row, match.group(0)))

            logger.info("Comprobantes detectados: %s.", len(rows_with_numero))
            for row, numero_comp in rows_with_numero:
                ver_button = row.get_by_role("button", name="Ver")
                if ver_button.count() == 0:
                    logger.warning("Sin boton Ver para comprobante %s.", numero_comp)
                    continue

                exists = False
                if db_conn and monotributista_id and tipo_comp_db:
                    exists = (
                        db_conn.execute(
                            """
                            SELECT 1
                            FROM factura
                            WHERE monotributista_id = ?
                              AND tipo_comp = ?
                              AND numero_comp = ?
                            LIMIT 1
                            """,
                            (monotributista_id, tipo_comp_db, numero_comp),
                        ).fetchone()
                        is not None
                    )
                elif db_conn and monotributista_id:
                    exists = (
                        db_conn.execute(
                            """
                            SELECT 1
                            FROM factura
                            WHERE monotributista_id = ?
                              AND numero_comp = ?
                            LIMIT 1
                            """,
                            (monotributista_id, numero_comp),
                        ).fetchone()
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
                target_path = download_dir / download.suggested_filename
                download.save_as(target_path)
                logger.info("PDF guardado en %s", target_path)

                if remaining_downloads is not None:
                    remaining_downloads -= 1

            page2.get_by_role("button", name="< Volver").click()
    finally:
        context.close()
        browser.close()
        if db_conn:
            db_conn.close()


with sync_playwright() as playwright:
    run(playwright)
