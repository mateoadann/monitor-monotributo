from __future__ import annotations

import json
from decimal import Decimal

from website import create_app
from website.models import Factura, FacturaImport, Monotributista, db
from website.pdf_extractor import extract_factura_data


def build_numero_comp(punto_venta: str | None, numero: str | None) -> str | None:
    if not punto_venta or not numero:
        return None
    punto_venta = punto_venta.strip()
    numero = numero.strip()
    if not punto_venta.isdigit() or not numero.isdigit():
        return None
    if len(punto_venta) <= 4:
        punto_venta = punto_venta.zfill(4)
    else:
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


def process_factura_import(import_id: int) -> None:
    app = create_app()
    with app.app_context():
        factura_import = FacturaImport.query.get(import_id)
        if not factura_import:
            return
        factura_import.status = "processing"
        factura_import.error = None
        db.session.commit()

        try:
            data = extract_factura_data(factura_import.pdf_path)
            factura_import.extracted_data = json.dumps(
                data, ensure_ascii=True, default=str
            )

            numero_comp = build_numero_comp(
                data.get("punto_venta"), data.get("numero_comp")
            )
            tipo_comp = data.get("tipo_comp")
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

            if tipo_comp and tipo_comp.startswith("NC") and isinstance(
                importe_total, Decimal
            ):
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

            monotributista_id = factura_import.monotributista_id or match_monotributista(
                data.get("cuit_emisor"), data.get("facturador")
            )

            factura = Factura(
                monotributista_id=monotributista_id,
                fecha=fecha_emision,
                tipo_comp=tipo_comp,
                numero_comp=numero_comp,
                cuit_receptor=cuit_receptor,
                razon_social_receptor=data.get("razon_receptor"),
                importe_total=importe_total,
                fecha_desde=fecha_desde,
                fecha_hasta=fecha_hasta,
                concepto=data.get("concepto"),
            )
            db.session.add(factura)
            db.session.flush()

            factura_import.factura_id = factura.id
            factura_import.status = "done"
            db.session.commit()
        except Exception as exc:
            factura_import.status = "failed"
            factura_import.error = str(exc)
            db.session.commit()
            raise
