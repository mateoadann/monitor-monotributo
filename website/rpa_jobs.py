import logging
import os
import subprocess
import sys
from pathlib import Path

from website import create_app
from website.queue import get_queue


def run_rpa_chain(
    monotributista_ids: list[int],
    fecha_desde: str | None = None,
    fecha_hasta: str | None = None,
    tipos: list[str] | None = None,
    seleccionar_tipo: bool = False,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger(__name__)
    if not monotributista_ids:
        logger.info("Sin monotributistas para procesar.")
        return

    app = create_app(init_db=False)
    with app.app_context():
        current_id = monotributista_ids[0]
        remaining = monotributista_ids[1:]
        script_path = (
            Path(__file__).resolve().parents[1] / "playwright" / "descargar-pdf.py"
        )
        args = [sys.executable, str(script_path), "--monotributista-id", str(current_id)]
        if fecha_desde:
            args.extend(["--fecha-desde", fecha_desde])
        if fecha_hasta:
            args.extend(["--fecha-hasta", fecha_hasta])
        if tipos:
            args.extend(["--tipos", ",".join(tipos)])
        if seleccionar_tipo:
            args.append("--seleccionar-tipo")

        logger.info("Ejecutando RPA para monotributista %s.", current_id)
        env = os.environ.copy()
        env.setdefault("PLAYWRIGHT_HEADLESS", "1")
        result = subprocess.run(args, check=False, env=env)
        if result.returncode != 0:
            logger.warning("RPA finalizo con error para %s.", current_id)

        if remaining:
            logger.info("Encolando siguiente monotributista. Pendientes: %s", len(remaining))
            queue = get_queue()
            queue.enqueue(
                run_rpa_chain,
                remaining,
                fecha_desde,
                fecha_hasta,
                tipos,
                seleccionar_tipo,
            )
