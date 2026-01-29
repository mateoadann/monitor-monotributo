import os

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError


def main() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL must be set.")
        return 1
    engine = create_engine(db_url)

    if engine.dialect.name == "sqlite":
        print("SQLite: no migration required (concepto length not enforced).")
        return 0

    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE factura ALTER COLUMN concepto TYPE TEXT"))
        print("OK: columna factura.concepto actualizada a TEXT.")
        return 0
    except SQLAlchemyError as exc:
        print(f"ERROR: no se pudo actualizar columna concepto: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
