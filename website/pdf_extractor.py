from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation

import pdfplumber


def extract_text_from_pdf(pdf_path: str) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        if not pdf.pages:
            return ""
        first_page_text = pdf.pages[0].extract_text() or ""
        if first_page_text.strip():
            return first_page_text
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def strip_accents(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", value)
        if unicodedata.category(char) != "Mn"
    )


def normalize_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def parse_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError:
        return None


def parse_decimal(value: str | None):
    if not value:
        return None
    cleaned = re.sub(r"[^0-9,\\.]", "", value)
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def extract_facturador(lines: list[str]) -> str | None:
    for line in lines:
        if line.lower().startswith("razon social:"):
            return line.split(":", 1)[1].strip()
    for line in lines:
        if line.lower().startswith("company name:"):
            return line.split(":", 1)[1].strip()
    return None


def extract_cuit_emisor(lines: list[str]) -> str | None:
    for line in lines:
        if "apellido y nombre / razon social:" in line.lower():
            break
        match = re.search(r"CUIT:\s*([0-9]{11})", line)
        if match:
            return match.group(1)
    return None


def extract_receptor(lines: list[str]) -> tuple[str | None, str | None]:
    for idx, line in enumerate(lines):
        if "apellido y nombre / razon social:" not in line.lower():
            continue
        match = re.search(
            r"CUIT:\s*([0-9]{11}).*Apellido y Nombre / Razon Social:\s*(.+)$",
            line,
            re.I,
        )
        if match:
            return match.group(1), match.group(2).strip()
        razon_match = re.search(
            r"Apellido y Nombre / Razon Social:\s*(.+)$", line, re.I
        )
        razon = razon_match.group(1).strip() if razon_match else None
        cuit = None
        for back in range(1, 3):
            if idx - back < 0:
                break
            prev = lines[idx - back]
            match = re.search(r"CUIT:\s*([0-9]{11})", prev)
            if match:
                cuit = match.group(1)
                break
        return cuit, razon

    for line in lines:
        if line.lower().startswith("customer:"):
            customer = line.split(":", 1)[1].strip()
            if "Address:" in customer:
                customer = customer.split("Address:", 1)[0].strip()
            return None, customer
    return None, None


def extract_tipo_comp(text: str) -> str | None:
    match = re.search(
        r"\b([A-Z])\s+(FACTURA|NOTA DE CREDITO|EXPORT INVOICE)\b",
        text,
        re.I,
    )
    if not match:
        match = re.search(
            r"\b(FACTURA|NOTA DE CREDITO|EXPORT INVOICE)\s+([A-Z])\b",
            text,
            re.I,
        )
    if not match:
        return None
    if match.lastindex == 2:
        if match.group(1).isalpha() and len(match.group(1)) == 1:
            letter = match.group(1)
            label = match.group(2)
        else:
            label = match.group(1)
            letter = match.group(2)
    else:
        return None
    if "NOTA DE CREDITO" in label.upper():
        return f"NC{letter}"
    return letter


def extract_punto_y_numero(text: str) -> tuple[str | None, str | None]:
    punto_venta = None
    numero_comp = None
    match = re.search(r"Punto de Venta:\s*([0-9]+)", text, re.I)
    if match:
        punto_venta = match.group(1)
    match = re.search(r"Comp\. Nro:\s*([0-9]+)", text, re.I)
    if match:
        numero_comp = match.group(1)
    if not punto_venta or not numero_comp:
        match = re.search(r"Invoice No\.?:\s*([0-9]{5})-([0-9]{8})", text, re.I)
        if match:
            punto_venta = punto_venta or match.group(1)
            numero_comp = numero_comp or match.group(2)
    return punto_venta, numero_comp


def extract_fecha_emision(lines: list[str]) -> str | None:
    for line in lines:
        match = re.search(
            r"Fecha de Emision:\s*(\d{2}/\d{2}/\d{4})", line, re.I
        )
        if match:
            return match.group(1)
    for line in lines:
        match = re.search(r"Invoice Date:\s*(\d{2}/\d{2}/\d{4})", line, re.I)
        if match:
            return match.group(1)
    return None


def extract_periodo(text: str) -> tuple[str | None, str | None]:
    match = re.search(
        r"Periodo Facturado Desde:\s*(\d{2}/\d{2}/\d{4}).*Hasta:\s*(\d{2}/\d{2}/\d{4})",
        text,
        re.S | re.I,
    )
    if match:
        return match.group(1), match.group(2)
    return None, None


def extract_importe_total(lines: list[str]) -> Decimal | None:
    for line in lines:
        match = re.search(r"asciende a:\s*\$?\s*([0-9.,]+)", line, re.I)
        if match:
            value = parse_decimal(match.group(1))
            if value is not None:
                return value
    for line in lines:
        match = re.search(r"importe total:\s*([A-Z$]*\s*)?([0-9.,]+)", line, re.I)
        if match:
            value = parse_decimal(match.group(2))
            if value is not None:
                return value
    for line in lines:
        match = re.search(r"total amount:\s*([A-Z$]*\s*)?([0-9.,]+)", line, re.I)
        if match:
            value = parse_decimal(match.group(2))
            if value is not None:
                return value
    return None


def extract_exchange_rate(lines: list[str]) -> Decimal | None:
    for line in lines:
        match = re.search(r"exchange rate:\s*([0-9.,]+)", line, re.I)
        if match:
            value = parse_decimal(match.group(1))
            if value is not None:
                return value
    return None


def extract_concepto(lines: list[str]) -> str | None:
    start_idx = None
    for idx, line in enumerate(lines):
        line_lower = line.lower()
        if "producto / servicio" in line_lower or line_lower.startswith("item description"):
            start_idx = idx + 1
            break
    if start_idx is None:
        return None
    items = []
    current = None
    stop_markers = (
        "Subtotal:",
        "Importe Total:",
        "Total Amount:",
        "Moneda:",
        "Currency:",
        "Exchange Rate:",
    )
    for line in lines[start_idx:]:
        if not line:
            continue
        line_lower = line.lower()
        if any(marker.lower() in line_lower for marker in stop_markers):
            break
        if line.lower().startswith("codigo"):
            continue
        if line.lower().startswith("unit:"):
            continue
        line_clean = normalize_line(line)
        if not line_clean:
            continue
        if re.search(r"\b\d+[.,]\d+\b", line_clean):
            match = re.match(r"^(?:\d+\s+)?(.+?)\s+\d+[.,]\d+\b", line_clean)
            if match:
                if current:
                    items.append(current.strip())
                current = match.group(1).strip()
                continue
        if current:
            current = f"{current} {line_clean}".strip()
    if current:
        items.append(current.strip())
    if not items:
        return None
    return " - ".join(items)


def extract_factura_data(pdf_path: str) -> dict:
    raw_text = extract_text_from_pdf(pdf_path)
    if not raw_text.strip():
        raise ValueError("No se pudo extraer texto del PDF")

    normalized_text = strip_accents(raw_text)
    lines = [normalize_line(line) for line in normalized_text.splitlines()]
    lines = [line for line in lines if line]

    facturador = extract_facturador(lines)
    cuit_emisor = extract_cuit_emisor(lines)
    fecha_emision_raw = extract_fecha_emision(lines)
    punto_venta, numero_comp = extract_punto_y_numero(normalized_text)
    cuit_receptor, razon_receptor = extract_receptor(lines)
    fecha_desde_raw, fecha_hasta_raw = extract_periodo(normalized_text)
    importe_total = extract_importe_total(lines)
    exchange_rate = extract_exchange_rate(lines)
    concepto = extract_concepto(lines)
    tipo_comp = extract_tipo_comp(normalized_text)
    importe_total_usd = importe_total if tipo_comp == "E" else None

    return {
        "facturador": facturador,
        "cuit_emisor": cuit_emisor,
        "fecha_emision": parse_date(fecha_emision_raw),
        "tipo_comp": tipo_comp,
        "punto_venta": punto_venta,
        "numero_comp": numero_comp,
        "cuit_receptor": cuit_receptor,
        "razon_receptor": razon_receptor,
        "importe_total": importe_total,
        "importe_total_usd": importe_total_usd,
        "exchange_rate": exchange_rate,
        "fecha_desde": parse_date(fecha_desde_raw),
        "fecha_hasta": parse_date(fecha_hasta_raw),
        "concepto": concepto,
    }
