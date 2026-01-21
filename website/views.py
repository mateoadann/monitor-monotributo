from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import os
import uuid

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import login_required
from rq import Retry
from sqlalchemy import or_
from werkzeug.utils import secure_filename

from website.models import Categoria, Factura, FacturaImport, Monotributista, db
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


def categoria_por_total(total: Decimal, categorias: list[Categoria]):
    if not categorias:
        return None
    total = total.quantize(Decimal("0.01"))
    for categoria in categorias:
        if total <= categoria.tope_facturacion:
            return categoria
    return categorias[-1]


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
        start = factura.fecha_desde or factura.fecha_hasta or factura.fecha
        end = factura.fecha_hasta or factura.fecha_desde or factura.fecha

        if not start or not end:
            continue

        if end < start:
            start, end = end, start

        if start.year == end.year and start.month == end.month:
            label = months_lookup.get((start.year, start.month))
            if label:
                month_totals[label] += factura.importe_total
            continue

        total_days = (end - start).days + 1
        if total_days <= 0:
            label = months_lookup.get((start.year, start.month))
            if label:
                month_totals[label] += factura.importe_total
            continue

        daily_amount = factura.importe_total / Decimal(total_days)
        current = start
        while current <= end:
            label = months_lookup.get((current.year, current.month))
            if label:
                month_totals[label] += daily_amount
            current += timedelta(days=1)

    total = sum(month_totals.values(), Decimal("0.00"))
    return month_totals, total


def build_calculo(
    monotributista: Monotributista, anchor: date, categorias: list[Categoria]
):
    month_totals, total = calcular_totales(monotributista, anchor)
    categoria_actual = monotributista.categoria_actual
    categoria_corresponde = categoria_por_total(total, categorias) or categoria_actual
    estado = estado_categoria(categoria_actual, categoria_corresponde)

    return {
        "categoria_actual": categoria_actual.nombre if categoria_actual else "-",
        "categoria_corresponde": categoria_corresponde.nombre if categoria_corresponde else "-",
        "tope_corresponde": (
            format_currency(categoria_corresponde.tope_facturacion)
            if categoria_corresponde
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
    categorias = Categoria.query.order_by(Categoria.tope_facturacion).all()
    for index, categoria in enumerate(categorias, start=1):
        categoria.orden = index
    db.session.commit()


@main_bp.route("/")
@login_required
def dashboard():
    active_tab = request.args.get("tab", "monotributistas")
    anchor_param = request.args.get("anchor")
    anchor_date = parse_anchor(anchor_param) or date.today().replace(day=1)
    anchor_value = f"{anchor_date.year:04d}-{anchor_date.month:02d}"
    monotributistas_raw = Monotributista.query.order_by(Monotributista.razon_social).all()
    categorias_raw = Categoria.query.order_by(Categoria.orden).all()
    categorias_sorted = sorted(categorias_raw, key=lambda item: item.tope_facturacion)
    facturas_raw = Factura.query.order_by(Factura.fecha.desc()).all()

    anchor_actual = date.today().replace(day=1)
    monotributistas = []
    for item in monotributistas_raw:
        _, total_actual = calcular_totales(item, anchor_actual)
        corresponde = categoria_por_total(total_actual, categorias_sorted) or item.categoria_actual
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
                "estado_categoria": estado_categoria(item.categoria_actual, corresponde),
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
        seleccionado = Monotributista.query.get(int(seleccionado_id))
    if not seleccionado and monotributistas_raw:
        seleccionado = monotributistas_raw[0]
    detalle = (
        build_calculo(seleccionado, anchor_date, categorias_sorted)
        if seleccionado
        else None
    )

    return render_template(
        "dashboard.html",
        monotributistas=monotributistas,
        facturas=facturas,
        categorias=categorias_raw,
        categorias_table=[
            {
                "id": categoria.id,
                "nombre": categoria.nombre,
                "orden": categoria.orden,
                "tope": format_currency(categoria.tope_facturacion),
            }
            for categoria in categorias_raw
        ],
        monotributistas_select=monotributistas_raw,
        seleccionado_id=seleccionado.id if seleccionado else None,
        detalle=detalle,
        active_tab=active_tab,
        anchor_value=anchor_value,
    )


@main_bp.post("/monotributistas/create")
@login_required
def create_monotributista():
    razon_social = request.form.get("razon_social", "").strip()
    cuit = request.form.get("cuit", "").strip()
    clave_fiscal = request.form.get("clave_fiscal", "").strip()
    categoria_actual_id = request.form.get("categoria_actual_id")

    if not razon_social or not cuit or not clave_fiscal or not categoria_actual_id:
        flash("Completa los campos requeridos para crear el monotributista.", "error")
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

    if request.method == "POST":
        razon_social = request.form.get("razon_social", "").strip()
        cuit = request.form.get("cuit", "").strip()
        clave_fiscal = request.form.get("clave_fiscal", "").strip()
        categoria_actual_id = request.form.get("categoria_actual_id")

        if not razon_social or not cuit or not clave_fiscal or not categoria_actual_id:
            flash("Completa los campos requeridos para editar.", "error")
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
    pdf_file = request.files.get("pdf_file")
    has_pdf = pdf_file and pdf_file.filename

    if has_pdf:
        if not pdf_file.filename.lower().endswith(".pdf"):
            flash("El archivo debe ser un PDF.", "error")
            return redirect(url_for("main.dashboard", tab="facturas"))

        monotributista_id = request.form.get("monotributista_id")
        monotributista_id = int(monotributista_id) if monotributista_id else None

        upload_root = current_app.config["UPLOAD_FOLDER"]
        upload_dir = os.path.join(upload_root, "facturas", uuid.uuid4().hex)
        os.makedirs(upload_dir, exist_ok=True)
        filename = secure_filename(pdf_file.filename) or "factura.pdf"
        pdf_path = os.path.join(upload_dir, filename)
        pdf_file.save(pdf_path)

        factura_import = FacturaImport(
            monotributista_id=monotributista_id,
            status="pending",
            pdf_path=pdf_path,
        )
        db.session.add(factura_import)
        db.session.commit()

        try:
            queue = get_queue()
            queue.enqueue(
                process_factura_import,
                factura_import.id,
                job_timeout=300,
                retry=Retry(max=3, interval=[10, 30, 60]),
            )
            flash("Factura en cola para procesamiento.", "success")
        except Exception as exc:
            factura_import.status = "failed"
            factura_import.error = f"No se pudo encolar: {exc}"
            db.session.commit()
            flash("No se pudo encolar el PDF.", "error")
        return redirect(url_for("main.dashboard", tab="facturas"))

    monotributista_id = request.form.get("monotributista_id")
    fecha = parse_date(request.form.get("fecha"))
    tipo_comp = request.form.get("tipo_comp", "").strip()
    punto_venta = request.form.get("punto_venta", "").strip()
    nro_comp = request.form.get("nro_comp", "").strip()
    cuit_receptor = request.form.get("cuit_receptor", "").strip()
    razon_social_receptor = request.form.get("razon_social_receptor", "").strip()
    importe_total = parse_decimal(request.form.get("importe_total"))
    fecha_desde = parse_date(request.form.get("fecha_desde"))
    fecha_hasta = parse_date(request.form.get("fecha_hasta"))
    concepto = request.form.get("concepto", "").strip()

    if (
        not monotributista_id
        or not fecha
        or not tipo_comp
        or not punto_venta
        or not nro_comp
        or importe_total is None
    ):
        flash("Completa los campos requeridos para crear la factura.", "error")
        return redirect(url_for("main.dashboard", tab="facturas"))

    if not punto_venta.isdigit() or not nro_comp.isdigit():
        flash("El punto de venta y el numero de comprobante deben ser numericos.", "error")
        return redirect(url_for("main.dashboard", tab="facturas"))

    numero_comp = f"{punto_venta.zfill(4)}-{nro_comp.zfill(8)}"

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
    db.session.commit()
    flash("Factura creada.", "success")
    return redirect(url_for("main.dashboard", tab="facturas"))


@main_bp.route("/facturas/<int:factura_id>/edit", methods=["GET", "POST"])
@login_required
def edit_factura(factura_id):
    factura = Factura.query.get_or_404(factura_id)
    monotributistas = Monotributista.query.order_by(Monotributista.razon_social).all()
    punto_venta_value, nro_comp_value = split_numero_comp(factura.numero_comp)

    if request.method == "POST":
        monotributista_id = request.form.get("monotributista_id")
        fecha = parse_date(request.form.get("fecha"))
        tipo_comp = request.form.get("tipo_comp", "").strip()
        punto_venta = request.form.get("punto_venta", "").strip()
        nro_comp = request.form.get("nro_comp", "").strip()
        cuit_receptor = request.form.get("cuit_receptor", "").strip()
        razon_social_receptor = request.form.get("razon_social_receptor", "").strip()
        importe_total = parse_decimal(request.form.get("importe_total"))
        fecha_desde = parse_date(request.form.get("fecha_desde"))
        fecha_hasta = parse_date(request.form.get("fecha_hasta"))
        concepto = request.form.get("concepto", "").strip()

        if (
            not monotributista_id
            or not fecha
            or not tipo_comp
            or not punto_venta
            or not nro_comp
            or importe_total is None
        ):
            flash("Completa los campos requeridos para editar la factura.", "error")
        else:
            if not punto_venta.isdigit() or not nro_comp.isdigit():
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

            numero_comp = f"{punto_venta.zfill(4)}-{nro_comp.zfill(8)}"
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
    db.session.delete(factura)
    db.session.commit()
    flash("Factura eliminada.", "success")
    return redirect(url_for("main.dashboard", tab="facturas"))


@main_bp.post("/categorias/create")
@login_required
def create_categoria():
    nombre = request.form.get("nombre", "").strip().upper()
    tope_facturacion = parse_decimal(request.form.get("tope_facturacion"))

    if not nombre or tope_facturacion is None:
        flash("Completa los campos requeridos para crear la categoria.", "error")
        return redirect(url_for("main.dashboard", tab="configuracion"))

    if Categoria.query.filter_by(nombre=nombre).first():
        flash("La categoria ya existe.", "error")
        return redirect(url_for("main.dashboard", tab="configuracion"))

    categoria = Categoria(
        nombre=nombre,
        orden=0,
        tope_facturacion=tope_facturacion,
    )
    db.session.add(categoria)
    db.session.commit()
    recalc_categoria_orden()
    flash("Categoria creada.", "success")
    return redirect(url_for("main.dashboard", tab="configuracion"))


@main_bp.route("/categorias/<int:categoria_id>/edit", methods=["GET", "POST"])
@login_required
def edit_categoria(categoria_id):
    categoria = Categoria.query.get_or_404(categoria_id)
    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip().upper()
        tope_facturacion = parse_decimal(request.form.get("tope_facturacion"))

        if not nombre or tope_facturacion is None:
            flash("Completa los campos requeridos para editar.", "error")
        else:
            categoria.nombre = nombre
            categoria.tope_facturacion = tope_facturacion
            db.session.commit()
            recalc_categoria_orden()
            flash("Categoria actualizada.", "success")
            return redirect(url_for("main.dashboard", tab="configuracion"))

    return render_template(
        "edit_categoria.html",
        categoria=categoria,
        tope_value=f"{categoria.tope_facturacion:.2f}",
    )


@main_bp.post("/categorias/<int:categoria_id>/delete")
@login_required
def delete_categoria(categoria_id):
    categoria = Categoria.query.get_or_404(categoria_id)
    usage = Monotributista.query.filter(
        or_(
            Monotributista.categoria_actual_id == categoria.id,
            Monotributista.categoria_corresponde_id == categoria.id,
        )
    ).first()
    if usage:
        flash("No se puede eliminar una categoria en uso.", "error")
        return redirect(url_for("main.dashboard", tab="configuracion"))

    db.session.delete(categoria)
    db.session.commit()
    recalc_categoria_orden()
    flash("Categoria eliminada.", "success")
    return redirect(url_for("main.dashboard", tab="configuracion"))
