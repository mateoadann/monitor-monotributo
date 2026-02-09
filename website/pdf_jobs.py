from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal

from website import create_app
from website.models import Factura, FacturaImport, Monotributista, db
from website.pdf_extractor import extract_factura_data

CREDIT_NOTE_TYPES = {"NC", "NCC", "NCE"}


def build_numero_comp(punto_venta: str | None, numero: str | None) -> str | None:
    if not punto_venta or not numero:
        return None
    punto_venta = punto_venta.strip()
    numero = numero.strip()
    if not punto_venta.isdigit() or not numero.isdigit():
        return None
    punto_venta = punto_venta.zfill(5)
    numero = numero.zfill(8)
    return f"{punto_venta}-{numero}"


def match_monotributista(cuit: str | None, razon: str | None) -> int | None:
    if cuit:
        item = Monotributista.query.filter_by(cuit=cuit).first()
        if item:
            return item.id
    if razon:
        item = Monotributista.query.filter(
            Monotributista.razon_social.ilike(razon)
        ).first()
        if item:
            return item.id
    return None


def cleanup_pdf_path(pdf_path: str | None, upload_root: str | None) -> None:
    if not pdf_path:
        return
    abs_path = os.path.abspath(pdf_path)
    if not os.path.exists(abs_path):
        return
    try:
        os.remove(abs_path)
    except OSError:
        return
    if not upload_root:
        return
    upload_root = os.path.abspath(upload_root)
    if os.path.commonpath([abs_path, upload_root]) != upload_root:
        return
    current = os.path.dirname(abs_path)
    while os.path.commonpath([current, upload_root]) == upload_root:
        if current == upload_root:
            break
        try:
            os.rmdir(current)
        except OSError:
            break
        current = os.path.dirname(current)


def handle_import_failure(job, exc_type, exc_value, tb) -> None:
    import_id = job.args[0] if job and job.args else None
    if not import_id:
        return
    app = create_app()
    with app.app_context():
        factura_import = db.session.get(FacturaImport, import_id)
        if not factura_import:
            return
        if factura_import.status != "done":
            error_message = str(exc_value) if exc_value else "Fallo en procesamiento"
            factura_import.status = "failed"
            factura_import.error = error_message
            factura_import.result_message = error_message
            factura_import.processed_at = datetime.now(timezone.utc)
            db.session.commit()
        cleanup_pdf_path(factura_import.pdf_path, app.config.get("UPLOAD_FOLDER"))


def process_factura_import(import_id: int) -> None:
    app = create_app()
    with app.app_context():
        factura_import = db.session.get(FacturaImport, import_id)
        if not factura_import:
            return
        pdf_path = factura_import.pdf_path
        factura_import.status = "processing"
        factura_import.error = None
        factura_import.result_message = None
        db.session.commit()

        try:
            data = extract_factura_data(factura_import.pdf_path)
            factura_import.extracted_data = json.dumps(
                data, ensure_ascii=True, default=str
            )

            tipo_comp = data.get("tipo_comp")
            punto_venta = data.get("punto_venta")
            numero_raw = data.get("numero_comp")
            if tipo_comp == "E" and not punto_venta and numero_raw:
                punto_venta = "0"
            numero_comp = build_numero_comp(punto_venta, numero_raw)
            importe_total = data.get("importe_total")
            cuit_receptor = data.get("cuit_receptor")
            fecha_emision = data.get("fecha_emision")
            fecha_desde = data.get("fecha_desde")
            fecha_hasta = data.get("fecha_hasta")

            if tipo_comp == "E":
                if not cuit_receptor:
                    cuit_receptor = "-"
                if not fecha_desde:
                    fecha_desde = fecha_emision
                if not fecha_hasta:
                    fecha_hasta = fecha_emision

                usd_total = data.get("importe_total_usd") or data.get("importe_total")
                exchange_rate = data.get("exchange_rate")
                if usd_total is None or exchange_rate is None:
                    raise ValueError(
                        "Faltan importe_total_usd o exchange_rate para factura E"
                    )
                importe_total = (usd_total * exchange_rate).quantize(Decimal("0.01"))

            if tipo_comp in CREDIT_NOTE_TYPES and isinstance(importe_total, Decimal):
                if importe_total > 0:
                    importe_total = -importe_total

            missing = []
            if not data.get("fecha_emision"):
                missing.append("fecha_emision")
            if not tipo_comp:
                missing.append("tipo_comp")
            if not numero_comp:
                missing.append("numero_comp")
            if importe_total is None:
                missing.append("importe_total")
            if missing:
                raise ValueError(
                    "Faltan campos obligatorios: " + ", ".join(missing)
                )

            cuit_emisor = data.get("cuit_emisor")
            if not cuit_emisor:
                raise ValueError("Falta cuit_emisor del facturador")

            monotributista = Monotributista.query.filter_by(cuit=cuit_emisor).first()
            if not monotributista:
                raise ValueError(
                    "El CUIT del facturador no existe en la base. Cargue el monotributista antes de importar."
                )
            if (
                factura_import.monotributista_id
                and factura_import.monotributista_id != monotributista.id
            ):
                raise ValueError(
                    "El monotributista seleccionado no coincide con el CUIT del facturador."
                )

            monotributista_id = monotributista.id
            factura_import.monotributista_id = monotributista_id

            existing = Factura.query.filter_by(
                monotributista_id=monotributista_id,
                tipo_comp=tipo_comp,
                numero_comp=numero_comp,
            ).first()
            if existing:
                raise ValueError(
                    "Ya existe una factura con ese numero y punto de venta para este monotributista."
                )

            factura = Factura()
            factura.monotributista_id = monotributista_id
            factura.fecha = fecha_emision
            factura.tipo_comp = tipo_comp
            factura.numero_comp = numero_comp
            factura.cuit_receptor = cuit_receptor
            factura.razon_social_receptor = data.get("razon_receptor")
            factura.importe_total = importe_total
            factura.fecha_desde = fecha_desde
            factura.fecha_hasta = fecha_hasta
            factura.concepto = data.get("concepto")
            db.session.add(factura)
            db.session.flush()

            factura_import.factura_id = factura.id
            factura_import.status = "done"
            factura_import.processed_at = datetime.now(timezone.utc)
            factura_import.result_message = f"Factura creada: {numero_comp}"
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            factura_import = db.session.get(FacturaImport, import_id)
            if factura_import:
                factura_import.status = "failed"
                factura_import.error = str(exc)
                factura_import.result_message = str(exc)
                factura_import.processed_at = datetime.now(timezone.utc)
                db.session.commit()
            raise
        finally:
            cleanup_pdf_path(pdf_path, app.config.get("UPLOAD_FOLDER"))
