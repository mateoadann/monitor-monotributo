import argparse
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from website import create_app
from website.models import Categoria, CategoriaTope, Vigencia, db


CATEGORY_CODES = [chr(code) for code in range(ord("A"), ord("K") + 1)]
ZERO = Decimal("0.00")

VIGENCIAS_PAYLOAD = [
    {
        "fecha_desde": "2025-08-01",
        "fecha_hasta": "2026-01-31",
        "topes": {
            "A": "8992597.87",
            "B": "13175201.52",
            "C": "18473166.15",
            "D": "22934610.05",
            "E": "26977793.60",
            "F": "33809379.57",
            "G": "40431835.35",
            "H": "61344853.64",
            "I": "68664410.05",
            "J": "78632948.76",
            "K": "94805682.90",
        },
    },
    {
        "fecha_desde": "2026-02-01",
        "fecha_hasta": "2026-07-31",
        "topes": {
            "A": "10277988.13",
            "B": "15058447.71",
            "C": "21113696.52",
            "D": "26212853.42",
            "E": "30833964.37",
            "F": "38642048.36",
            "G": "46211109.37",
            "H": "70113407.33",
            "I": "78479211.62",
            "J": "89872640.30",
            "K": "108357084.05",
        },
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap idempotente de vigencias y topes para produccion"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Aplica cambios en DB. Si no se envia, corre en dry-run.",
    )
    return parser.parse_args()


def parse_iso_date(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def find_vigencia(fecha_desde, fecha_hasta):
    query = Vigencia.query.filter(Vigencia.fecha_desde == fecha_desde)
    if fecha_hasta is None:
        query = query.filter(Vigencia.fecha_hasta.is_(None))
    else:
        query = query.filter(Vigencia.fecha_hasta == fecha_hasta)
    return query.first()


def ensure_payload_valid() -> None:
    expected = set(CATEGORY_CODES)
    for idx, item in enumerate(VIGENCIAS_PAYLOAD, start=1):
        topes = item.get("topes") or {}
        got = set(topes.keys())
        if got != expected:
            faltantes = ", ".join(sorted(expected - got)) or "-"
            extra = ", ".join(sorted(got - expected)) or "-"
            raise ValueError(
                "Payload invalido en vigencia "
                f"{idx}: faltantes=[{faltantes}] extra=[{extra}]"
            )


def bootstrap(apply_changes: bool) -> int:
    mode = "APPLY" if apply_changes else "DRY-RUN"
    stats = {
        "categorias_creadas": 0,
        "categorias_actualizadas": 0,
        "vigencias_creadas": 0,
        "vigencias_existentes": 0,
        "topes_creados": 0,
        "topes_actualizados": 0,
        "topes_sin_cambios": 0,
    }

    app = create_app(init_db=False)
    with app.app_context():
        ensure_payload_valid()

        categorias_by_code = {cat.nombre: cat for cat in Categoria.query.all()}
        for orden, code in enumerate(CATEGORY_CODES, start=1):
            categoria = categorias_by_code.get(code)
            if not categoria:
                categoria = Categoria(nombre=code, orden=orden, tope_facturacion=ZERO)
                db.session.add(categoria)
                categorias_by_code[code] = categoria
                stats["categorias_creadas"] += 1
            else:
                changed = False
                if categoria.orden != orden:
                    categoria.orden = orden
                    changed = True
                if categoria.tope_facturacion != ZERO:
                    categoria.tope_facturacion = ZERO
                    changed = True
                if changed:
                    stats["categorias_actualizadas"] += 1

        db.session.flush()

        for payload in VIGENCIAS_PAYLOAD:
            fecha_desde = parse_iso_date(payload["fecha_desde"])
            fecha_hasta_raw = payload.get("fecha_hasta")
            fecha_hasta = parse_iso_date(fecha_hasta_raw) if fecha_hasta_raw else None
            vigencia = find_vigencia(fecha_desde, fecha_hasta)

            if not vigencia:
                vigencia = Vigencia(fecha_desde=fecha_desde, fecha_hasta=fecha_hasta)
                db.session.add(vigencia)
                db.session.flush()
                stats["vigencias_creadas"] += 1
            else:
                stats["vigencias_existentes"] += 1

            for code in CATEGORY_CODES:
                categoria = categorias_by_code[code]
                target_tope = Decimal(payload["topes"][code]).quantize(Decimal("0.01"))
                categoria_tope = CategoriaTope.query.filter_by(
                    vigencia_id=vigencia.id,
                    categoria_id=categoria.id,
                ).first()

                if not categoria_tope:
                    categoria_tope = CategoriaTope(
                        vigencia_id=vigencia.id,
                        categoria_id=categoria.id,
                        tope_facturacion=target_tope,
                    )
                    db.session.add(categoria_tope)
                    stats["topes_creados"] += 1
                    continue

                current_tope = categoria_tope.tope_facturacion.quantize(Decimal("0.01"))
                if current_tope != target_tope:
                    categoria_tope.tope_facturacion = target_tope
                    stats["topes_actualizados"] += 1
                else:
                    stats["topes_sin_cambios"] += 1

        if apply_changes:
            db.session.commit()
        else:
            db.session.rollback()

    print(f"Modo: {mode}")
    print(f"Categorias creadas: {stats['categorias_creadas']}")
    print(f"Categorias actualizadas: {stats['categorias_actualizadas']}")
    print(f"Vigencias creadas: {stats['vigencias_creadas']}")
    print(f"Vigencias existentes: {stats['vigencias_existentes']}")
    print(f"Topes creados: {stats['topes_creados']}")
    print(f"Topes actualizados: {stats['topes_actualizados']}")
    print(f"Topes sin cambios: {stats['topes_sin_cambios']}")
    return 0


def main() -> int:
    args = parse_args()
    try:
        return bootstrap(apply_changes=args.apply)
    except Exception as exc:
        db.session.rollback()
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
