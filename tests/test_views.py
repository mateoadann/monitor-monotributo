from decimal import Decimal

from website.models import Categoria, Factura, Monotributista, db


def login(client):
    return client.post(
        "/login",
        data={"username": "admin", "password": "admin"},
        follow_redirects=True,
    )


def create_categoria():
    categoria = Categoria(nombre="A", orden=1, tope_facturacion=Decimal("0.00"))
    db.session.add(categoria)
    db.session.commit()
    return categoria


def test_login_success_renders_dashboard(client):
    response = login(client)
    assert response.status_code == 200
    assert b"Listado de monotributistas" in response.data


def test_create_monotributista_requires_fields(client, app):
    login(client)
    response = client.post(
        "/monotributistas/create",
        data={"razon_social": "", "cuit": "", "clave_fiscal": ""},
        follow_redirects=True,
    )
    assert b"Completa los campos requeridos" in response.data
    with app.app_context():
        assert Monotributista.query.count() == 0


def test_create_monotributista_success(client, app):
    with app.app_context():
        categoria = create_categoria()
        categoria_id = categoria.id

    login(client)
    response = client.post(
        "/monotributistas/create",
        data={
            "razon_social": "Demo SRL",
            "cuit": "20304050607",
            "clave_fiscal": "clave",
            "categoria_actual_id": str(categoria_id),
        },
        follow_redirects=True,
    )

    assert b"Monotributista creado." in response.data
    with app.app_context():
        monotributistas = Monotributista.query.all()
        assert len(monotributistas) == 1
        assert monotributistas[0].razon_social == "Demo SRL"


def test_create_factura_nc_guarda_importe_negativo(client, app):
    with app.app_context():
        categoria = create_categoria()
        monotributista = Monotributista(
            razon_social="Demo SRL",
            cuit="20304050607",
            clave_fiscal="clave",
            categoria_actual_id=categoria.id,
            categoria_corresponde_id=categoria.id,
        )
        db.session.add(monotributista)
        db.session.commit()
        monotributista_id = monotributista.id

    login(client)
    response = client.post(
        "/facturas/create",
        data={
            "monotributista_id": str(monotributista_id),
            "fecha": "2025-03-10",
            "tipo_comp": "NCB",
            "punto_venta": "2",
            "nro_comp": "147",
            "importe_total": "1000",
            "fecha_desde": "2025-03-10",
            "fecha_hasta": "2025-03-10",
        },
        follow_redirects=True,
    )

    assert b"Factura creada." in response.data
    with app.app_context():
        facturas = Factura.query.all()
        assert len(facturas) == 1
        assert str(facturas[0].importe_total) == "-1000.00"
        assert facturas[0].numero_comp == "00002-00000147"
