"""Scheduler de tareas RPA programadas.

Hilo en background que cada minuto revisa si algún RpaSchedule debe ejecutarse,
según día de semana y hora/minuto en zona horaria America/Argentina/Cordoba.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from calendar import monthrange
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
        from website.models import RpaSchedule, Monotributista, db
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

        # Generar fechas usando lookback_days del schedule
        now = datetime.now(TZ_AR)
        from datetime import timedelta, date
        lookback_days = getattr(schedule, 'lookback_days', None) or 365
        today = now.date() if hasattr(now, 'date') else now
        fecha_hasta_dt = today
        fecha_desde_dt = today - timedelta(days=lookback_days)

        fecha_desde = fecha_desde_dt.strftime("%d/%m/%Y")
        fecha_hasta = fecha_hasta_dt.strftime("%d/%m/%Y")

        batch_id = uuid.uuid4().hex
        status = "running"

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
                schedule_id=schedule_id,
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

        schedule.last_run_at = datetime.now(TZ_AR)
        schedule.last_run_status = status
        db.session.commit()


def _matches_email_mass_schedule(schedule, now: datetime) -> bool:
    """Chequea si *now* (TZ_AR) coincide con un EmailMassSchedule (mensual).

    Si un día configurado es mayor al último día del mes, se dispara el último día.
    """
    if not schedule.is_active:
        return False
    if now.hour != schedule.hour or now.minute != schedule.minute:
        return False
    last_day = monthrange(now.year, now.month)[1]
    days = schedule.get_days_of_month()
    if not days:
        return False
    expanded: set[int] = set()
    for d in days:
        expanded.add(min(d, last_day))
    return now.day in expanded


def _run_scheduled_email_mass(app, schedule_id: int) -> None:
    """Dispara el envío masivo programado: encola la chain RPA + emails."""
    with app.app_context():
        from website.models import (
            EmailMassSchedule,
            Monotributista,
            db,
            resolve_monos_for_email_schedule,
        )
        from website.queue import get_queue
        from website.email_mass_jobs import run_email_mass_chain

        schedule = db.session.get(EmailMassSchedule, schedule_id)
        if not schedule:
            return

        mono_ids = resolve_monos_for_email_schedule(schedule)
        if not mono_ids:
            logger.info(
                "EmailMassSchedule '%s' sin monotributistas, salteo.", schedule.name
            )
            schedule.last_run_at = datetime.now(TZ_AR)
            schedule.last_run_status = "success"
            db.session.commit()
            return

        # Filtrar a IDs que efectivamente existen
        monos = Monotributista.query.filter(Monotributista.id.in_(mono_ids)).all()
        valid_ids = [m.id for m in monos]
        if not valid_ids:
            logger.warning(
                "EmailMassSchedule '%s': ningun monotributista valido.",
                schedule.name,
            )
            schedule.last_run_at = datetime.now(TZ_AR)
            schedule.last_run_status = "failed"
            db.session.commit()
            return

        from datetime import timedelta
        now = datetime.now(TZ_AR)
        lookback_days = getattr(schedule, "lookback_days", None) or 365
        today = now.date()
        fecha_hasta = today.strftime("%d/%m/%Y")
        fecha_desde = (today - timedelta(days=lookback_days)).strftime("%d/%m/%Y")

        batch_id = uuid.uuid4().hex
        status = "running"

        try:
            queue = get_queue()
            queue.enqueue(
                run_email_mass_chain,
                schedule_id,
                batch_id,
                valid_ids,
                fecha_desde,
                fecha_hasta,
                job_timeout=600,
            )
            logger.info(
                "EmailMassSchedule '%s': encolados %d monotributistas, batch=%s",
                schedule.name,
                len(valid_ids),
                batch_id,
            )
        except Exception as exc:
            logger.error(
                "EmailMassSchedule '%s': error al encolar: %s", schedule.name, exc
            )
            status = "failed"

        schedule.last_run_at = datetime.now(TZ_AR)
        schedule.last_run_status = status
        db.session.commit()


def _already_ran_this_minute(last_run_at, now: datetime) -> bool:
    """True si el schedule ya tiene last_run_at apuntando a este minuto."""
    if not last_run_at:
        return False
    return (
        last_run_at.year == now.year
        and last_run_at.month == now.month
        and last_run_at.day == now.day
        and last_run_at.hour == now.hour
        and last_run_at.minute == now.minute
    )


def _scheduler_loop(app) -> None:
    """Loop principal del scheduler. Corre cada 60 segundos."""
    logger.info("Scheduler RPA iniciado.")
    while True:
        try:
            now = datetime.now(TZ_AR)
            with app.app_context():
                from website.models import EmailMassSchedule, RpaSchedule, db

                # --- RPA semanal ---
                schedules = RpaSchedule.get_active_schedules()
                for schedule in schedules:
                    if _matches_schedule(schedule, now):
                        if _already_ran_this_minute(schedule.last_run_at, now):
                            continue
                        logger.info("Disparando schedule '%s'.", schedule.name)
                        t = threading.Thread(
                            target=_run_scheduled_rpa,
                            args=(app, schedule.id),
                            daemon=True,
                        )
                        t.start()

                # --- Email masivo mensual ---
                email_schedules = EmailMassSchedule.get_active_schedules()
                for schedule in email_schedules:
                    if _matches_email_mass_schedule(schedule, now):
                        if _already_ran_this_minute(schedule.last_run_at, now):
                            continue
                        logger.info(
                            "Disparando email mass schedule '%s'.", schedule.name
                        )
                        t = threading.Thread(
                            target=_run_scheduled_email_mass,
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
