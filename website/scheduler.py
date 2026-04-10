"""Scheduler de tareas RPA programadas.

Hilo en background que cada minuto revisa si algún RpaSchedule debe ejecutarse,
según día de semana y hora/minuto en zona horaria America/Argentina/Cordoba.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

TZ_AR = ZoneInfo("America/Argentina/Cordoba")

_scheduler_started = False
_lock = threading.Lock()


def _matches_schedule(schedule, now: datetime) -> bool:
    """Chequea si *now* (ya en TZ_AR) coincide con el schedule."""
    if now.hour != schedule.hour or now.minute != schedule.minute:
        return False
    # isoweekday(): lunes=1 … domingo=7
    current_dow = str(now.isoweekday())
    allowed_days = [d.strip() for d in schedule.day_of_week.split(",") if d.strip()]
    return current_dow in allowed_days


def _run_scheduled_rpa(app, schedule_id: int) -> None:
    """Ejecuta el proceso RPA para un schedule y envía reporte por email."""
    with app.app_context():
        from website.models import RpaSchedule, Monotributista, SmtpConfig, db
        from website.queue import get_queue
        from website.rpa_jobs import run_rpa_chain

        schedule = db.session.get(RpaSchedule, schedule_id)
        if not schedule:
            return

        mono_ids = schedule.get_monotributista_ids()
        if not mono_ids:
            logger.info("Schedule '%s' sin monotributistas, salteo.", schedule.name)
            schedule.last_run_at = datetime.now(TZ_AR)
            schedule.last_run_status = "success"
            db.session.commit()
            return

        # Verificar que los monotributistas existen
        monos = Monotributista.query.filter(Monotributista.id.in_(mono_ids)).all()
        valid_ids = [m.id for m in monos]
        if not valid_ids:
            logger.warning("Schedule '%s': ningun monotributista valido.", schedule.name)
            schedule.last_run_at = datetime.now(TZ_AR)
            schedule.last_run_status = "failed"
            db.session.commit()
            return

        # Generar fechas: últimos 12 meses completos
        now = datetime.now(TZ_AR)
        # fecha_hasta = último día del mes anterior
        first_of_month = now.replace(day=1)
        from datetime import timedelta
        last_day_prev = first_of_month - timedelta(days=1)
        # fecha_desde = primer día de hace 11 meses desde last_day_prev
        month = last_day_prev.month
        year = last_day_prev.year
        start_month = month - 11
        start_year = year
        if start_month <= 0:
            start_month += 12
            start_year -= 1
        from datetime import date
        fecha_desde_dt = date(start_year, start_month, 1)
        fecha_hasta_dt = last_day_prev.date() if hasattr(last_day_prev, 'date') else last_day_prev

        fecha_desde = fecha_desde_dt.strftime("%d/%m/%Y")
        fecha_hasta = fecha_hasta_dt.strftime("%d/%m/%Y")

        batch_id = uuid.uuid4().hex
        status = "success"
        error_msg = None

        try:
            queue = get_queue()
            queue.enqueue(
                run_rpa_chain,
                valid_ids,
                fecha_desde,
                fecha_hasta,
                None,   # tipos (default)
                False,  # seleccionar_tipo
                batch_id,
                job_timeout=600,
            )
            logger.info(
                "Schedule '%s': encolados %d monotributistas, batch=%s",
                schedule.name,
                len(valid_ids),
                batch_id,
            )
        except Exception as exc:
            logger.error("Schedule '%s': error al encolar: %s", schedule.name, exc)
            status = "failed"
            error_msg = str(exc)

        schedule.last_run_at = datetime.now(TZ_AR)
        schedule.last_run_status = status
        db.session.commit()

        # Enviar reporte por email
        if schedule.send_report:
            _send_schedule_report(
                app, schedule, monos, valid_ids, batch_id,
                fecha_desde, fecha_hasta, status, error_msg,
            )


def _send_schedule_report(
    app, schedule, monos, valid_ids, batch_id,
    fecha_desde, fecha_hasta, status, error_msg,
) -> None:
    """Envía email con resumen del proceso programado."""
    with app.app_context():
        from website.email_utils import send_email, get_smtp_config
        from website.models import SmtpConfig

        smtp_cfg = get_smtp_config()
        if not smtp_cfg:
            logger.warning("No hay SMTP configurado, no se envia reporte de schedule.")
            return

        report_email = schedule.report_email or smtp_cfg.from_email
        if not report_email:
            logger.warning("Sin email destino para reporte de schedule '%s'.", schedule.name)
            return

        now = datetime.now(TZ_AR)
        mono_map = {m.id: m for m in monos}

        rows_html = ""
        for mid in valid_ids:
            m = mono_map.get(mid)
            name = m.razon_social if m else f"ID {mid}"
            cuit = m.cuit if m else "-"
            rows_html += (
                f"<tr><td style='padding:6px 12px;border:1px solid #ddd;'>{name}</td>"
                f"<td style='padding:6px 12px;border:1px solid #ddd;'>{cuit}</td>"
                f"<td style='padding:6px 12px;border:1px solid #ddd;'>Encolado</td></tr>"
            )

        status_label = {
            "success": "Exitoso",
            "failed": "Fallido",
            "partial": "Parcial",
        }.get(status, status)

        error_section = ""
        if error_msg:
            error_section = (
                f"<p style='color:#9b2c2c;'><strong>Error:</strong> {error_msg}</p>"
            )

        body_html = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
            <h2 style="color:#242c4f;">Reporte de proceso RPA programado</h2>
            <table style="width:100%;margin-bottom:16px;">
                <tr>
                    <td><strong>Programacion:</strong></td>
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
                    <td><strong>Estado:</strong></td>
                    <td>{status_label}</td>
                </tr>
                <tr>
                    <td><strong>Monotributistas:</strong></td>
                    <td>{len(valid_ids)}</td>
                </tr>
            </table>
            {error_section}
            <h3 style="color:#242c4f;">Detalle</h3>
            <table style="width:100%;border-collapse:collapse;margin-bottom:16px;">
                <thead>
                    <tr style="background:#242c4f;color:#fff;">
                        <th style="padding:8px 12px;text-align:left;">Razon social</th>
                        <th style="padding:8px 12px;text-align:left;">CUIT</th>
                        <th style="padding:8px 12px;text-align:left;">Estado</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
            </table>
            <p style="font-size:12px;color:#888;">
                Este email fue generado automaticamente por Monitor Monotributo.
            </p>
        </div>
        """

        subject = f"Reporte RPA - {schedule.name} - {now.strftime('%d/%m/%Y %H:%M')}"
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


def _scheduler_loop(app) -> None:
    """Loop principal del scheduler. Corre cada 60 segundos."""
    logger.info("Scheduler RPA iniciado.")
    while True:
        try:
            now = datetime.now(TZ_AR)
            with app.app_context():
                from website.models import RpaSchedule, db

                schedules = RpaSchedule.get_active_schedules()
                for schedule in schedules:
                    if _matches_schedule(schedule, now):
                        # Evitar doble ejecución: si ya corrió en este minuto, saltear
                        if schedule.last_run_at:
                            last_run_local = schedule.last_run_at
                            if (
                                last_run_local.year == now.year
                                and last_run_local.month == now.month
                                and last_run_local.day == now.day
                                and last_run_local.hour == now.hour
                                and last_run_local.minute == now.minute
                            ):
                                continue
                        logger.info("Disparando schedule '%s'.", schedule.name)
                        # Lanzar en thread separado para no bloquear el loop
                        t = threading.Thread(
                            target=_run_scheduled_rpa,
                            args=(app, schedule.id),
                            daemon=True,
                        )
                        t.start()
        except Exception:
            logger.exception("Error en scheduler loop.")

        # Dormir hasta el inicio del próximo minuto
        elapsed = time.time() % 60
        time.sleep(60 - elapsed + 0.5)


def start_scheduler(app) -> None:
    """Inicia el scheduler si no fue iniciado aún. Seguro para reloader de Flask."""
    global _scheduler_started
    with _lock:
        if _scheduler_started:
            return
        _scheduler_started = True

    t = threading.Thread(target=_scheduler_loop, args=(app,), daemon=True)
    t.start()
    logger.info("Scheduler thread lanzado.")
