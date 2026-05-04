import logging
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from website import create_app
from website.queue import get_queue


def run_rpa_chain(
    monotributista_ids: list[int],
    fecha_desde: str | None = None,
    fecha_hasta: str | None = None,
    tipos: list[str] | None = None,
    seleccionar_tipo: bool = False,
    batch_id: str | None = None,
    schedule_id: int | None = None,
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
        if batch_id:
            args.extend(["--batch-id", batch_id])

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
                batch_id,
                schedule_id=schedule_id,
                job_timeout=600,
            )
        elif schedule_id is not None:
            logger.info(
                "Cadena RPA finalizada. Encolando reporte para schedule %s, batch %s.",
                schedule_id, batch_id,
            )
            queue = get_queue()
            queue.enqueue(
                send_rpa_schedule_report,
                schedule_id,
                batch_id,
                job_timeout=600,
            )


_TERMINAL_STATUSES = {"done", "failed", "needs_review"}
_TZ_AR = ZoneInfo("America/Argentina/Cordoba")


def send_rpa_schedule_report(schedule_id: int, batch_id: str) -> None:
    """Wait for all FacturaImport records to reach terminal status, then send report."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger(__name__)

    app = create_app(init_db=False)
    with app.app_context():
        from website.models import RpaSchedule, FacturaImport, Monotributista, db
        from website.email_utils import send_email, get_smtp_config

        # --- Poll until all imports reach terminal status ---
        timed_out = False
        max_polls = 30  # 30 * 10s = 5 min
        for i in range(max_polls):
            imports = FacturaImport.query.filter_by(batch_id=batch_id).all()
            if imports and all(fi.status in _TERMINAL_STATUSES for fi in imports):
                break
            logger.info(
                "Esperando imports del batch %s (%d/%d polls, %d imports).",
                batch_id, i + 1, max_polls, len(imports),
            )
            time.sleep(10)
            # Expire cached objects so next query gets fresh data
            db.session.expire_all()
        else:
            timed_out = True
            imports = FacturaImport.query.filter_by(batch_id=batch_id).all()

        # --- Load schedule ---
        schedule = db.session.get(RpaSchedule, schedule_id)
        if not schedule:
            logger.error("Schedule %s no encontrado, no se envia reporte.", schedule_id)
            return

        # --- Gather results grouped by monotributista ---
        by_mono: dict[int | None, list[FacturaImport]] = defaultdict(list)
        for fi in imports:
            by_mono[fi.monotributista_id].append(fi)

        # Determine success / failure per monotributista
        successful_mono_ids: list[int] = []
        failed_monos: list[dict] = []  # {id, razon_social, cuit, errors}

        if not imports:
            # No records at all — everything failed (Playwright crashed)
            status = "failed"
        else:
            for mono_id, fi_list in by_mono.items():
                has_error = any(
                    fi.status in ("failed", "needs_review") for fi in fi_list
                )
                if has_error:
                    errors = [
                        fi.error or fi.result_message or fi.status
                        for fi in fi_list
                        if fi.status in ("failed", "needs_review")
                    ]
                    failed_monos.append({
                        "id": mono_id,
                        "errors": errors,
                    })
                else:
                    if mono_id is not None:
                        successful_mono_ids.append(mono_id)

            total = len(by_mono)
            failed_count = len(failed_monos)
            if failed_count == 0:
                status = "success"
            elif failed_count == total:
                status = "failed"
            else:
                status = "partial"

        # --- Update schedule ---
        schedule.last_run_at = datetime.now(_TZ_AR)
        schedule.last_run_status = status
        db.session.commit()

        # --- Send email ---
        if not schedule.send_report:
            return

        smtp_cfg = get_smtp_config()
        if not smtp_cfg:
            logger.warning("No hay SMTP configurado, no se envia reporte de schedule.")
            return

        report_email = schedule.report_email or smtp_cfg.from_email
        if not report_email:
            logger.warning("Sin email destino para reporte de schedule '%s'.", schedule.name)
            return

        # Load monotributista info for failed ones
        failed_mono_ids = [fm["id"] for fm in failed_monos if fm["id"] is not None]
        mono_map: dict[int, Monotributista] = {}
        if failed_mono_ids:
            monos = Monotributista.query.filter(
                Monotributista.id.in_(failed_mono_ids)
            ).all()
            mono_map = {m.id: m for m in monos}

        # Enrich failed_monos with razon_social / cuit
        for fm in failed_monos:
            m = mono_map.get(fm["id"])
            fm["razon_social"] = m.razon_social if m else f"ID {fm['id']}"
            fm["cuit"] = m.cuit if m else "-"

        now = datetime.now(_TZ_AR)
        lookback_days = getattr(schedule, "lookback_days", None) or 365
        from datetime import timedelta
        today = now.date()
        fecha_hasta = today.strftime("%d/%m/%Y")
        fecha_desde = (today - timedelta(days=lookback_days)).strftime("%d/%m/%Y")

        status_label = {
            "success": "Exitoso",
            "failed": "Fallido",
            "partial": "Parcial",
        }.get(status, status)

        success_count = len(successful_mono_ids)

        # --- Build email HTML ---
        # Success line
        if success_count > 0:
            success_html = (
                f"<p style='color:#276749;font-weight:bold;'>"
                f"{success_count} monotributista{'s' if success_count != 1 else ''} "
                f"procesado{'s' if success_count != 1 else ''} exitosamente.</p>"
            )
        else:
            success_html = ""

        # Error table
        error_table_html = ""
        if failed_monos:
            rows = ""
            for fm in failed_monos:
                error_detail = "; ".join(fm["errors"])
                rows += (
                    f"<tr>"
                    f"<td style='padding:6px 12px;border:1px solid #ddd;'>{fm['razon_social']}</td>"
                    f"<td style='padding:6px 12px;border:1px solid #ddd;'>{fm['cuit']}</td>"
                    f"<td style='padding:6px 12px;border:1px solid #ddd;color:#9b2c2c;'>{error_detail}</td>"
                    f"</tr>"
                )
            error_table_html = f"""
            <h3 style="color:#9b2c2c;">Monotributistas con errores</h3>
            <table style="width:100%;border-collapse:collapse;margin-bottom:16px;">
                <thead>
                    <tr style="background:#242c4f;color:#fff;">
                        <th style="padding:8px 12px;text-align:left;">Razon social</th>
                        <th style="padding:8px 12px;text-align:left;">CUIT</th>
                        <th style="padding:8px 12px;text-align:left;">Error</th>
                    </tr>
                </thead>
                <tbody>
                    {rows}
                </tbody>
            </table>"""

        # No imports at all
        no_imports_html = ""
        if not imports:
            no_imports_html = (
                "<p style='color:#9b2c2c;'><strong>Error:</strong> "
                "No se generaron registros de importacion. "
                "Es posible que el proceso RPA haya fallado antes de descargar facturas.</p>"
            )

        # Timeout warning
        timeout_html = ""
        if timed_out:
            timeout_html = (
                "<p style='color:#b7791f;'><strong>Aviso:</strong> "
                "El reporte se genero antes de que todos los PDFs terminaran de procesarse. "
                "Algunos resultados pueden estar incompletos.</p>"
            )

        body_html = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
            <h2 style="color:#242c4f;">Reporte de actualizacion masiva programada</h2>
            <table style="width:100%;margin-bottom:16px;">
                <tr>
                    <td><strong>Actualizacion masiva:</strong></td>
                    <td>{schedule.name}</td>
                </tr>
                <tr>
                    <td><strong>Fecha ejecucion:</strong></td>
                    <td>{now.strftime("%d/%m/%Y %H:%M")}</td>
                </tr>
                <tr>
                    <td><strong>Periodo procesado:</strong></td>
                    <td>{fecha_desde} - {fecha_hasta}</td>
                </tr>
                <tr>
                    <td><strong>Estado general:</strong></td>
                    <td>{status_label}</td>
                </tr>
            </table>
            {timeout_html}
            {no_imports_html}
            {success_html}
            {error_table_html}
            <p style="margin-top:16px;">
                Ingresa al dashboard para ver el detalle completo.
            </p>
            <p style="font-size:12px;color:#888;">
                Este email fue generado automaticamente por Monitor Monotributo.
            </p>
        </div>
        """

        subject = f"Reporte de actualizacion masiva - {schedule.name} - {now.strftime('%d/%m/%Y %H:%M')}"
        try:
            ok, msg = send_email(
                to=report_email,
                subject=subject,
                body_html=body_html,
            )
            if ok:
                logger.info("Reporte de schedule '%s' enviado a %s.", schedule.name, report_email)
            else:
                logger.error("Error al enviar reporte de schedule '%s': %s", schedule.name, msg)
        except Exception as exc:
            logger.error("Excepcion enviando reporte de schedule '%s': %s", schedule.name, exc)
