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
    Factura,
    FacturaImport,
    Monotributista,
    User,
    Vigencia,
    db,
)
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


def build_calculo(
    monotributista: Monotributista, anchor: date, topes: list[CategoriaTope]
):
    month_totals, total = calcular_totales(monotributista, anchor)
    topes_map = topes_por_categoria(topes)
    categoria_actual = monotributista.categoria_actual
    exclusion = is_exclusion(total, topes)
    max_tope = max_tope_facturacion(topes)
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
        "margen_exclusion": margen_exclusion,
        "margen_categoria": margen_categoria,
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
    facturas_raw = Factura.query.order_by(Factura.fecha.desc()).all()
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
                "categoria_actual": item.categoria_actual.nombre if item.categoria_actual else "-",
                "categoria_corresponde": corresponde_label,
                "estado_categoria": estado,
            }
        )

    facturas = [
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
        for item in facturas_raw
    ]

    seleccionado_id = request.args.get("monotributista")
    seleccionado = None
    if seleccionado_id:
        seleccionado = db.session.get(Monotributista, int(seleccionado_id))
    if not seleccionado and monotributistas_raw:
        seleccionado = monotributistas_raw[0]
    detalle = build_calculo(seleccionado, anchor_date, topes_anchor) if seleccionado else None

    mono_form = session.pop("mono_form", None)
    open_modal = session.pop("open_modal", None)
    factura_import_logs = []
    if active_tab == "facturas":
        factura_import_logs = (
            FacturaImport.query.order_by(FacturaImport.created_at.desc())
            .all()
        )
        for item in factura_import_logs:
            item.created_at_label = format_datetime_ar(item.created_at)

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
        facturas=facturas,
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
        factura_import_logs=factura_import_logs,
        usuarios=usuarios,
        can_edit=current_user.can_edit_data(),
        can_manage_config=current_user.can_manage_config(),
        can_manage_users=current_user.can_manage_users(),
        can_run_rpa=current_user.can_run_rpa(),
    )


@main_bp.post("/monotributistas/create")
@login_required
@editor_required
def create_monotributista():
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
        flash(f"Completa los campos requeridos: {missing_label}.", "error")
        session["mono_form"] = {
            "razon_social": razon_social,
            "cuit": cuit,
            "clave_fiscal": clave_fiscal,
            "categoria_actual_id": categoria_actual_id or "",
        }
        session["open_modal"] = "monotributista"
        return redirect(url_for("main.dashboard", tab="monotributistas"))
    if not is_valid_cuit(cuit):
        flash("El CUIT debe tener 11 digitos y solo numeros.", "error")
        session["mono_form"] = {
            "razon_social": razon_social,
            "cuit": cuit,
            "clave_fiscal": clave_fiscal,
            "categoria_actual_id": categoria_actual_id or "",
        }
        session["open_modal"] = "monotributista"
        return redirect(url_for("main.dashboard", tab="monotributistas"))
    existing = Monotributista.query.filter_by(cuit=cuit).first()
    if existing:
        flash("El CUIT ingresado ya existe.", "error")
        session["mono_form"] = {
            "razon_social": razon_social,
            "cuit": cuit,
            "clave_fiscal": clave_fiscal,
            "categoria_actual_id": categoria_actual_id or "",
        }
        session["open_modal"] = "monotributista"
        return redirect(url_for("main.dashboard", tab="monotributistas"))

    monotributista = Monotributista(
        razon_social=razon_social,
        cuit=cuit,
        clave_fiscal=clave_fiscal,
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
                monotributista.razon_social = razon_social
                monotributista.cuit = cuit
                monotributista.clave_fiscal = clave_fiscal
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
    logs = (
        FacturaImport.query.order_by(FacturaImport.created_at.desc())
        .limit(500)
        .all()
    )
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
    batch_id = uuid.uuid4().hex
    queue = get_queue()
    queue.enqueue(
        run_rpa_chain,
        ids,
        fecha_desde,
        fecha_hasta,
        tipos if seleccionar_tipo else None,
        seleccionar_tipo,
        batch_id,
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
