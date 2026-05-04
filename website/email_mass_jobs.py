"""Jobs RQ para el envío automático y masivo de email.

Cadena de procesamiento:

1. ``run_email_mass_chain`` — ejecuta RPA (Playwright) para un mono y se
   reencola con los restantes. Cuando no quedan más, encola
   ``send_emails_with_pause``.
2. ``send_emails_with_pause`` — espera a que los ``FacturaImport`` del
   batch lleguen a estado terminal, arma y envía un email a cada mono
   (con su PDF), con una pausa de 10s entre cada envío. Acumula el
   resumen y encola ``send_email_mass_report``.
3. ``send_email_mass_report`` — envía el reporte HTML al admin.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from website import create_app
from website.queue import get_queue


_TERMINAL_STATUSES = {"done", "failed", "needs_review"}
_TZ_AR = ZoneInfo("America/Argentina/Cordoba")


def _run_playwright_for_mono(
    monotributista_id: int,
    fecha_desde: str | None,
    fecha_hasta: str | None,
    batch_id: str,
) -> int:
    """Lanza el subprocess de Playwright para descargar/procesar el mono.

    Devuelve el returncode del subprocess.
    """
    script_path = (
        Path(__file__).resolve().parents[1] / "playwright" / "descargar-pdf.py"
    )
    args = [
        sys.executable,
        str(script_path),
        "--monotributista-id",
        str(monotributista_id),
    ]
    if fecha_desde:
        args.extend(["--fecha-desde", fecha_desde])
    if fecha_hasta:
        args.extend(["--fecha-hasta", fecha_hasta])
    if batch_id:
        args.extend(["--batch-id", batch_id])

    env = os.environ.copy()
    env.setdefault("PLAYWRIGHT_HEADLESS", "1")
    result = subprocess.run(args, check=False, env=env)
    return result.returncode


def run_email_mass_chain(
    schedule_id: int,
    batch_id: str,
    mono_ids_pending: list[int],
    fecha_desde: str | None = None,
    fecha_hasta: str | None = None,
) -> None:
    """Procesa el RPA secuencial y al terminar encola la fase de envíos."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    logger = logging.getLogger(__name__)

    if not mono_ids_pending:
        logger.info(
            "EmailMass batch=%s: cadena RPA finalizada, encolando envíos.",
            batch_id,
        )
        app = create_app(init_db=False)
        with app.app_context():
            queue = get_queue()
            queue.enqueue(
                send_emails_with_pause,
                schedule_id,
                batch_id,
                job_timeout=3600,  # hasta 1h: polling 5min + envíos con pausa
            )
        return

    app = create_app(init_db=False)
    with app.app_context():
        current_id = mono_ids_pending[0]
        remaining = mono_ids_pending[1:]

        logger.info(
            "EmailMass batch=%s: ejecutando RPA para mono %s (quedan %d).",
            batch_id, current_id, len(remaining),
        )
        try:
            rc = _run_playwright_for_mono(
                current_id, fecha_desde, fecha_hasta, batch_id
            )
            if rc != 0:
                logger.warning(
                    "EmailMass batch=%s: RPA mono %s exit=%s.",
                    batch_id, current_id, rc,
                )
        except Exception as exc:
            logger.error(
                "EmailMass batch=%s: excepcion RPA mono %s: %s",
                batch_id, current_id, exc,
            )

        # Reencolar — incluso cuando ``remaining`` está vacío, así la
        # transición a la fase de envíos pasa por una nueva ejecución
        # del job (consistente y simple).
        queue = get_queue()
        queue.enqueue(
            run_email_mass_chain,
            schedule_id,
            batch_id,
            remaining,
            fecha_desde,
            fecha_hasta,
            job_timeout=600,
        )


def _wait_imports_terminal(batch_id: str, max_polls: int = 30) -> tuple[list, bool]:
    """Polling hasta que todos los FacturaImport del batch sean terminales.

    Devuelve (imports, timed_out).
    """
    from website.models import FacturaImport, db

    logger = logging.getLogger(__name__)
    timed_out = False
    imports: list = []
    for i in range(max_polls):
        imports = FacturaImport.query.filter_by(batch_id=batch_id).all()
        if imports and all(fi.status in _TERMINAL_STATUSES for fi in imports):
            return imports, False
        logger.info(
            "EmailMass batch=%s: esperando imports (%d/%d, %d items).",
            batch_id, i + 1, max_polls, len(imports),
        )
        time.sleep(10)
        db.session.expire_all()

    timed_out = True
    imports = FacturaImport.query.filter_by(batch_id=batch_id).all()
    return imports, timed_out


def send_emails_with_pause(schedule_id: int, batch_id: str) -> None:
    """Arma y envía emails a cada mono del schedule con pausa de 10s."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    logger = logging.getLogger(__name__)

    app = create_app(init_db=False)
    with app.app_context():
        # Imports diferidos para evitar ciclos con views.py
        from website.models import (
            EmailMassSchedule,
            EmailTemplate,
            FacturaImport,
            Monotributista,
            db,
            resolve_monos_for_email_schedule,
        )
        from website.email_utils import get_smtp_config, send_email
        from website.pdf_generator import generate_calculo_pdf
        from website.views import (
            ADAIN_SIGNATURE_HTML,
            build_calculo,
            get_last_rpa_date_label,
            obtener_topes_vigencia,
            obtener_vigencia_para_fecha,
        )
        import html as html_mod

        schedule = db.session.get(EmailMassSchedule, schedule_id)
        if not schedule:
            logger.error("EmailMassSchedule %s no encontrado.", schedule_id)
            return

        # 1. Esperar a que los imports lleguen a estado terminal
        imports, timed_out = _wait_imports_terminal(batch_id)

        # Mapa: mono_id -> True si tuvo error en este batch
        rpa_failed_by_mono: dict[int, list[str]] = defaultdict(list)
        for fi in imports:
            if fi.monotributista_id is None:
                continue
            if fi.status in ("failed", "needs_review"):
                msg = fi.error or fi.result_message or fi.status
                rpa_failed_by_mono[fi.monotributista_id].append(msg)

        # 2. Cargar template + smtp
        tpl = EmailTemplate.get_or_default()
        smtp_cfg = get_smtp_config()

        # 3. Resolver monos efectivos
        mono_ids = resolve_monos_for_email_schedule(schedule)
        monos = (
            Monotributista.query.filter(Monotributista.id.in_(mono_ids)).all()
            if mono_ids
            else []
        )

        # 4. Calcular periodo (último mes completo, igual criterio que dashboard)
        today = datetime.now(_TZ_AR).date()
        anchor_date = today.replace(day=1) - timedelta(days=1)
        anchor_date = anchor_date.replace(day=1)
        anchor_value = f"{anchor_date.year:04d}-{anchor_date.month:02d}"
        vigencia = obtener_vigencia_para_fecha(anchor_date)
        topes = obtener_topes_vigencia(vigencia)

        # Acumuladores del reporte
        skipped_no_email: list[dict] = []
        skipped_rpa_failed: list[dict] = []
        email_errors: list[dict] = []
        email_sent_ok: list[dict] = []

        if not smtp_cfg:
            logger.warning(
                "EmailMass batch=%s: sin SMTP configurado, no se pueden enviar emails.",
                batch_id,
            )
            # Igual seguimos para que el reporte refleje el problema.

        for mono in monos:
            # a) Mono sin email: skip envío, anotar
            if not (mono.email or "").strip():
                skipped_no_email.append(
                    {
                        "id": mono.id,
                        "razon_social": mono.razon_social,
                        "cuit": mono.cuit,
                    }
                )
                continue

            # b) Mono con RPA fallado en este batch: skip envío, anotar
            if mono.id in rpa_failed_by_mono:
                skipped_rpa_failed.append(
                    {
                        "id": mono.id,
                        "razon_social": mono.razon_social,
                        "cuit": mono.cuit,
                        "errors": rpa_failed_by_mono[mono.id],
                    }
                )
                continue

            # c) Si no hay SMTP, anotar como error y seguir
            if not smtp_cfg:
                email_errors.append(
                    {
                        "id": mono.id,
                        "razon_social": mono.razon_social,
                        "cuit": mono.cuit,
                        "email": mono.email,
                        "error": "SMTP no configurado",
                    }
                )
                time.sleep(10)
                continue

            # d) Armar y mandar email con PDF adjunto
            try:
                detalle = build_calculo(mono, anchor_date, topes)
                meses = list(detalle["mensual"].keys())
                periodo_label = (
                    f"{meses[0]} – {meses[-1]}" if meses else ""
                )
                generated_at = datetime.now(_TZ_AR).strftime("%d/%m/%Y %H:%M")
                last_rpa_date = get_last_rpa_date_label(mono.id)
                pdf_bytes = generate_calculo_pdf(
                    detalle,
                    mono.razon_social,
                    mono.cuit,
                    periodo_label,
                    generated_at,
                    last_rpa_date=last_rpa_date,
                )
                filename = f"calculo_{mono.cuit}_{anchor_value}.pdf"

                subject = tpl.subject.replace(
                    "{nombre}", mono.razon_social or ""
                ).replace("{periodo}", periodo_label)

                escaped_body = html_mod.escape(tpl.body_html)
                with_breaks = escaped_body.replace("\n", "<br>\n")
                body = with_breaks.replace(
                    "{nombre}", html_mod.escape(mono.razon_social or "")
                ).replace("{periodo}", html_mod.escape(periodo_label or ""))
                body = body + ADAIN_SIGNATURE_HTML

                ok, message = send_email(
                    to=mono.email,
                    subject=subject,
                    body_html=body,
                    attachments=[(filename, pdf_bytes, "application/pdf")],
                )
                if ok:
                    email_sent_ok.append(
                        {
                            "id": mono.id,
                            "razon_social": mono.razon_social,
                            "cuit": mono.cuit,
                            "email": mono.email,
                        }
                    )
                else:
                    email_errors.append(
                        {
                            "id": mono.id,
                            "razon_social": mono.razon_social,
                            "cuit": mono.cuit,
                            "email": mono.email,
                            "error": message,
                        }
                    )
            except Exception as exc:
                logger.exception(
                    "EmailMass batch=%s: excepcion al armar/mandar email mono %s",
                    batch_id, mono.id,
                )
                email_errors.append(
                    {
                        "id": mono.id,
                        "razon_social": mono.razon_social,
                        "cuit": mono.cuit,
                        "email": mono.email,
                        "error": str(exc),
                    }
                )

            # Pausa hardcoded de 10s entre envíos para no abusar SMTP
            time.sleep(10)

        summary = {
            "timed_out": timed_out,
            "periodo_label": (
                f"{anchor_date.strftime('%m/%Y')}"
                if anchor_date
                else ""
            ),
            "total_monos": len(monos),
            "skipped_no_email": skipped_no_email,
            "skipped_rpa_failed": skipped_rpa_failed,
            "email_errors": email_errors,
            "email_sent_ok": email_sent_ok,
        }

        # Encolar el reporte
        queue = get_queue()
        queue.enqueue(
            send_email_mass_report,
            schedule_id,
            batch_id,
            summary,
            job_timeout=300,
        )


def _format_status(summary: dict) -> str:
    sent = len(summary.get("email_sent_ok", []))
    errors = len(summary.get("email_errors", []))
    rpa_failed = len(summary.get("skipped_rpa_failed", []))
    no_email = len(summary.get("skipped_no_email", []))
    if sent == 0 and (errors + rpa_failed + no_email) > 0:
        return "failed"
    if errors == 0 and rpa_failed == 0 and no_email == 0:
        return "success"
    return "partial"


def send_email_mass_report(
    schedule_id: int, batch_id: str, summary: dict
) -> None:
    """Envía el reporte HTML al admin con el resumen del corrida."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    logger = logging.getLogger(__name__)

    app = create_app(init_db=False)
    with app.app_context():
        from website.models import EmailMassSchedule, db
        from website.email_utils import get_smtp_config, send_email
        from website.views import ADAIN_SIGNATURE_HTML

        schedule = db.session.get(EmailMassSchedule, schedule_id)
        if not schedule:
            logger.error(
                "EmailMassSchedule %s no encontrado, no se envia reporte.",
                schedule_id,
            )
            return

        status = _format_status(summary)
        schedule.last_run_at = datetime.now(_TZ_AR)
        schedule.last_run_status = status
        db.session.commit()

        if not schedule.send_report:
            logger.info(
                "EmailMassSchedule '%s': send_report=False, salto reporte.",
                schedule.name,
            )
            return

        smtp_cfg = get_smtp_config()
        if not smtp_cfg:
            logger.warning(
                "EmailMassSchedule '%s': sin SMTP, no se envia reporte.",
                schedule.name,
            )
            return

        report_email = (schedule.report_email or "").strip() or smtp_cfg.from_email
        if not report_email:
            logger.warning(
                "EmailMassSchedule '%s': sin email destino para reporte.",
                schedule.name,
            )
            return

        now = datetime.now(_TZ_AR)
        sent_count = len(summary.get("email_sent_ok", []))
        errors_count = len(summary.get("email_errors", []))
        rpa_failed_count = len(summary.get("skipped_rpa_failed", []))
        no_email_count = len(summary.get("skipped_no_email", []))
        total_count = summary.get("total_monos", 0)

        status_label = {
            "success": "Exitoso",
            "failed": "Fallido",
            "partial": "Parcial",
        }.get(status, status)

        # Tabla helper
        def _build_table(title: str, color: str, headers: list[str], rows_html: str) -> str:
            if not rows_html:
                return ""
            ths = "".join(
                f'<th style="padding:8px 12px;text-align:left;">{h}</th>'
                for h in headers
            )
            return (
                f'<h3 style="color:{color};">{title}</h3>'
                '<table style="width:100%;border-collapse:collapse;margin-bottom:16px;">'
                '<thead><tr style="background:#242c4f;color:#fff;">'
                f"{ths}</tr></thead>"
                f"<tbody>{rows_html}</tbody></table>"
            )

        def _td(value: str, color: str | None = None) -> str:
            style = "padding:6px 12px;border:1px solid #ddd;"
            if color:
                style += f"color:{color};"
            return f'<td style="{style}">{value}</td>'

        # Sin email
        rows_no_email = "".join(
            f"<tr>{_td(item['razon_social'])}{_td(item['cuit'])}</tr>"
            for item in summary.get("skipped_no_email", [])
        )
        table_no_email = _build_table(
            "Sin email (RPA OK, no enviado)",
            "#b7791f",
            ["Razon social", "CUIT"],
            rows_no_email,
        )

        # RPA fallido
        rows_rpa_failed = "".join(
            f"<tr>"
            f"{_td(item['razon_social'])}"
            f"{_td(item['cuit'])}"
            f"{_td('; '.join(item.get('errors', [])), '#9b2c2c')}"
            f"</tr>"
            for item in summary.get("skipped_rpa_failed", [])
        )
        table_rpa_failed = _build_table(
            "Errores RPA (no enviado)",
            "#9b2c2c",
            ["Razon social", "CUIT", "Error"],
            rows_rpa_failed,
        )

        # Errores SMTP
        rows_errors = "".join(
            f"<tr>"
            f"{_td(item['razon_social'])}"
            f"{_td(item['cuit'])}"
            f"{_td(item.get('email', ''))}"
            f"{_td(item.get('error', ''), '#9b2c2c')}"
            f"</tr>"
            for item in summary.get("email_errors", [])
        )
        table_errors = _build_table(
            "Errores de envío",
            "#9b2c2c",
            ["Razon social", "CUIT", "Email", "Error"],
            rows_errors,
        )

        # Enviados OK
        rows_ok = "".join(
            f"<tr>"
            f"{_td(item['razon_social'])}"
            f"{_td(item['cuit'])}"
            f"{_td(item.get('email', ''))}"
            f"</tr>"
            for item in summary.get("email_sent_ok", [])
        )
        table_ok = _build_table(
            "Enviados OK",
            "#276749",
            ["Razon social", "CUIT", "Email"],
            rows_ok,
        )

        timeout_html = ""
        if summary.get("timed_out"):
            timeout_html = (
                "<p style='color:#b7791f;'><strong>Aviso:</strong> "
                "El RPA no terminó dentro del tiempo de espera; algunos monos "
                "pueden no haber sido procesados completamente.</p>"
            )

        body_html = f"""
        <div style="font-family:Arial,sans-serif;max-width:720px;margin:0 auto;">
            <h2 style="color:#242c4f;">Reporte de envío masivo automático</h2>
            <table style="width:100%;margin-bottom:16px;">
                <tr><td><strong>Programación:</strong></td><td>{schedule.name}</td></tr>
                <tr><td><strong>Fecha ejecución:</strong></td><td>{now.strftime("%d/%m/%Y %H:%M")}</td></tr>
                <tr><td><strong>Período cubierto:</strong></td><td>{summary.get('periodo_label', '')}</td></tr>
                <tr><td><strong>Estado general:</strong></td><td>{status_label}</td></tr>
            </table>
            <p style="font-weight:bold;">
                Total: {total_count} | Enviados: {sent_count} |
                Errores SMTP: {errors_count} | Errores RPA: {rpa_failed_count} |
                Sin email: {no_email_count}
            </p>
            {timeout_html}
            {table_ok}
            {table_no_email}
            {table_rpa_failed}
            {table_errors}
            <p style="font-size:12px;color:#888;margin-top:24px;">
                Email generado automáticamente por Monitor Monotributo.
            </p>
            {ADAIN_SIGNATURE_HTML}
        </div>
        """

        subject = (
            f"Reporte de envío masivo automático - {schedule.name} - "
            f"{now.strftime('%d/%m/%Y %H:%M')}"
        )
        try:
            ok, msg = send_email(
                to=report_email,
                subject=subject,
                body_html=body_html,
            )
            if ok:
                logger.info(
                    "Reporte EmailMass '%s' enviado a %s.",
                    schedule.name, report_email,
                )
            else:
                logger.error(
                    "Error al enviar reporte EmailMass '%s': %s",
                    schedule.name, msg,
                )
        except Exception as exc:
            logger.error(
                "Excepcion al enviar reporte EmailMass '%s': %s",
                schedule.name, exc,
            )
