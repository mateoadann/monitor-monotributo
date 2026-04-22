from __future__ import annotations

import csv
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation
import os
import io
import uuid

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_login import current_user, login_required
from rq import Retry
from sqlalchemy import func, or_
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from website.auth_utils import admin_required, editor_required
from website.models import (
    Categoria,
    CategoriaTope,
    EmailTemplate,
    Factura,
    FacturaImport,
    Monotributista,
    RpaSchedule,
    SmtpConfig,
    User,
    Vigencia,
    db,
)
from website.email_utils import encrypt_password, decrypt_password, get_smtp_config, send_email
from website.pdf_generator import generate_calculo_pdf
from website.pdf_jobs import cleanup_pdf_path, handle_import_failure, process_factura_import
from website.queue import get_queue
from website.rpa_jobs import run_rpa_chain

main_bp = Blueprint("main", __name__)

MONTH_LABELS = [
    "Ene",
    "Feb",
    "Mar",
    "Abr",
    "May",
    "Jun",
    "Jul",
    "Ago",
    "Sep",
    "Oct",
    "Nov",
    "Dic",
]

CATEGORY_CODES = [chr(code) for code in range(ord("A"), ord("K") + 1)]
RPA_TIPOS_DEFAULT = ["11", "12", "13", "15", "19", "20", "21"]
CREDIT_NOTE_TYPES = {"NC", "NCC", "NCE"}
MONOTRIBUTISTA_IMPORT_REQUIRED_HEADERS = (
    "razon_social",
    "cuit",
    "clave_fiscal",
    "categoria_actual",
)


def parse_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_decimal(value: str | None):
    if not value:
        return None
    cleaned = value.strip()
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def format_datetime_ar(value: datetime | None) -> str:
    if not value:
        return ""
    argentina_tz = ZoneInfo("America/Argentina/Cordoba")
    try:
        return value.astimezone(argentina_tz).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return value.strftime("%d/%m/%Y %H:%M")


def split_numero_comp(value: str | None) -> tuple[str, str]:
    if not value:
        return "", ""
    if "-" in value:
        punto_venta, nro = value.split("-", 1)
    else:
        punto_venta, nro = value[:5], value[5:]

    def normalize(part: str) -> str:
        if not part:
            return ""
        if not part.isdigit():
            return part
        trimmed = part.lstrip("0")
        return trimmed if trimmed else "0"

    return normalize(punto_venta), normalize(nro)


def normalize_cuit(value: str | None) -> str:
    return (value or "").strip()


def is_valid_cuit(cuit: str) -> bool:
    return cuit.isdigit() and len(cuit) == 11


def normalize_csv_header(value: str | None) -> str:
    return (value or "").strip().lower().replace(" ", "_")


def load_monotributistas_csv(uploaded_file) -> list[dict[str, str]]:
    raw_bytes = uploaded_file.read()
    if not raw_bytes:
        raise ValueError("El archivo CSV esta vacio.")

    try:
        content = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            content = raw_bytes.decode("latin-1")
        except UnicodeDecodeError as exc:
            raise ValueError(
                "No se pudo leer el archivo CSV. Usa UTF-8 o Latin-1."
            ) from exc

    if not content.strip():
        raise ValueError("El archivo CSV no contiene filas.")

    sample = "\n".join(content.splitlines()[:5])
    delimiter = ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
        delimiter = dialect.delimiter
    except csv.Error:
        if sample.count(";") > sample.count(","):
            delimiter = ";"

    reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError("No se detectaron encabezados en el CSV.")

    header_map = {
        normalize_csv_header(header): header
        for header in reader.fieldnames
        if normalize_csv_header(header)
    }
    missing_headers = [
        header
        for header in MONOTRIBUTISTA_IMPORT_REQUIRED_HEADERS
        if header not in header_map
    ]
    if missing_headers:
        missing_label = ", ".join(missing_headers)
        raise ValueError(f"Faltan columnas requeridas en CSV: {missing_label}.")

    rows: list[dict[str, str]] = []
    for line_no, row in enumerate(reader, start=2):
        if not row:
            continue
        parsed = {
            key: (row.get(header_map[key]) or "").strip()
            for key in MONOTRIBUTISTA_IMPORT_REQUIRED_HEADERS
        }
        if not any(parsed.values()):
            continue
        parsed["line_no"] = str(line_no)
        rows.append(parsed)

    if not rows:
        raise ValueError("El CSV no tiene filas de datos para importar.")

    return rows


def format_date(value: date | None) -> str:
    if not value:
        return ""
    return value.strftime("%d/%m/%Y")


def format_currency(value: Decimal | None) -> str:
    if value is None:
        return ""
    value = value.quantize(Decimal("0.01"))
    sign = "-" if value < 0 else ""
    value = abs(value)
    whole = int(value)
    cents = int((value - whole) * 100)
    whole_str = f"{whole:,}".replace(",", ".")
    return f"{sign}$ {whole_str},{cents:02d}"


def format_decimal_input(value: Decimal | None) -> str:
    if value is None:
        return ""
    value = value.quantize(Decimal("0.01"))
    sign = "-" if value < 0 else ""
    value = abs(value)
    whole = int(value)
    cents = int((value - whole) * 100)
    whole_str = f"{whole:,}".replace(",", ".")
    return f"{sign}{whole_str},{cents:02d}"


def last_12_months(anchor: date):
    months = []
    year = anchor.year
    month = anchor.month
    for _ in range(12):
        months.append((year, month))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return list(reversed(months))


def parse_anchor(value: str | None):
    if not value:
        return None
    try:
        year_str, month_str = value.split("-")
        return date(int(year_str), int(month_str), 1)
    except (ValueError, TypeError):
        return None


def ensure_categorias() -> list[Categoria]:
    existentes = {categoria.nombre: categoria for categoria in Categoria.query.all()}
    categorias = []
    for index, codigo in enumerate(CATEGORY_CODES, start=1):
        categoria = existentes.get(codigo)
        if not categoria:
            categoria = Categoria(
                nombre=codigo,
                orden=index,
                tope_facturacion=Decimal("0.00"),
            )
            db.session.add(categoria)
        else:
            categoria.orden = index
        categorias.append(categoria)
    db.session.flush()
    return categorias


def obtener_vigencia_para_fecha(anchor: date) -> Vigencia | None:
    if not anchor:
        return None
    return (
        Vigencia.query.filter(
            Vigencia.fecha_desde <= anchor,
            or_(Vigencia.fecha_hasta.is_(None), Vigencia.fecha_hasta >= anchor),
        )
        .order_by(Vigencia.fecha_desde.desc())
        .first()
    )


def obtener_topes_vigencia(vigencia: Vigencia | None) -> list[CategoriaTope]:
    if not vigencia:
        return []
    return (
        CategoriaTope.query.join(Categoria)
        .filter(CategoriaTope.vigencia_id == vigencia.id)
        .order_by(Categoria.orden)
        .all()
    )


def categoria_por_total(total: Decimal, topes: list[CategoriaTope]):
    if not topes:
        return None
    if all(tope.tope_facturacion == 0 for tope in topes):
        return None
    total = total.quantize(Decimal("0.01"))
    topes_sorted = sorted(topes, key=lambda tope: tope.tope_facturacion)
    for tope in topes_sorted:
        if total <= tope.tope_facturacion:
            return tope.categoria
    return topes_sorted[-1].categoria


def max_tope_facturacion(topes: list[CategoriaTope]):
    if not topes:
        return None
    if all(tope.tope_facturacion == 0 for tope in topes):
        return None
    return max(topes, key=lambda tope: tope.tope_facturacion).tope_facturacion


def is_exclusion(total: Decimal, topes: list[CategoriaTope]) -> bool:
    max_tope = max_tope_facturacion(topes)
    if max_tope is None:
        return False
    total = total.quantize(Decimal("0.01"))
    return total > max_tope


def topes_por_categoria(topes: list[CategoriaTope]) -> dict[int, CategoriaTope]:
    return {tope.categoria_id: tope for tope in topes}


def calcular_totales(monotributista: Monotributista, anchor: date):
    months = last_12_months(anchor)
    month_totals = OrderedDict()
    months_lookup = {}
    for year, month in months:
        label = f"{MONTH_LABELS[month - 1]} {year}"
        month_totals[label] = Decimal("0.00")
        months_lookup[(year, month)] = label

    facturas = Factura.query.filter_by(monotributista_id=monotributista.id).all()
    for factura in facturas:
        importe_total = factura.importe_total
        tipo_comp = factura.tipo_comp.upper() if factura.tipo_comp else ""
        if tipo_comp in CREDIT_NOTE_TYPES and importe_total > 0:
            importe_total = -importe_total

        start = factura.fecha_desde or factura.fecha_hasta or factura.fecha
        end = factura.fecha_hasta or factura.fecha_desde or factura.fecha

        if not start or not end:
            continue

        if end < start:
            start, end = end, start

        if start.year == end.year and start.month == end.month:
            label = months_lookup.get((start.year, start.month))
            if label:
                month_totals[label] += importe_total
            continue

        total_days = (end - start).days + 1
        if total_days <= 0:
            label = months_lookup.get((start.year, start.month))
            if label:
                month_totals[label] += importe_total
            continue

        daily_amount = importe_total / Decimal(total_days)
        current = start
        while current <= end:
            label = months_lookup.get((current.year, current.month))
            if label:
                month_totals[label] += daily_amount
            current += timedelta(days=1)

    total = sum(month_totals.values(), Decimal("0.00"))
    return month_totals, total


def calcular_detalle_mes(
    monotributista: Monotributista, anchor: date, target_year: int, target_month: int
) -> list[dict]:
    """Return per-invoice prorated contributions for a single month.

    Each dict: {
        "numero_comp": str,
        "tipo_comp": str,
        "importe_total": str,    # "12345.67"
        "fecha_desde": str,      # "YYYY-MM-DD"
        "fecha_hasta": str,      # "YYYY-MM-DD"
        "importe_mes": str       # "3456.78" (prorated for target month)
    }
    """
    months = last_12_months(anchor)
    if (target_year, target_month) not in months:
        return []

    result = []
    facturas = Factura.query.filter_by(monotributista_id=monotributista.id).all()
    for factura in facturas:
        importe_total = factura.importe_total
        tipo_comp = factura.tipo_comp.upper() if factura.tipo_comp else ""
        if tipo_comp in CREDIT_NOTE_TYPES and importe_total > 0:
            importe_total = -importe_total

        start = factura.fecha_desde or factura.fecha_hasta or factura.fecha
        end = factura.fecha_hasta or factura.fecha_desde or factura.fecha

        if not start or not end:
            continue

        if end < start:
            start, end = end, start

        if start.year == end.year and start.month == end.month:
            if start.year == target_year and start.month == target_month:
                result.append({
                    "numero_comp": factura.numero_comp or "",
                    "tipo_comp": factura.tipo_comp or "",
                    "importe_total": f"{importe_total:.2f}",
                    "fecha_desde": start.strftime("%d/%m/%Y"),
                    "fecha_hasta": end.strftime("%d/%m/%Y"),
                    "importe_mes": f"{importe_total:.2f}",
                })
            continue

        total_days = (end - start).days + 1
        if total_days <= 0:
            if start.year == target_year and start.month == target_month:
                result.append({
                    "numero_comp": factura.numero_comp or "",
                    "tipo_comp": factura.tipo_comp or "",
                    "importe_total": f"{importe_total:.2f}",
                    "fecha_desde": start.strftime("%d/%m/%Y"),
                    "fecha_hasta": end.strftime("%d/%m/%Y"),
                    "importe_mes": f"{importe_total:.2f}",
                })
            continue

        daily_amount = importe_total / Decimal(total_days)
        days_in_month = Decimal(0)
        current = start
        while current <= end:
            if current.year == target_year and current.month == target_month:
                days_in_month += 1
            current += timedelta(days=1)

        if days_in_month > 0:
            importe_mes = daily_amount * days_in_month
            result.append({
                "numero_comp": factura.numero_comp or "",
                "tipo_comp": factura.tipo_comp or "",
                "importe_total": f"{importe_total:.2f}",
                "fecha_desde": start.strftime("%d/%m/%Y"),
                "fecha_hasta": end.strftime("%d/%m/%Y"),
                "importe_mes": f"{importe_mes:.2f}",
            })

    return result


def build_calculo(
    monotributista: Monotributista, anchor: date, topes: list[CategoriaTope]
):
    month_totals, total = calcular_totales(monotributista, anchor)
    month_keys = list(last_12_months(anchor))
    topes_map = topes_por_categoria(topes)
    categoria_actual = monotributista.categoria_actual
    exclusion = is_exclusion(total, topes)
    max_tope = max_tope_facturacion(topes)
    tope_corresponde = None
    if exclusion:
        categoria_corresponde_label = "Exclusión"
        estado = "exclusion"
        tope_corresponde_label = format_currency(max_tope) if max_tope else "-"
    else:
        categoria_corresponde = categoria_por_total(total, topes) or categoria_actual
        categoria_corresponde_label = (
            categoria_corresponde.nombre if categoria_corresponde else "-"
        )
        estado = estado_categoria(categoria_actual, categoria_corresponde)
        tope_corresponde = (
            topes_map.get(categoria_corresponde.id) if categoria_corresponde else None
        )
        tope_corresponde_label = (
            format_currency(tope_corresponde.tope_facturacion)
            if tope_corresponde
            else "-"
        )

    tope_actual = topes_map.get(categoria_actual.id) if categoria_actual else None
    margen_exclusion = (
        format_currency(max_tope - total) if max_tope is not None else "-"
    )
    margen_categoria = (
        format_currency(tope_actual.tope_facturacion - total) if tope_actual else "-"
    )
    margen_siguiente = None
    if not exclusion and estado in ("sube", "baja") and tope_corresponde:
        margen_siguiente = format_currency(tope_corresponde.tope_facturacion - total)

    return {
        "categoria_actual": categoria_actual.nombre if categoria_actual else "-",
        "categoria_corresponde": categoria_corresponde_label,
        "tope_actual": (
            format_currency(tope_actual.tope_facturacion) if tope_actual else "-"
        ),
        "tope_corresponde": tope_corresponde_label,
        "estado_categoria": estado,
        "total_12m": format_currency(total),
        "mensual": {label: format_currency(value) for label, value in month_totals.items()},
        "month_keys": month_keys,
        "margen_exclusion": margen_exclusion,
        "margen_categoria": margen_categoria,
        "margen_siguiente": margen_siguiente,
    }


def estado_categoria(actual: Categoria | None, corresponde: Categoria | None) -> str:
    if not actual or not corresponde:
        return "mantiene"
    if corresponde.orden > actual.orden:
        return "sube"
    if corresponde.orden < actual.orden:
        return "baja"
    return "mantiene"


def recalc_categoria_orden() -> None:
    changed = False
    for index, codigo in enumerate(CATEGORY_CODES, start=1):
        categoria = Categoria.query.filter_by(nombre=codigo).first()
        if not categoria:
            continue
        if categoria.orden != index:
            categoria.orden = index
            changed = True
    if changed:
        db.session.commit()


def rangos_se_solapan(
    desde: date, hasta: date | None, otro_desde: date, otro_hasta: date | None
) -> bool:
    fin = hasta or date.max
    otro_fin = otro_hasta or date.max
    return desde <= otro_fin and otro_desde <= fin


def validar_vigencia_sin_solapamiento(
    fecha_desde: date,
    fecha_hasta: date | None,
    vigencia_id: int | None = None,
) -> bool:
    query = Vigencia.query
    if vigencia_id:
        query = query.filter(Vigencia.id != vigencia_id)
    for vigencia in query.all():
        if rangos_se_solapan(
            fecha_desde,
            fecha_hasta,
            vigencia.fecha_desde,
            vigencia.fecha_hasta,
        ):
            return False
    return True


@main_bp.route("/")
@login_required
def dashboard():
    active_tab = request.args.get("tab", "monotributistas")
    anchor_param = request.args.get("anchor")
    anchor_date = parse_anchor(anchor_param) or date.today().replace(day=1)
    anchor_cutoff = (anchor_date.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    anchor_cutoff_label = anchor_cutoff.strftime("%d/%m/%y")
    anchor_value = f"{anchor_date.year:04d}-{anchor_date.month:02d}"
    monotributistas_raw = Monotributista.query.order_by(Monotributista.razon_social).all()
    categorias_raw = Categoria.query.order_by(Categoria.orden).all()
    vigencia_anchor = obtener_vigencia_para_fecha(anchor_date)
    topes_anchor = obtener_topes_vigencia(vigencia_anchor)

    anchor_actual = date.today().replace(day=1) - timedelta(days=1)
    vigencia_actual = obtener_vigencia_para_fecha(anchor_actual)
    topes_actual = obtener_topes_vigencia(vigencia_actual)

    mono_anchor_default_date = anchor_actual.replace(day=1)
    mono_anchor_param = request.args.get("mono_anchor") if active_tab == "monotributistas" else None
    mono_anchor_date = parse_anchor(mono_anchor_param) or mono_anchor_default_date
    mono_anchor_cutoff = (mono_anchor_date.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    mono_anchor_cutoff_label = mono_anchor_cutoff.strftime("%d/%m/%y")
    mono_anchor_value = f"{mono_anchor_date.year:04d}-{mono_anchor_date.month:02d}"
    mono_anchor_default_value = (
        f"{mono_anchor_default_date.year:04d}-{mono_anchor_default_date.month:02d}"
    )
    mono_anchor_default_cutoff = (
        (mono_anchor_default_date.replace(day=1) + timedelta(days=32))
        .replace(day=1)
        - timedelta(days=1)
    )
    mono_anchor_default_cutoff_label = mono_anchor_default_cutoff.strftime("%d/%m/%y")
    vigencia_mono = obtener_vigencia_para_fecha(mono_anchor_date)
    topes_mono = obtener_topes_vigencia(vigencia_mono)
    monotributistas = []
    count_sube = 0
    count_baja = 0
    count_exclusion = 0
    for item in monotributistas_raw:
        _, total_actual = calcular_totales(item, mono_anchor_date)
        exclusion = is_exclusion(total_actual, topes_mono)
        if exclusion:
            corresponde_label = "Exclusión"
            estado = "exclusion"
            count_exclusion += 1
        else:
            corresponde = categoria_por_total(total_actual, topes_mono) or item.categoria_actual
            corresponde_label = (
                corresponde.nombre if corresponde else (item.categoria_actual.nombre if item.categoria_actual else "-")
            )
            estado = estado_categoria(item.categoria_actual, corresponde)
            if estado == "sube":
                count_sube += 1
            elif estado == "baja":
                count_baja += 1
        monotributistas.append(
            {
                "id": item.id,
                "razon_social": item.razon_social,
                "cuit": item.cuit,
                "clave_fiscal": item.clave_fiscal,
                "email": item.email or "",
                "categoria_actual": item.categoria_actual.nombre if item.categoria_actual else "-",
                "categoria_corresponde": corresponde_label,
                "estado_categoria": estado,
            }
        )

    seleccionado_id = request.args.get("monotributista")
    seleccionado = None
    if seleccionado_id:
        seleccionado = db.session.get(Monotributista, int(seleccionado_id))
    if not seleccionado and monotributistas_raw:
        seleccionado = monotributistas_raw[0]
    detalle = build_calculo(seleccionado, anchor_date, topes_anchor) if seleccionado else None

    smtp_cfg = SmtpConfig.get_config()
    smtp_config_dict = (
        {
            "host": smtp_cfg.host,
            "port": smtp_cfg.port,
            "username": smtp_cfg.username,
            "use_tls": smtp_cfg.use_tls,
            "from_email": smtp_cfg.from_email,
            "from_name": smtp_cfg.from_name,
            "has_config": True,
        }
        if smtp_cfg
        else None
    )

    email_tpl = EmailTemplate.get_or_default()
    email_template_dict = {
        "subject": email_tpl.subject,
        "body_html": email_tpl.body_html,
    }

    rpa_schedules = []
    if current_user.can_manage_config():
        rpa_schedules = [
            {
                "id": s.id,
                "name": s.name,
                "day_of_week": s.day_of_week,
                "hour": s.hour,
                "minute": s.minute,
                "monotributista_ids": s.get_monotributista_ids(),
                "lookback_days": s.lookback_days,
                "send_report": s.send_report,
                "report_email": s.report_email or "",
                "is_active": s.is_active,
                "last_run_at": s.last_run_at.strftime("%d/%m/%Y %H:%M") if s.last_run_at else None,
                "last_run_status": s.last_run_status,
            }
            for s in RpaSchedule.query.order_by(RpaSchedule.created_at.desc()).all()
        ]

    mono_form = session.pop("mono_form", None)
    open_modal = session.pop("open_modal", None)
    usuarios = []
    if current_user.can_manage_users():
        usuarios = [
            {
                "id": u.id,
                "username": u.username,
                "nombre": u.nombre,
                "role": u.role,
                "role_label": User.ROLE_LABELS.get(u.role, u.role),
                "is_active": u.is_active_user,
            }
            for u in User.query.order_by(User.username).all()
        ]

    return render_template(
        "dashboard.html",
        monotributistas=monotributistas,
        categorias=categorias_raw,
        vigencias_table=[
            {
                "id": vigencia.id,
                "vigencia_desde": format_date(vigencia.fecha_desde),
                "vigencia_hasta": format_date(vigencia.fecha_hasta) or "Sin fin",
            }
            for vigencia in Vigencia.query.order_by(Vigencia.fecha_desde.desc()).all()
        ],
        monotributistas_select=monotributistas_raw,
        seleccionado_id=seleccionado.id if seleccionado else None,
        seleccionado_label=seleccionado.razon_social if seleccionado else "",
        detalle=detalle,
        active_tab=active_tab,
        anchor_value=anchor_value,
        anchor_cutoff_label=anchor_cutoff_label,
        mono_anchor_value=mono_anchor_value,
        mono_anchor_default_value=mono_anchor_default_value,
        mono_anchor_cutoff_label=mono_anchor_cutoff_label,
        mono_anchor_default_cutoff_label=mono_anchor_default_cutoff_label,
        count_sube=count_sube,
        count_baja=count_baja,
        count_exclusion=count_exclusion,
        mono_form=mono_form,
        open_modal=open_modal,
        usuarios=usuarios,
        can_edit=current_user.can_edit_data(),
        can_manage_config=current_user.can_manage_config(),
        can_manage_users=current_user.can_manage_users(),
        can_run_rpa=current_user.can_run_rpa(),
        smtp_config=smtp_config_dict,
        email_template=email_template_dict,
        has_smtp=smtp_cfg is not None,
        rpa_schedules=rpa_schedules,
    )


@main_bp.get("/calculo/<int:mono_id>/pdf")
@login_required
def calculo_pdf(mono_id):
    mono = db.session.get(Monotributista, mono_id)
    if not mono:
        abort(404)
    anchor_param = request.args.get("anchor")
    anchor_date = parse_anchor(anchor_param) or date.today().replace(day=1)
    anchor_value = f"{anchor_date.year:04d}-{anchor_date.month:02d}"
    vigencia = obtener_vigencia_para_fecha(anchor_date)
    topes = obtener_topes_vigencia(vigencia)
    detalle = build_calculo(mono, anchor_date, topes)
    meses = list(detalle["mensual"].keys())
    periodo_label = f"{meses[0]} – {meses[-1]}" if meses else ""
    generated_at = datetime.now(
        ZoneInfo("America/Argentina/Cordoba")
    ).strftime("%d/%m/%Y %H:%M")
    pdf_bytes = generate_calculo_pdf(
        detalle, mono.razon_social, mono.cuit, periodo_label, generated_at
    )
    filename = f"calculo_{mono.cuit}_{anchor_value}.pdf"
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@main_bp.route("/facturas/api")
@login_required
def facturas_api():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 10, type=int), 100)
    search = request.args.get("search", "").strip().lower()
    facturador_ids = request.args.getlist("facturador", type=int)
    tipo = request.args.get("tipo", "").strip()
    orden = request.args.get("orden", "fecha_desc").strip()

    query = Factura.query.join(Monotributista, isouter=True)

    if search:
        search_pattern = f"%{search}%"
        query = query.filter(
            or_(
                Monotributista.razon_social.ilike(search_pattern),
                Factura.tipo_comp.ilike(search_pattern),
                Factura.numero_comp.ilike(search_pattern),
                Factura.cuit_receptor.ilike(search_pattern),
                Factura.razon_social_receptor.ilike(search_pattern),
                Factura.concepto.ilike(search_pattern),
            )
        )

    if facturador_ids:
        query = query.filter(Factura.monotributista_id.in_(facturador_ids))

    if tipo:
        query = query.filter(Factura.tipo_comp == tipo)

    order_map = {
        "fecha_desc": Factura.fecha.desc(),
        "fecha_asc": Factura.fecha.asc(),
        "importe_desc": Factura.importe_total.desc(),
        "importe_asc": Factura.importe_total.asc(),
    }
    query = query.order_by(order_map.get(orden, Factura.fecha.desc()))

    total = query.count()
    facturas_page = query.offset((page - 1) * per_page).limit(per_page).all()

    items = []
    can_edit = current_user.can_edit_data()
    for item in facturas_page:
        items.append(
            {
                "id": item.id,
                "facturador_id": item.monotributista_id,
                "facturador": item.monotributista.razon_social if item.monotributista else "-",
                "fecha": format_date(item.fecha),
                "fecha_iso": item.fecha.isoformat(),
                "tipo": item.tipo_comp,
                "numero": item.numero_comp,
                "cuit_receptor": item.cuit_receptor or "-",
                "razon_receptor": item.razon_social_receptor or "-",
                "importe": format_currency(item.importe_total),
                "importe_raw": str(item.importe_total),
                "desde": format_date(item.fecha_desde),
                "hasta": format_date(item.fecha_hasta),
                "concepto": item.concepto or "-",
            }
        )

    total_pages = max(1, -(-total // per_page))
    return jsonify(
        {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "can_edit": can_edit,
        }
    )


@main_bp.get("/api/calculo/<int:mono_id>/mes/<int:year>/<int:month>")
@login_required
def api_calculo_mes(mono_id, year, month):
    monotributista = db.session.get(Monotributista, mono_id)
    if not monotributista:
        return jsonify([])
    anchor_param = request.args.get("anchor")
    anchor = parse_anchor(anchor_param) if anchor_param else None
    if not anchor:
        anchor = date.today().replace(day=1)
    result = calcular_detalle_mes(monotributista, anchor, year, month)
    return jsonify(result)


@main_bp.post("/monotributistas/create")
@login_required
@editor_required
def create_monotributista():
    razon_social = request.form.get("razon_social", "").strip()
    cuit = normalize_cuit(request.form.get("cuit"))
    clave_fiscal = request.form.get("clave_fiscal", "").strip()
    categoria_actual_id = request.form.get("categoria_actual_id")
    email = request.form.get("email", "").strip()

    def _mono_form_dict():
        return {
            "razon_social": razon_social,
            "cuit": cuit,
            "clave_fiscal": clave_fiscal,
            "categoria_actual_id": categoria_actual_id or "",
            "email": email,
        }

    missing = []
    if not razon_social:
        missing.append("Razon social")
    if not cuit:
        missing.append("CUIT")
    if not clave_fiscal:
        missing.append("Clave fiscal")
    if not categoria_actual_id:
        missing.append("Categoria actual")
    if missing:
        missing_label = ", ".join(missing)
        flash(f"Completa los campos requeridos: {missing_label}.", "error")
        session["mono_form"] = _mono_form_dict()
        session["open_modal"] = "monotributista"
        return redirect(url_for("main.dashboard", tab="monotributistas"))
    if not is_valid_cuit(cuit):
        flash("El CUIT debe tener 11 digitos y solo numeros.", "error")
        session["mono_form"] = _mono_form_dict()
        session["open_modal"] = "monotributista"
        return redirect(url_for("main.dashboard", tab="monotributistas"))
    existing = Monotributista.query.filter_by(cuit=cuit).first()
    if existing:
        flash("El CUIT ingresado ya existe.", "error")
        session["mono_form"] = _mono_form_dict()
        session["open_modal"] = "monotributista"
        return redirect(url_for("main.dashboard", tab="monotributistas"))

    monotributista = Monotributista(
        razon_social=razon_social,
        cuit=cuit,
        clave_fiscal=clave_fiscal,
        email=email or None,
        categoria_actual_id=int(categoria_actual_id),
        categoria_corresponde_id=int(categoria_actual_id),
    )
    db.session.add(monotributista)
    db.session.commit()
    flash("Monotributista creado.", "success")
    return redirect(url_for("main.dashboard", tab="monotributistas"))


@main_bp.post("/monotributistas/import-csv")
@login_required
@editor_required
def import_monotributistas_csv():
    csv_file = request.files.get("monotributistas_csv")
    dry_run = request.form.get("dry_run") == "1"

    if not csv_file or not csv_file.filename:
        flash("Selecciona un archivo CSV para importar.", "error")
        session["open_modal"] = "monotributistas-import"
        return redirect(url_for("main.dashboard", tab="monotributistas"))

    try:
        parsed_rows = load_monotributistas_csv(csv_file)
    except ValueError as exc:
        flash(str(exc), "error")
        session["open_modal"] = "monotributistas-import"
        return redirect(url_for("main.dashboard", tab="monotributistas"))

    categorias = Categoria.query.order_by(Categoria.orden).all()
    categorias_by_code = {categoria.nombre.upper(): categoria for categoria in categorias}

    seen_cuits = set()
    valid_rows = []
    errors = []
    for row in parsed_rows:
        line_no = row.get("line_no", "?")
        razon_social = row["razon_social"].strip()
        cuit = normalize_cuit(row["cuit"])
        clave_fiscal = row["clave_fiscal"].strip()
        categoria_code = row["categoria_actual"].strip().upper()

        row_errors = []
        if not razon_social:
            row_errors.append("razon_social vacia")
        if not cuit:
            row_errors.append("cuit vacio")
        elif not is_valid_cuit(cuit):
            row_errors.append("cuit invalido")
        if not clave_fiscal:
            row_errors.append("clave_fiscal vacia")
        categoria = categorias_by_code.get(categoria_code)
        if not categoria:
            row_errors.append("categoria_actual invalida")
        if cuit in seen_cuits:
            row_errors.append("cuit duplicado en archivo")

        if row_errors:
            errors.append(f"Fila {line_no}: {', '.join(row_errors)}")
            continue

        seen_cuits.add(cuit)
        valid_rows.append(
            {
                "razon_social": razon_social,
                "cuit": cuit,
                "clave_fiscal": clave_fiscal,
                "categoria": categoria,
            }
        )

    cuits_to_check = [row["cuit"] for row in valid_rows]
    existing_cuits = set()
    if cuits_to_check:
        existing_rows = Monotributista.query.filter(
            Monotributista.cuit.in_(cuits_to_check)
        ).all()
        existing_cuits = {item.cuit for item in existing_rows}

    created = 0
    skipped_existing = 0
    for row in valid_rows:
        if row["cuit"] in existing_cuits:
            skipped_existing += 1
            continue
        created += 1
        if dry_run:
            continue
        monotributista = Monotributista(
            razon_social=row["razon_social"],
            cuit=row["cuit"],
            clave_fiscal=row["clave_fiscal"],
            categoria_actual_id=row["categoria"].id,
            categoria_corresponde_id=row["categoria"].id,
        )
        db.session.add(monotributista)

    if dry_run:
        db.session.rollback()
    else:
        db.session.commit()

    mode_label = "Simulacion" if dry_run else "Importacion"
    flash(
        f"{mode_label} finalizada: creados={created}, omitidos_existentes={skipped_existing}, errores={len(errors)}.",
        "success",
    )

    if skipped_existing:
        flash(f"Se omitieron {skipped_existing} CUIT ya existentes.", "success")

    if errors:
        session["open_modal"] = "monotributistas-import"
        for message in errors[:8]:
            flash(message, "error")
        if len(errors) > 8:
            flash(f"... y {len(errors) - 8} errores adicionales.", "error")

    return redirect(url_for("main.dashboard", tab="monotributistas"))


@main_bp.route("/monotributistas/<int:monotributista_id>/edit", methods=["GET", "POST"])
@login_required
@editor_required
def edit_monotributista(monotributista_id):
    monotributista = Monotributista.query.get_or_404(monotributista_id)
    categorias = Categoria.query.order_by(Categoria.orden).all()
    form_values = None

    if request.method == "POST":
        razon_social = request.form.get("razon_social", "").strip()
        cuit = normalize_cuit(request.form.get("cuit"))
        clave_fiscal = request.form.get("clave_fiscal", "").strip()
        categoria_actual_id = request.form.get("categoria_actual_id")

        missing = []
        if not razon_social:
            missing.append("Razon social")
        if not cuit:
            missing.append("CUIT")
        if not clave_fiscal:
            missing.append("Clave fiscal")
        if not categoria_actual_id:
            missing.append("Categoria actual")
        if missing:
            missing_label = ", ".join(missing)
            flash(f"Faltan campos requeridos: {missing_label}.", "error")
            form_values = {
                "razon_social": razon_social,
                "cuit": cuit,
                "clave_fiscal": clave_fiscal,
                "categoria_actual_id": categoria_actual_id or "",
            }
        elif not is_valid_cuit(cuit):
            flash("El CUIT debe tener 11 digitos y solo numeros.", "error")
            form_values = {
                "razon_social": razon_social,
                "cuit": cuit,
                "clave_fiscal": clave_fiscal,
                "categoria_actual_id": categoria_actual_id or "",
            }
        else:
            existing = (
                Monotributista.query.filter(
                    Monotributista.cuit == cuit,
                    Monotributista.id != monotributista.id,
                ).first()
            )
            if existing:
                flash("El CUIT ingresado ya existe.", "error")
                form_values = {
                    "razon_social": razon_social,
                    "cuit": cuit,
                    "clave_fiscal": clave_fiscal,
                    "categoria_actual_id": categoria_actual_id or "",
                }
            else:
                email = request.form.get("email", "").strip()
                monotributista.razon_social = razon_social
                monotributista.cuit = cuit
                monotributista.clave_fiscal = clave_fiscal
                monotributista.email = email or None
                monotributista.categoria_actual_id = int(categoria_actual_id)
                db.session.commit()
                flash("Monotributista actualizado.", "success")
                return redirect(url_for("main.dashboard", tab="monotributistas"))

    return render_template(
        "edit_monotributista.html",
        monotributista=monotributista,
        categorias=categorias,
        form_values=form_values,
        form_flash=True,
    )


@main_bp.post("/monotributistas/<int:monotributista_id>/delete")
@login_required
@editor_required
def delete_monotributista(monotributista_id):
    monotributista = Monotributista.query.get_or_404(monotributista_id)
    FacturaImport.query.filter_by(monotributista_id=monotributista.id).update(
        {FacturaImport.monotributista_id: None}, synchronize_session=False
    )
    db.session.delete(monotributista)
    db.session.commit()
    flash("Monotributista eliminado.", "success")
    return redirect(url_for("main.dashboard", tab="monotributistas"))


@main_bp.post("/facturas/create")
@login_required
@editor_required
def create_factura():
    pdf_files = [item for item in request.files.getlist("pdf_file") if item and item.filename]
    has_pdf = len(pdf_files) > 0

    if has_pdf:
        monotributista_id = request.form.get("monotributista_id")
        monotributista_id = int(monotributista_id) if monotributista_id else None
        upload_batch_id = uuid.uuid4().hex

        invalid = [item.filename for item in pdf_files if not item.filename.lower().endswith(".pdf")]
        if invalid:
            db.session.add(
                FacturaImport(
                    batch_id=upload_batch_id,
                    monotributista_id=monotributista_id,
                    status="failed",
                    pdf_path="",
                    source="upload",
                    result_message="Todos los archivos deben ser PDFs.",
                    processed_at=datetime.now(timezone.utc),
                )
            )
            db.session.commit()
            flash("Todos los archivos deben ser PDFs.", "error")
            return redirect(url_for("main.dashboard", tab="facturas"))

        upload_root = current_app.config["UPLOAD_FOLDER"]
        queue = get_queue()
        enqueued = 0

        for pdf_file in pdf_files:
            upload_dir = os.path.join(upload_root, "facturas", uuid.uuid4().hex)
            os.makedirs(upload_dir, exist_ok=True)
            filename = secure_filename(pdf_file.filename) or "factura.pdf"
            pdf_path = os.path.join(upload_dir, filename)
            pdf_file.save(pdf_path)

            factura_import = FacturaImport(
                batch_id=upload_batch_id,
                monotributista_id=monotributista_id,
                status="pending",
                pdf_path=pdf_path,
                filename=filename,
                source="upload",
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
                enqueued += 1
                factura_import.result_message = "En cola para procesamiento."
            except Exception as exc:
                factura_import.status = "failed"
                factura_import.error = f"No se pudo encolar: {exc}"
                factura_import.result_message = factura_import.error
                factura_import.processed_at = datetime.now(timezone.utc)
                cleanup_pdf_path(pdf_path, upload_root)
        db.session.commit()

        if enqueued:
            flash(
                f"PDFs en cola para procesamiento: {enqueued}.",
                "success",
            )
        else:
            flash("No se pudo encolar ningun PDF.", "error")
        return redirect(url_for("main.dashboard", tab="facturas"))

    monotributista_id = request.form.get("monotributista_id")
    fecha = parse_date(request.form.get("fecha"))
    tipo_comp = request.form.get("tipo_comp", "").strip()
    tipo_comp = tipo_comp.upper()
    punto_venta = request.form.get("punto_venta", "").strip()
    nro_comp = request.form.get("nro_comp", "").strip()
    cuit_receptor = request.form.get("cuit_receptor", "").strip()
    razon_social_receptor = request.form.get("razon_social_receptor", "").strip()
    importe_total = parse_decimal(request.form.get("importe_total"))
    fecha_desde = parse_date(request.form.get("fecha_desde"))
    fecha_hasta = parse_date(request.form.get("fecha_hasta"))
    concepto = request.form.get("concepto", "").strip()
    manual_batch_id = uuid.uuid4().hex

    is_export = tipo_comp == "E"
    if (
        not monotributista_id
        or not fecha
        or not tipo_comp
        or (not punto_venta and not is_export)
        or not nro_comp
        or importe_total is None
    ):
        db.session.add(
            FacturaImport(
                batch_id=manual_batch_id,
                monotributista_id=int(monotributista_id) if monotributista_id else None,
                status="failed",
                pdf_path="",
                source="manual",
                result_message="Completa los campos requeridos para crear la factura.",
                processed_at=datetime.now(timezone.utc),
            )
        )
        db.session.commit()
        session["open_modal"] = "factura-imports"
        return redirect(url_for("main.dashboard", tab="facturas"))

    monotributista = db.session.get(Monotributista, int(monotributista_id))
    if not monotributista:
        db.session.add(
            FacturaImport(
                batch_id=manual_batch_id,
                monotributista_id=None,
                status="failed",
                pdf_path="",
                source="manual",
                result_message=(
                    "El monotributista seleccionado no existe. Carguelo antes de importar."
                ),
                processed_at=datetime.now(timezone.utc),
            )
        )
        db.session.commit()
        session["open_modal"] = "factura-imports"
        return redirect(url_for("main.dashboard", tab="facturas"))

    if tipo_comp in CREDIT_NOTE_TYPES and importe_total > 0:
        importe_total = -importe_total

    if (punto_venta and not punto_venta.isdigit()) or not nro_comp.isdigit():
        db.session.add(
            FacturaImport(
                batch_id=manual_batch_id,
                monotributista_id=int(monotributista_id) if monotributista_id else None,
                status="failed",
                pdf_path="",
                source="manual",
                result_message="El punto de venta y el numero de comprobante deben ser numericos.",
                processed_at=datetime.now(timezone.utc),
            )
        )
        db.session.commit()
        session["open_modal"] = "factura-imports"
        return redirect(url_for("main.dashboard", tab="facturas"))

    if is_export:
        punto_venta = punto_venta or "0"

    numero_comp = f"{punto_venta.zfill(5)}-{nro_comp.zfill(8)}"
    existing = Factura.query.filter_by(
        monotributista_id=int(monotributista_id),
        tipo_comp=tipo_comp,
        numero_comp=numero_comp,
    ).first()
    if existing:
        db.session.add(
            FacturaImport(
                batch_id=manual_batch_id,
                monotributista_id=int(monotributista_id),
                status="failed",
                pdf_path="",
                source="manual",
                result_message=(
                    "Ya existe una factura con ese numero y punto de venta para este monotributista."
                ),
                processed_at=datetime.now(timezone.utc),
            )
        )
        db.session.commit()
        session["open_modal"] = "factura-imports"
        return redirect(url_for("main.dashboard", tab="facturas"))

    factura = Factura(
        monotributista_id=int(monotributista_id),
        fecha=fecha,
        tipo_comp=tipo_comp,
        numero_comp=numero_comp,
        cuit_receptor=cuit_receptor,
        razon_social_receptor=razon_social_receptor,
        importe_total=importe_total,
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
        concepto=concepto,
    )
    db.session.add(factura)
    db.session.flush()
    db.session.add(
        FacturaImport(
            batch_id=manual_batch_id,
            monotributista_id=int(monotributista_id),
            status="done",
            pdf_path="",
            source="manual",
            factura_id=factura.id,
            result_message=f"Comprobante {punto_venta.zfill(5)}_{tipo_comp}_{nro_comp.zfill(8)}",
            processed_at=datetime.now(timezone.utc),
        )
    )
    db.session.commit()
    session["open_modal"] = "factura-imports"
    flash("Factura creada.", "success")
    return redirect(url_for("main.dashboard", tab="facturas"))


@main_bp.route("/facturas/<int:factura_id>/edit", methods=["GET", "POST"])
@login_required
@editor_required
def edit_factura(factura_id):
    factura = Factura.query.get_or_404(factura_id)
    monotributistas = Monotributista.query.order_by(Monotributista.razon_social).all()
    punto_venta_value, nro_comp_value = split_numero_comp(factura.numero_comp)
    if factura.tipo_comp and factura.tipo_comp.upper() == "E" and punto_venta_value == "0":
        punto_venta_value = ""

    if request.method == "POST":
        monotributista_id = request.form.get("monotributista_id")
        fecha = parse_date(request.form.get("fecha"))
        tipo_comp = request.form.get("tipo_comp", "").strip()
        tipo_comp = tipo_comp.upper()
        punto_venta = request.form.get("punto_venta", "").strip()
        nro_comp = request.form.get("nro_comp", "").strip()
        cuit_receptor = request.form.get("cuit_receptor", "").strip()
        razon_social_receptor = request.form.get("razon_social_receptor", "").strip()
        importe_total = parse_decimal(request.form.get("importe_total"))
        fecha_desde = parse_date(request.form.get("fecha_desde"))
        fecha_hasta = parse_date(request.form.get("fecha_hasta"))
        concepto = request.form.get("concepto", "").strip()

        is_export = tipo_comp == "E"
        if (
            not monotributista_id
            or not fecha
            or not tipo_comp
            or (not punto_venta and not is_export)
            or not nro_comp
            or importe_total is None
        ):
            flash("Completa los campos requeridos para editar la factura.", "error")
        else:
            if tipo_comp in CREDIT_NOTE_TYPES and importe_total > 0:
                importe_total = -importe_total
            if (punto_venta and not punto_venta.isdigit()) or not nro_comp.isdigit():
                flash(
                    "El punto de venta y el numero de comprobante deben ser numericos.",
                    "error",
                )
                return render_template(
                    "edit_factura.html",
                    factura=factura,
                    monotributistas=monotributistas,
                    importe_value=f"{factura.importe_total:.2f}",
                    punto_venta_value=punto_venta_value,
                    nro_comp_value=nro_comp_value,
                )

            if is_export:
                punto_venta = punto_venta or "0"
            numero_comp = f"{punto_venta.zfill(5)}-{nro_comp.zfill(8)}"
            existing = Factura.query.filter(
                Factura.monotributista_id == int(monotributista_id),
                Factura.tipo_comp == tipo_comp,
                Factura.numero_comp == numero_comp,
                Factura.id != factura.id,
            ).first()
            if existing:
                flash(
                    "Ya existe una factura con ese numero y punto de venta para este monotributista.",
                    "error",
                )
                return render_template(
                    "edit_factura.html",
                    factura=factura,
                    monotributistas=monotributistas,
                    importe_value=f"{factura.importe_total:.2f}",
                    punto_venta_value=punto_venta_value,
                    nro_comp_value=nro_comp_value,
                )
            factura.monotributista_id = int(monotributista_id)
            factura.fecha = fecha
            factura.tipo_comp = tipo_comp
            factura.numero_comp = numero_comp
            factura.cuit_receptor = cuit_receptor
            factura.razon_social_receptor = razon_social_receptor
            factura.importe_total = importe_total
            factura.fecha_desde = fecha_desde
            factura.fecha_hasta = fecha_hasta
            factura.concepto = concepto
            db.session.commit()
            flash("Factura actualizada.", "success")
            return redirect(url_for("main.dashboard", tab="facturas"))

    return render_template(
        "edit_factura.html",
        factura=factura,
        monotributistas=monotributistas,
        importe_value=f"{factura.importe_total:.2f}",
        punto_venta_value=punto_venta_value,
        nro_comp_value=nro_comp_value,
    )


@main_bp.post("/facturas/<int:factura_id>/delete")
@login_required
@editor_required
def delete_factura(factura_id):
    factura = Factura.query.get_or_404(factura_id)
    FacturaImport.query.filter_by(factura_id=factura.id).update(
        {"factura_id": None}
    )
    db.session.delete(factura)
    db.session.commit()
    flash("Factura eliminada.", "success")
    return redirect(url_for("main.dashboard", tab="facturas"))


@main_bp.get("/facturas/imports")
@login_required
def factura_imports():
    terminal_statuses = ["done", "failed_viewed"]
    non_done = (
        FacturaImport.query
        .filter(FacturaImport.status.notin_(terminal_statuses))
        .order_by(FacturaImport.created_at.desc())
        .all()
    )
    remaining = max(0, 500 - len(non_done))
    done_records = (
        FacturaImport.query
        .filter(FacturaImport.status.in_(terminal_statuses))
        .order_by(FacturaImport.created_at.desc())
        .limit(remaining)
        .all()
    ) if remaining > 0 else []
    logs = sorted(non_done + done_records, key=lambda x: x.created_at, reverse=True)
    items = []
    counts = {"pending": 0, "processing": 0, "done": 0, "failed": 0, "needs_review": 0}
    active_row = (
        FacturaImport.query.filter(
            FacturaImport.batch_id.isnot(None),
            FacturaImport.status.in_(["pending", "processing"]),
        )
        .order_by(FacturaImport.created_at.desc())
        .first()
    )
    active_batch_id = active_row.batch_id if active_row else None
    for item in logs:
        status = item.status or "pending"
        created_at_label = format_datetime_ar(item.created_at)
        items.append(
            {
                "id": item.id,
                "created_at": created_at_label,
                "monotributista": item.monotributista.razon_social if item.monotributista else "-",
                "source": item.source or "-",
                "status": status,
                "result": item.result_message or item.error or "-",
                "has_pdf": bool(item.pdf_path),
            }
        )
    if active_batch_id:
        batch_logs = FacturaImport.query.filter_by(batch_id=active_batch_id).all()
        for item in batch_logs:
            status = item.status or "pending"
            bucket = status if status in counts else (
                "failed" if status == "failed_viewed" else "pending"
            )
            counts[bucket] += 1
    historical_counts = {"pending": 0, "processing": 0, "done": 0, "failed": 0, "needs_review": 0}
    rows = (
        db.session.query(FacturaImport.status, func.count())
        .group_by(FacturaImport.status)
        .all()
    )
    for status, cnt in rows:
        bucket = status if status in historical_counts else "pending"
        historical_counts[bucket] += cnt
    return jsonify(
        {
            "items": items,
            "counts": counts,
            "historical_counts": historical_counts,
            "has_active": bool(active_batch_id),
            "active_batch_id": active_batch_id,
        }
    )


@main_bp.get("/facturas/imports/<int:import_id>/pdf")
@login_required
def download_import_pdf(import_id):
    factura_import = db.session.get(FacturaImport, import_id)
    if not factura_import or not factura_import.pdf_path:
        abort(404)

    pdf_full_path = os.path.abspath(factura_import.pdf_path)
    upload_folder = os.path.abspath(current_app.config["UPLOAD_FOLDER"])
    if not pdf_full_path.startswith(upload_folder):
        abort(403)

    if not os.path.isfile(pdf_full_path):
        abort(404)

    return send_file(
        pdf_full_path,
        as_attachment=True,
        download_name=factura_import.filename or os.path.basename(pdf_full_path),
    )


@main_bp.post("/facturas/imports/<int:import_id>/delete")
@login_required
@editor_required
def delete_factura_import(import_id):
    factura_import = db.session.get(FacturaImport, import_id)
    if not factura_import:
        return jsonify({"error": "Importación no encontrada."}), 404
    if factura_import.status not in ("failed", "needs_review"):
        return jsonify({
            "error": "Solo se pueden borrar importaciones con estado fallido o en revisión."
        }), 400

    cleanup_pdf_path(
        factura_import.pdf_path,
        current_app.config.get("UPLOAD_FOLDER"),
    )

    db.session.delete(factura_import)
    db.session.commit()
    return jsonify({"deleted": 1})


@main_bp.post("/facturas/imports/cleanup-failed")
@login_required
@editor_required
def cleanup_failed_imports():
    failed_imports = FacturaImport.query.filter_by(status="failed").all()
    upload_root = current_app.config.get("UPLOAD_FOLDER")
    for fi in failed_imports:
        cleanup_pdf_path(fi.pdf_path, upload_root)
        db.session.delete(fi)
    db.session.commit()
    return jsonify({"deleted": len(failed_imports)})


@main_bp.post("/vigencias/create")
@login_required
@admin_required
def create_vigencia():
    fecha_desde = parse_date(request.form.get("vigencia_desde"))
    fecha_hasta = parse_date(request.form.get("vigencia_hasta"))

    if not fecha_desde:
        flash("Completa los campos requeridos para crear la vigencia.", "error")
        return redirect(url_for("main.dashboard", tab="configuracion"))

    if fecha_hasta and fecha_hasta < fecha_desde:
        flash("La vigencia hasta debe ser posterior a la vigencia desde.", "error")
        return redirect(url_for("main.dashboard", tab="configuracion"))

    if not validar_vigencia_sin_solapamiento(fecha_desde, fecha_hasta):
        flash("La vigencia se superpone con otra existente.", "error")
        return redirect(url_for("main.dashboard", tab="configuracion"))

    categorias = ensure_categorias()
    vigencia = Vigencia(fecha_desde=fecha_desde, fecha_hasta=fecha_hasta)
    db.session.add(vigencia)
    db.session.flush()

    topes = [
        CategoriaTope(
            vigencia_id=vigencia.id,
            categoria_id=categoria.id,
            tope_facturacion=Decimal("0.00"),
        )
        for categoria in categorias
    ]
    db.session.add_all(topes)
    db.session.commit()
    recalc_categoria_orden()
    flash("Vigencia creada.", "success")
    return redirect(url_for("main.dashboard", tab="configuracion"))


@main_bp.route("/vigencias/<int:vigencia_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def edit_vigencia(vigencia_id):
    vigencia = Vigencia.query.get_or_404(vigencia_id)
    if request.method == "POST":
        fecha_desde = parse_date(request.form.get("vigencia_desde"))
        fecha_hasta = parse_date(request.form.get("vigencia_hasta"))

        if not fecha_desde:
            flash("Completa los campos requeridos para editar.", "error")
        elif fecha_hasta and fecha_hasta < fecha_desde:
            flash("La vigencia hasta debe ser posterior a la vigencia desde.", "error")
        else:
            if not validar_vigencia_sin_solapamiento(
                fecha_desde, fecha_hasta, vigencia.id
            ):
                flash("La vigencia se superpone con otra existente.", "error")
            else:
                vigencia.fecha_desde = fecha_desde
                vigencia.fecha_hasta = fecha_hasta
                db.session.commit()
                flash("Vigencia actualizada.", "success")
                return redirect(url_for("main.dashboard", tab="configuracion"))

    topes = (
        CategoriaTope.query.join(Categoria)
        .filter(CategoriaTope.vigencia_id == vigencia.id)
        .order_by(Categoria.orden)
        .all()
    )

    return render_template(
        "edit_categoria.html",
        vigencia=vigencia,
        vigencia_desde_value=vigencia.fecha_desde.isoformat(),
        vigencia_hasta_value=vigencia.fecha_hasta.isoformat() if vigencia.fecha_hasta else "",
        topes_table=[
            {
                "categoria": tope.categoria.nombre,
                "categoria_id": tope.categoria_id,
                "tope_value": format_decimal_input(tope.tope_facturacion),
            }
            for tope in topes
        ],
    )


@main_bp.post("/vigencias/<int:vigencia_id>/topes")
@login_required
@admin_required
def update_vigencia_topes(vigencia_id):
    vigencia = Vigencia.query.get_or_404(vigencia_id)
    topes = (
        CategoriaTope.query.join(Categoria)
        .filter(CategoriaTope.vigencia_id == vigencia.id)
        .order_by(Categoria.orden)
        .all()
    )
    errores = False
    for tope in topes:
        raw_value = request.form.get(f"tope_{tope.categoria_id}", "").strip()
        parsed = parse_decimal(raw_value)
        if parsed is None:
            errores = True
            break
        tope.tope_facturacion = parsed

    if errores:
        db.session.rollback()
        flash("Completa los topes con un formato valido.", "error")
        return redirect(url_for("main.edit_vigencia", vigencia_id=vigencia.id))

    db.session.commit()
    flash("Topes actualizados.", "success")
    return redirect(url_for("main.edit_vigencia", vigencia_id=vigencia.id))


@main_bp.post("/vigencias/<int:vigencia_id>/delete")
@login_required
@admin_required
def delete_vigencia(vigencia_id):
    vigencia = Vigencia.query.get_or_404(vigencia_id)
    db.session.delete(vigencia)
    db.session.commit()
    flash("Vigencia eliminada.", "success")
    return redirect(url_for("main.dashboard", tab="configuracion"))


def _find_active_rpa_conflict(queue, requested_ids: list[int]) -> list[int]:
    """
    Retorna la lista de monotributista_ids que ya tienen un job run_rpa_chain
    pending o en ejecución. Lista vacía = no hay conflicto.
    """
    from rq.registry import StartedJobRegistry

    requested_set = set(requested_ids)
    conflict_ids: set[int] = set()

    # Jobs pending en la cola
    try:
        pending_jobs = queue.jobs
    except Exception:
        pending_jobs = []

    # Jobs started (en ejecución)
    try:
        started_registry = StartedJobRegistry(queue=queue)
        started_ids = started_registry.get_job_ids()
        started_jobs = [queue.fetch_job(jid) for jid in started_ids]
        started_jobs = [j for j in started_jobs if j is not None]
    except Exception:
        started_jobs = []

    for job in list(pending_jobs) + list(started_jobs):
        try:
            func_name = job.func_name or ""
        except Exception:
            continue
        if not func_name.endswith("run_rpa_chain"):
            continue
        try:
            job_args = job.args or ()
        except Exception:
            continue
        if not job_args:
            continue
        job_ids = job_args[0] or []
        if not isinstance(job_ids, (list, tuple)):
            continue
        for jid in job_ids:
            if jid in requested_set:
                conflict_ids.add(jid)

    return sorted(conflict_ids)


@main_bp.post("/rpa/run")
@login_required
@editor_required
def run_rpa():
    fecha_desde_raw = request.form.get("rpa_fecha_desde", "").strip()
    fecha_hasta_raw = request.form.get("rpa_fecha_hasta", "").strip()
    group = request.form.get("rpa_group", "all")
    categorias_raw = request.form.getlist("rpa_categorias")
    monotributistas_raw = request.form.getlist("rpa_monotributistas")
    tipos = [item for item in request.form.getlist("rpa_tipos") if item]

    categorias = [int(item) for item in categorias_raw if item.isdigit()]
    seleccionados = [int(item) for item in monotributistas_raw if item.isdigit()]

    missing = []
    if not fecha_desde_raw:
        missing.append("fecha desde")
    if not fecha_hasta_raw:
        missing.append("fecha hasta")
    if missing:
        missing_label = " y ".join(missing)
        return jsonify({"error": f"Completa {missing_label}."}), 400

    def format_rpa_date(value: str) -> str | None:
        if not value:
            return None
        parsed = parse_date(value)
        if parsed:
            return parsed.strftime("%d/%m/%Y")
        return None

    fecha_desde = format_rpa_date(fecha_desde_raw)
    fecha_hasta = format_rpa_date(fecha_hasta_raw)
    if not fecha_desde or not fecha_hasta:
        return jsonify({"error": "Completa las fechas con un formato valido."}), 400

    seleccionar_tipo = False
    if tipos and set(tipos) != set(RPA_TIPOS_DEFAULT):
        seleccionar_tipo = True
    elif not tipos:
        tipos = RPA_TIPOS_DEFAULT

    query = Monotributista.query
    if group == "all":
        if not seleccionados:
            return jsonify({"error": "Selecciona al menos un monotributista."}), 400
        query = query.filter(Monotributista.id.in_(seleccionados))
    elif group == "categorias":
        if not categorias:
            return jsonify({"error": "Selecciona al menos una categoria."}), 400
        query = query.filter(Monotributista.categoria_actual_id.in_(categorias))
    elif group == "seleccion":
        if not seleccionados:
            return jsonify({"error": "Selecciona al menos un monotributista."}), 400
        query = query.filter(Monotributista.id.in_(seleccionados))

    monotributistas = query.order_by(Monotributista.razon_social).all()
    if not monotributistas:
        return jsonify({"error": "No hay monotributistas para procesar."}), 400

    ids = [item.id for item in monotributistas]
    queue = get_queue()

    conflict_ids = _find_active_rpa_conflict(queue, ids)
    if conflict_ids:
        conflict_monos = Monotributista.query.filter(
            Monotributista.id.in_(conflict_ids)
        ).all()
        nombres = ", ".join(m.razon_social for m in conflict_monos) or "otro monotributista"
        return jsonify({
            "error": (
                f"Ya hay una descarga RPA en curso o en cola para: {nombres}. "
                "Esperá a que termine antes de ejecutar otra."
            )
        }), 409

    batch_id = uuid.uuid4().hex
    queue.enqueue(
        run_rpa_chain,
        ids,
        fecha_desde,
        fecha_hasta,
        tipos if seleccionar_tipo else None,
        seleccionar_tipo,
        batch_id,
        job_timeout=600,
    )
    return jsonify({"queued": len(ids), "batch_id": batch_id})


# --- Gestion de usuarios ---


@main_bp.post("/usuarios/create")
@login_required
@admin_required
def create_usuario():
    username = request.form.get("username", "").strip().lower()
    nombre = request.form.get("nombre", "").strip()
    password = request.form.get("password", "").strip()
    role = request.form.get("role", "visor")

    if not username or not password:
        flash("Usuario y contrasena son requeridos.", "error")
        return redirect(url_for("main.dashboard", tab="usuarios"))

    if role not in User.ROLES:
        flash("Rol invalido.", "error")
        return redirect(url_for("main.dashboard", tab="usuarios"))

    if User.query.filter_by(username=username).first():
        flash("El nombre de usuario ya existe.", "error")
        return redirect(url_for("main.dashboard", tab="usuarios"))

    user = User(
        username=username,
        nombre=nombre,
        password_hash=generate_password_hash(password),
        role=role,
    )
    db.session.add(user)
    db.session.commit()
    flash("Usuario creado.", "success")
    return redirect(url_for("main.dashboard", tab="usuarios"))


@main_bp.route("/usuarios/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def edit_usuario(user_id):
    user = User.query.get_or_404(user_id)

    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        role = request.form.get("role", user.role)
        is_active = request.form.get("is_active") == "1"
        new_password = request.form.get("password", "").strip()

        if role not in User.ROLES:
            flash("Rol invalido.", "error")
        elif user.id == current_user.id and role != "admin":
            flash("No puedes quitarte el rol de administrador.", "error")
        else:
            user.nombre = nombre
            user.role = role
            user.is_active_user = is_active
            if new_password:
                user.password_hash = generate_password_hash(new_password)
            db.session.commit()
            flash("Usuario actualizado.", "success")
            return redirect(url_for("main.dashboard", tab="usuarios"))

    return render_template(
        "edit_usuario.html",
        user=user,
        roles=User.ROLES,
        role_labels=User.ROLE_LABELS,
    )


@main_bp.post("/usuarios/<int:user_id>/delete")
@login_required
@admin_required
def delete_usuario(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("No puedes eliminarte a ti mismo.", "error")
        return redirect(url_for("main.dashboard", tab="usuarios"))
    db.session.delete(user)
    db.session.commit()
    flash("Usuario eliminado.", "success")
    return redirect(url_for("main.dashboard", tab="usuarios"))


@main_bp.route("/perfil/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_password = request.form.get("current_password", "").strip()
        new_password = request.form.get("new_password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()

        if not check_password_hash(current_user.password_hash, current_password):
            flash("La contrasena actual es incorrecta.", "error")
        elif not new_password or len(new_password) < 4:
            flash("La nueva contrasena debe tener al menos 4 caracteres.", "error")
        elif new_password != confirm_password:
            flash("Las contrasenas no coinciden.", "error")
        else:
            current_user.password_hash = generate_password_hash(new_password)
            db.session.commit()
            flash("Contrasena actualizada.", "success")
            return redirect(url_for("main.dashboard"))

    return render_template("change_password.html")


# ---------------------------------------------------------------------------
# SMTP Configuration
# ---------------------------------------------------------------------------


@main_bp.post("/smtp/save")
@login_required
@admin_required
def smtp_save():
    data = request.get_json(silent=True) or {}
    host = (data.get("host") or "").strip()
    port = data.get("port")
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    use_tls = bool(data.get("use_tls", True))
    from_email = (data.get("from_email") or "").strip()
    from_name = (data.get("from_name") or "").strip()

    if not host:
        return jsonify({"error": "El host SMTP es obligatorio."}), 400
    try:
        port = int(port)
        if port < 1 or port > 65535:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "El puerto debe ser un número entre 1 y 65535."}), 400
    if not username:
        return jsonify({"error": "El usuario SMTP es obligatorio."}), 400
    if not from_email or "@" not in from_email:
        return jsonify({"error": "El email remitente debe contener '@'."}), 400

    config = SmtpConfig.get_config()
    if config:
        config.host = host
        config.port = port
        config.username = username
        if password:
            config.password_encrypted = encrypt_password(password)
        config.use_tls = use_tls
        config.from_email = from_email
        config.from_name = from_name
    else:
        if not password:
            return jsonify({"error": "La contraseña es obligatoria para la configuración inicial."}), 400
        config = SmtpConfig(
            host=host,
            port=port,
            username=username,
            password_encrypted=encrypt_password(password),
            use_tls=use_tls,
            from_email=from_email,
            from_name=from_name,
        )
        db.session.add(config)

    db.session.commit()
    return jsonify({"ok": True})


@main_bp.post("/smtp/test")
@login_required
@admin_required
def smtp_test():
    data = request.get_json(silent=True) or {}
    test_to = (data.get("test_to") or "").strip()
    if not test_to or "@" not in test_to:
        return jsonify({"error": "Ingrese un email de destino válido."}), 400

    try:
        ok, message = send_email(
            to=test_to,
            subject="Test - Monitor Monotributo",
            body_html="<p>Email de prueba enviado correctamente desde Monitor Monotributo.</p>",
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 500

    if ok:
        return jsonify({"ok": True, "message": f"Email de prueba enviado a {test_to}"})
    return jsonify({"error": message}), 500


@main_bp.post("/smtp/delete")
@login_required
@admin_required
def smtp_delete():
    SmtpConfig.query.delete()
    db.session.commit()
    return jsonify({"ok": True})


@main_bp.post("/email-template/save")
@login_required
@admin_required
def email_template_save():
    data = request.get_json(silent=True) or {}
    subject = (data.get("subject") or "").strip()
    body_html = (data.get("body_html") or "").strip()

    if not subject:
        return jsonify({"error": "El asunto es obligatorio."}), 400
    if not body_html:
        return jsonify({"error": "El cuerpo del email es obligatorio."}), 400

    tpl = EmailTemplate.get_template()
    if tpl:
        tpl.subject = subject
        tpl.body_html = body_html
        tpl.updated_at = datetime.now(timezone.utc)
    else:
        tpl = EmailTemplate(subject=subject, body_html=body_html)
        db.session.add(tpl)

    db.session.commit()
    return jsonify({"ok": True})


@main_bp.post("/envio-masivo")
@login_required
@admin_required
def envio_masivo():
    data = request.get_json(silent=True) or {}
    mono_ids = data.get("monotributista_ids", [])
    anchor_param = data.get("anchor")

    if not mono_ids:
        return jsonify({"error": "Seleccioná al menos un monotributista."}), 400

    anchor_date = parse_anchor(anchor_param) or date.today().replace(day=1)
    anchor_value = f"{anchor_date.year:04d}-{anchor_date.month:02d}"
    vigencia = obtener_vigencia_para_fecha(anchor_date)
    topes = obtener_topes_vigencia(vigencia)

    tpl = EmailTemplate.get_or_default()

    sent = 0
    failed = 0
    errors = []
    skipped = 0

    for mono_id in mono_ids:
        mono = db.session.get(Monotributista, int(mono_id))
        if not mono:
            errors.append({"id": mono_id, "error": "Monotributista no encontrado"})
            failed += 1
            continue
        if not mono.email:
            skipped += 1
            continue

        try:
            detalle = build_calculo(mono, anchor_date, topes)
            meses = list(detalle["mensual"].keys())
            periodo_label = f"{meses[0]} – {meses[-1]}" if meses else ""
            generated_at = datetime.now(
                ZoneInfo("America/Argentina/Cordoba")
            ).strftime("%d/%m/%Y %H:%M")

            pdf_bytes = generate_calculo_pdf(
                detalle, mono.razon_social, mono.cuit, periodo_label, generated_at
            )
            filename = f"calculo_{mono.cuit}_{anchor_value}.pdf"

            subject = tpl.subject.replace("{nombre}", mono.razon_social).replace(
                "{periodo}", periodo_label
            )
            body = tpl.body_html.replace("{nombre}", mono.razon_social).replace(
                "{periodo}", periodo_label
            )

            ok, message = send_email(
                to=mono.email,
                subject=subject,
                body_html=body,
                attachments=[(filename, pdf_bytes, "application/pdf")],
            )

            if ok:
                sent += 1
            else:
                failed += 1
                errors.append(
                    {"id": mono.id, "razon_social": mono.razon_social, "error": message}
                )
        except Exception as e:
            failed += 1
            errors.append(
                {"id": mono.id, "razon_social": mono.razon_social, "error": str(e)}
            )

    return jsonify({
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
        "errors": errors,
    })


# --- Programacion RPA ---


@main_bp.post("/rpa-schedule/save")
@login_required
@admin_required
def rpa_schedule_save():
    import json as _json

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    days = data.get("day_of_week", "")
    hour = data.get("hour")
    minute = data.get("minute")
    mono_ids = data.get("monotributista_ids", [])
    lookback_days = data.get("lookback_days")
    send_report = bool(data.get("send_report", True))
    report_email = (data.get("report_email") or "").strip()
    schedule_id = data.get("id")

    if not name:
        return jsonify({"error": "El nombre es obligatorio."}), 400
    if not days:
        return jsonify({"error": "Selecciona al menos un dia."}), 400
    if hour is None or minute is None:
        return jsonify({"error": "Completa la hora y los minutos."}), 400

    try:
        hour = int(hour)
        minute = int(minute)
    except (ValueError, TypeError):
        return jsonify({"error": "Hora y minutos deben ser numeros."}), 400

    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return jsonify({"error": "Hora (0-23) y minutos (0-59) invalidos."}), 400

    if lookback_days is not None:
        try:
            lookback_days = int(lookback_days)
            if lookback_days < 1 or lookback_days > 730:
                return jsonify({"error": "Los dias anteriores deben estar entre 1 y 730."}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "Los dias anteriores deben ser un numero entero."}), 400
    else:
        lookback_days = 365

    if not isinstance(mono_ids, list) or not mono_ids:
        return jsonify({"error": "Selecciona al menos un monotributista."}), 400

    mono_ids = [int(mid) for mid in mono_ids if str(mid).isdigit()]

    if schedule_id:
        schedule = db.session.get(RpaSchedule, int(schedule_id))
        if not schedule:
            return jsonify({"error": "Programacion no encontrada."}), 404
    else:
        schedule = RpaSchedule()
        db.session.add(schedule)

    schedule.name = name
    schedule.day_of_week = days if isinstance(days, str) else ",".join(str(d) for d in days)
    schedule.hour = hour
    schedule.minute = minute
    schedule.set_monotributista_ids(mono_ids)
    schedule.lookback_days = lookback_days
    schedule.send_report = send_report
    schedule.report_email = report_email
    db.session.commit()

    return jsonify({"ok": True, "id": schedule.id})


@main_bp.post("/rpa-schedule/delete")
@login_required
@admin_required
def rpa_schedule_delete():
    data = request.get_json(silent=True) or {}
    schedule_id = data.get("id")
    if not schedule_id:
        return jsonify({"error": "Falta id."}), 400

    schedule = db.session.get(RpaSchedule, int(schedule_id))
    if not schedule:
        return jsonify({"error": "Programacion no encontrada."}), 404

    db.session.delete(schedule)
    db.session.commit()
    return jsonify({"ok": True})


@main_bp.post("/rpa-schedule/toggle")
@login_required
@admin_required
def rpa_schedule_toggle():
    data = request.get_json(silent=True) or {}
    schedule_id = data.get("id")
    if not schedule_id:
        return jsonify({"error": "Falta id."}), 400

    schedule = db.session.get(RpaSchedule, int(schedule_id))
    if not schedule:
        return jsonify({"error": "Programacion no encontrada."}), 404

    schedule.is_active = not schedule.is_active
    db.session.commit()
    return jsonify({"ok": True, "is_active": schedule.is_active})


@main_bp.post("/rpa-schedule/run-now")
@login_required
@admin_required
def rpa_schedule_run_now():
    data = request.get_json(silent=True) or {}
    schedule_id = data.get("id")
    if not schedule_id:
        return jsonify({"error": "Falta id."}), 400

    schedule = db.session.get(RpaSchedule, int(schedule_id))
    if not schedule:
        return jsonify({"error": "Programacion no encontrada."}), 404

    import threading
    from website.scheduler import _run_scheduled_rpa
    from flask import current_app

    app = current_app._get_current_object()
    t = threading.Thread(
        target=_run_scheduled_rpa,
        args=(app, schedule.id),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True, "message": "Proceso iniciado."})
