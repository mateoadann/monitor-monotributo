from datetime import date
from decimal import Decimal

from sqlalchemy.orm import sessionmaker

from website.models import Categoria, Factura, Monotributista, db


def _crear_categoria():
    categoria = Categoria(nombre="A", orden=1, tope_facturacion=Decimal("0.00"))
    db.session.add(categoria)
    db.session.commit()
    return categoria


def _crear_monotributista(categoria):
    mono = Monotributista(
        razon_social="RPA Test",
        cuit="20111111119",
        clave_fiscal="clave",
        categoria_actual_id=categoria.id,
        categoria_corresponde_id=categoria.id,
    )
    db.session.add(mono)
    db.session.commit()
    return mono


def _precheck_existe(monotributista_id, tipo_comp, numero_comp):
    """Replica del pre-check que hace el RPA antes de descargar un PDF."""
    return (
        Factura.query.filter_by(
            monotributista_id=monotributista_id,
            tipo_comp=tipo_comp,
            numero_comp=numero_comp,
        ).first()
    )


def test_precheck_ve_facturas_recien_insertadas_tras_expire_all(app):
    """
    Regresion del incidente del 07/04: el RPA no veia facturas insertadas
    concurrentemente por el worker de PDFs, causando descargas duplicadas.

    El fix en playwright/descargar-pdf.py agrega db.session.expire_all() antes
    del pre-check contra Factura.query.filter_by(...) dentro del loop de
    descarga, forzando a SQLAlchemy a releer la fila desde la DB en vez de
    servir desde el identity map / snapshot de la transaccion actual.
    """
    with app.app_context():
        categoria = _crear_categoria()
        mono = _crear_monotributista(categoria)
        monotributista_id = mono.id

        # Primer pre-check: todavia no hay facturas. Este query "calienta" la
        # sesion actual con el snapshot transaccional vigente.
        exists_before = _precheck_existe(
            monotributista_id=monotributista_id,
            tipo_comp="C",
            numero_comp="00001-00000001",
        )
        assert exists_before is None

        # Simular que OTRO proceso (el worker de PDFs) inserta una factura y
        # commitea. Usamos una sesion paralela sobre el mismo engine para que
        # la escritura sea externa a la sesion actual de SQLAlchemy.
        engine = db.engine
        OtherSession = sessionmaker(bind=engine)
        other = OtherSession()
        try:
            otra = Factura(
                monotributista_id=monotributista_id,
                fecha=date(2025, 1, 10),
                tipo_comp="C",
                numero_comp="00001-00000001",
                importe_total=Decimal("100.00"),
                fecha_desde=date(2025, 1, 10),
                fecha_hasta=date(2025, 1, 10),
            )
            other.add(otra)
            other.commit()
        finally:
            other.close()

        # Fix aplicado: expire_all fuerza el reload desde DB. Con esto el
        # pre-check debe ver la factura recien insertada por el otro proceso.
        db.session.expire_all()

        exists_after = _precheck_existe(
            monotributista_id=monotributista_id,
            tipo_comp="C",
            numero_comp="00001-00000001",
        )
        assert exists_after is not None
        assert exists_after.numero_comp == "00001-00000001"
        assert exists_after.tipo_comp == "C"
        assert exists_after.monotributista_id == monotributista_id
