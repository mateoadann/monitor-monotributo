from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import os
import uuid

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import login_required
from rq import Retry
from sqlalchemy import or_
from werkzeug.utils import secure_filename

from website.models import (
    Categoria,
    CategoriaTope,
    Factura,
    FacturaImport,
    Monotributista,
    Vigencia,
    db,
)
from website.pdf_jobs import process_factura_import
from website.queue import get_queue

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


def split_numero_comp(value: str | None) -> tuple[str, str]:
    if not value:
        return "", ""
    if "-" in value:
        punto_venta, nro = value.split("-", 1)
    else:
        punto_venta, nro = value[:4], value[4:]

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
        if (
            factura.tipo_comp
            and factura.tipo_comp.upper().startswith("NC")
            and importe_total > 0
        ):
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
    categoria_corresponde = categoria_por_total(total, topes) or categoria_actual
    estado = estado_categoria(categoria_actual, categoria_corresponde)

    tope_actual = topes_map.get(categoria_actual.id) if categoria_actual else None
    tope_corresponde = (
        topes_map.get(categoria_corresponde.id) if categoria_corresponde else None
    )

    return {
        "categoria_actual": categoria_actual.nombre if categoria_actual else "-",
        "categoria_corresponde": categoria_corresponde.nombre if categoria_corresponde else "-",
        "tope_actual": (
            format_currency(tope_actual.tope_facturacion) if tope_actual else "-"
        ),
        "tope_corresponde": (
            format_currency(tope_corresponde.tope_facturacion)
            if tope_corresponde
            else "-"
        ),
        "estado_categoria": estado,
        "total_12m": format_currency(total),
        "mensual": {label: format_currency(value) for label, value in month_totals.items()},
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
    monotributistas = []
    count_sube = 0
    count_baja = 0
    for item in monotributistas_raw:
        _, total_actual = calcular_totales(item, anchor_actual)
        corresponde = categoria_por_total(total_actual, topes_actual) or item.categoria_actual
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
                "categoria_corresponde": (
                    corresponde.nombre if corresponde else (item.categoria_actual.nombre if item.categoria_actual else "-")
                ),
                "estado_categoria": estado,
            }
        )

    facturas = [
        {
            "id": item.id,
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

    fecha_corte_label = anchor_actual.strftime("%d/%m/%y")
    mono_form = session.pop("mono_form", None)
    open_modal = session.pop("open_modal", None)
    factura_import_logs = []
    if active_tab == "facturas":
        factura_import_logs = (
            FacturaImport.query.order_by(FacturaImport.created_at.desc())
            .all()
        )

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
        detalle=detalle,
        active_tab=active_tab,
        anchor_value=anchor_value,
        anchor_cutoff_label=anchor_cutoff_label,
        count_sube=count_sube,
        count_baja=count_baja,
        fecha_corte_label=fecha_corte_label,
        mono_form=mono_form,
        open_modal=open_modal,
        factura_import_logs=factura_import_logs,
    )


@main_bp.post("/monotributistas/create")
@login_required
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


@main_bp.route("/monotributistas/<int:monotributista_id>/edit", methods=["GET", "POST"])
@login_required
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
def delete_monotributista(monotributista_id):
    monotributista = Monotributista.query.get_or_404(monotributista_id)
    db.session.delete(monotributista)
    db.session.commit()
    flash("Monotributista eliminado.", "success")
    return redirect(url_for("main.dashboard", tab="monotributistas"))


@main_bp.post("/facturas/create")
@login_required
def create_factura():
    pdf_files = [item for item in request.files.getlist("pdf_file") if item and item.filename]
    has_pdf = len(pdf_files) > 0

    if has_pdf:
        monotributista_id = request.form.get("monotributista_id")
        monotributista_id = int(monotributista_id) if monotributista_id else None

        invalid = [item.filename for item in pdf_files if not item.filename.lower().endswith(".pdf")]
        if invalid:
            db.session.add(
                FacturaImport(
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
                )
                enqueued += 1
                factura_import.result_message = "En cola para procesamiento."
            except Exception as exc:
                factura_import.status = "failed"
                factura_import.error = f"No se pudo encolar: {exc}"
                factura_import.result_message = factura_import.error
                factura_import.processed_at = datetime.now(timezone.utc)
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

    if tipo_comp.upper().startswith("NC") and importe_total > 0:
        importe_total = -importe_total

    if (punto_venta and not punto_venta.isdigit()) or not nro_comp.isdigit():
        db.session.add(
            FacturaImport(
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

    numero_comp = f"{punto_venta.zfill(4)}-{nro_comp.zfill(8)}"
    existing = Factura.query.filter_by(
        monotributista_id=int(monotributista_id),
        tipo_comp=tipo_comp,
        numero_comp=numero_comp,
    ).first()
    if existing:
        db.session.add(
            FacturaImport(
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
            monotributista_id=int(monotributista_id),
            status="done",
            pdf_path="",
            source="manual",
            factura_id=factura.id,
            result_message=f"Factura creada: {numero_comp}",
            processed_at=datetime.now(timezone.utc),
        )
    )
    db.session.commit()
    session["open_modal"] = "factura-imports"
    flash("Factura creada.", "success")
    return redirect(url_for("main.dashboard", tab="facturas"))


@main_bp.route("/facturas/<int:factura_id>/edit", methods=["GET", "POST"])
@login_required
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
            if tipo_comp.upper().startswith("NC") and importe_total > 0:
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
            numero_comp = f"{punto_venta.zfill(4)}-{nro_comp.zfill(8)}"
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
        .limit(200)
        .all()
    )
    payload = []
    for item in logs:
        payload.append(
            {
                "created_at": item.created_at.isoformat() if item.created_at else "",
                "monotributista": item.monotributista.razon_social if item.monotributista else "-",
                "source": item.source or "-",
                "status": item.status or "-",
                "result": item.result_message or item.error or "-",
            }
        )
    return jsonify(payload)


@main_bp.post("/vigencias/create")
@login_required
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
def delete_vigencia(vigencia_id):
    vigencia = Vigencia.query.get_or_404(vigencia_id)
    db.session.delete(vigencia)
    db.session.commit()
    flash("Vigencia eliminada.", "success")
    return redirect(url_for("main.dashboard", tab="configuracion"))
