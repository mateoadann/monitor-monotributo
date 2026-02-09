from decimal import Decimal
from datetime import date
from decimal import Decimal

from website.models import Categoria, CategoriaTope, Factura, Monotributista, Vigencia, db
from website.views import build_calculo, calcular_totales, categoria_por_total, estado_categoria


def test_categoria_por_total_selecciona_por_tope():
    categoria_a = Categoria(nombre="A", orden=1, tope_facturacion=Decimal("0.00"))
    categoria_b = Categoria(nombre="B", orden=2, tope_facturacion=Decimal("0.00"))

    tope_a = CategoriaTope(tope_facturacion=Decimal("100"), categoria=categoria_a)
    tope_b = CategoriaTope(tope_facturacion=Decimal("200"), categoria=categoria_b)

    assert categoria_por_total(Decimal("150"), [tope_b, tope_a]).nombre == "B"
    assert categoria_por_total(Decimal("250"), [tope_a, tope_b]).nombre == "B"
    assert categoria_por_total(Decimal("50"), [tope_b, tope_a]).nombre == "A"


def test_categoria_por_total_devuelve_none_sin_topes():
    assert categoria_por_total(Decimal("10"), []) is None
    tope_zero = CategoriaTope(
        tope_facturacion=Decimal("0.00"),
        categoria=Categoria(nombre="A", orden=1, tope_facturacion=Decimal("0.00")),
    )
    assert categoria_por_total(Decimal("10"), [tope_zero]) is None


def test_estado_categoria_compara_orden():
    categoria_a = Categoria(nombre="A", orden=1, tope_facturacion=Decimal("0.00"))
    categoria_c = Categoria(nombre="C", orden=3, tope_facturacion=Decimal("0.00"))
    assert estado_categoria(categoria_a, categoria_c) == "sube"
    assert estado_categoria(categoria_c, categoria_a) == "baja"
    assert estado_categoria(categoria_a, categoria_a) == "mantiene"


def test_calcular_totales_prorratea_y_descuenta_nc(app):
    with app.app_context():
        monotributista = Monotributista(
            razon_social="Demo",
            cuit="20304050607",
            clave_fiscal="clave",
        )
        db.session.add(monotributista)
        db.session.commit()

        factura_prorrateo = Factura(
            monotributista_id=monotributista.id,
            fecha=date(2025, 3, 20),
            tipo_comp="C",
            numero_comp="0001-00000001",
            importe_total=Decimal("220000.00"),
            fecha_desde=date(2025, 3, 20),
            fecha_hasta=date(2025, 4, 10),
        )
        nota_credito = Factura(
            monotributista_id=monotributista.id,
            fecha=date(2025, 4, 5),
            tipo_comp="NCC",
            numero_comp="0001-00000002",
            importe_total=Decimal("1000.00"),
            fecha_desde=date(2025, 4, 5),
            fecha_hasta=date(2025, 4, 5),
        )
        db.session.add_all([factura_prorrateo, nota_credito])
        db.session.commit()

        month_totals, total = calcular_totales(monotributista, date(2025, 4, 1))

        assert month_totals["Mar 2025"] == Decimal("120000.00")
        assert month_totals["Abr 2025"] == Decimal("99000.00")
        assert total == Decimal("219000.00")


def test_build_calculo_marca_exclusion(app):
    with app.app_context():
        categoria_a = Categoria(nombre="A", orden=1, tope_facturacion=Decimal("0.00"))
        categoria_b = Categoria(nombre="B", orden=2, tope_facturacion=Decimal("0.00"))
        db.session.add_all([categoria_a, categoria_b])
        db.session.commit()

        vigencia = Vigencia(fecha_desde=date(2025, 1, 1))
        db.session.add(vigencia)
        db.session.commit()

        monotributista = Monotributista(
            razon_social="Demo",
            cuit="20304050607",
            clave_fiscal="clave",
            categoria_actual_id=categoria_a.id,
        )
        db.session.add(monotributista)
        db.session.commit()

        tope_a = CategoriaTope(
            tope_facturacion=Decimal("100.00"),
            categoria_id=categoria_a.id,
            vigencia_id=vigencia.id,
        )
        tope_b = CategoriaTope(
            tope_facturacion=Decimal("200.00"),
            categoria_id=categoria_b.id,
            vigencia_id=vigencia.id,
        )
        db.session.add_all([tope_a, tope_b])
        db.session.commit()

        factura = Factura(
            monotributista_id=monotributista.id,
            fecha=date(2025, 4, 1),
            tipo_comp="C",
            numero_comp="00001-00000001",
            importe_total=Decimal("250.00"),
            fecha_desde=date(2025, 4, 1),
            fecha_hasta=date(2025, 4, 1),
        )
        db.session.add(factura)
        db.session.commit()

        detalle = build_calculo(monotributista, date(2025, 4, 1), [tope_a, tope_b])

        assert detalle["estado_categoria"] == "exclusion"
        assert detalle["categoria_corresponde"] == "Exclusión"
