import os

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError


def main() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL must be set.")
        return 1

    engine = create_engine(db_url)

    try:
        with engine.begin() as conn:
            dialect = engine.dialect.name

            if dialect == "sqlite":
                columns = conn.execute(text("PRAGMA table_info(factura_import)")).fetchall()
                has_batch_id = any(col[1] == "batch_id" for col in columns)
            else:
                has_batch_id = (
                    conn.execute(
                        text(
                            """
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_name = 'factura_import'
                              AND column_name = 'batch_id'
                            LIMIT 1
                            """
                        )
                    ).scalar()
                    is not None
                )

            if not has_batch_id:
                conn.execute(
                    text("ALTER TABLE factura_import ADD COLUMN batch_id VARCHAR(32)")
                )
                print("OK: columna factura_import.batch_id creada.")
            else:
                print("OK: columna factura_import.batch_id ya existe.")

            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_factura_import_batch_id "
                    "ON factura_import (batch_id)"
                )
            )
            print("OK: indice ix_factura_import_batch_id verificado.")

        return 0
    except SQLAlchemyError as exc:
        print(f"ERROR: no se pudo migrar factura_import.batch_id: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
