from datetime import date, datetime, timezone
from decimal import Decimal
import io

import pytest
from website.models import Categoria, CategoriaTope, Factura, FacturaImport, Monotributista, Vigencia, db
from website.pdf_jobs import process_factura_import


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


def test_import_monotributistas_csv_crea_y_omite_existentes(client, app):
    with app.app_context():
        categoria_a = Categoria(nombre="A", orden=1, tope_facturacion=Decimal("0.00"))
        categoria_b = Categoria(nombre="B", orden=2, tope_facturacion=Decimal("0.00"))
        db.session.add_all([categoria_a, categoria_b])
        db.session.flush()
        existente = Monotributista(
            razon_social="Existente SA",
            cuit="20304050607",
            clave_fiscal="clave",
            categoria_actual_id=categoria_a.id,
            categoria_corresponde_id=categoria_a.id,
        )
        db.session.add(existente)
        db.session.commit()

    login(client)
    csv_content = "\n".join(
        [
            "razon_social,cuit,clave_fiscal,categoria_actual",
            "Existente SA,20304050607,clave,A",
            "Nuevo SRL,20304050608,clave2,B",
            "Sin Categoria,20304050609,clave3,Z",
        ]
    )
    response = client.post(
        "/monotributistas/import-csv",
        data={
            "monotributistas_csv": (
                io.BytesIO(csv_content.encode("utf-8")),
                "monotributistas.csv",
            ),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert b"Importacion finalizada" in response.data
    assert b"omitidos_existentes=1" in response.data
    assert b"errores=1" in response.data
    with app.app_context():
        monotributistas = Monotributista.query.order_by(Monotributista.cuit).all()
        assert len(monotributistas) == 2
        nuevo = Monotributista.query.filter_by(cuit="20304050608").first()
        assert nuevo is not None
        assert nuevo.razon_social == "Nuevo SRL"
        assert nuevo.categoria_actual.nombre == "B"


def test_import_monotributistas_csv_dry_run_no_persiste(client, app):
    with app.app_context():
        categoria = Categoria(nombre="A", orden=1, tope_facturacion=Decimal("0.00"))
        db.session.add(categoria)
        db.session.commit()

    login(client)
    csv_content = "\n".join(
        [
            "razon_social,cuit,clave_fiscal,categoria_actual",
            "Demo Dry,20304050610,clave,A",
        ]
    )
    response = client.post(
        "/monotributistas/import-csv",
        data={
            "dry_run": "1",
            "monotributistas_csv": (
                io.BytesIO(csv_content.encode("utf-8")),
                "dry.csv",
            ),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert b"Simulacion finalizada" in response.data
    assert b"creados=1" in response.data
    with app.app_context():
        assert Monotributista.query.filter_by(cuit="20304050610").first() is None


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
            "tipo_comp": "NCC",
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


def test_create_factura_manual_formatea_numero_comp(client, app):
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
            "tipo_comp": "C",
            "punto_venta": "3",
            "nro_comp": "3",
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
        assert facturas[0].numero_comp == "00003-00000003"


def test_pdf_import_formatea_numero_comp(monkeypatch, app):
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

        factura_import = FacturaImport(
            monotributista_id=monotributista.id,
            status="pending",
            pdf_path="/tmp/demo.pdf",
            filename="demo.pdf",
            source="upload",
        )
        db.session.add(factura_import)
        db.session.commit()
        import_id = factura_import.id
        cuit_emisor = monotributista.cuit

    def fake_extract(_path):
        return {
            "fecha_emision": date(2025, 3, 10),
            "tipo_comp": "C",
            "punto_venta": "3",
            "numero_comp": "3",
            "importe_total": Decimal("1000.00"),
            "cuit_emisor": cuit_emisor,
            "fecha_desde": date(2025, 3, 10),
            "fecha_hasta": date(2025, 3, 10),
        }

    monkeypatch.setattr("website.pdf_jobs.extract_factura_data", fake_extract)
    monkeypatch.setattr("website.pdf_jobs.cleanup_pdf_path", lambda *_: None)

    process_factura_import(import_id)

    with app.app_context():
        facturas = Factura.query.all()
        assert len(facturas) == 1
        assert facturas[0].numero_comp == "00003-00000003"


def test_monotributistas_usa_mono_anchor_en_categoria_corresponde(client, app):
    with app.app_context():
        categoria_a = Categoria(nombre="A", orden=1, tope_facturacion=Decimal("0.00"))
        categoria_b = Categoria(nombre="B", orden=2, tope_facturacion=Decimal("0.00"))
        db.session.add_all([categoria_a, categoria_b])
        db.session.commit()

        vigencia_2024 = Vigencia(fecha_desde=date(2024, 1, 1), fecha_hasta=date(2024, 12, 31))
        vigencia_2025 = Vigencia(fecha_desde=date(2025, 1, 1), fecha_hasta=None)
        db.session.add_all([vigencia_2024, vigencia_2025])
        db.session.commit()

        db.session.add_all(
            [
                CategoriaTope(
                    vigencia_id=vigencia_2024.id,
                    categoria_id=categoria_a.id,
                    tope_facturacion=Decimal("50.00"),
                ),
                CategoriaTope(
                    vigencia_id=vigencia_2024.id,
                    categoria_id=categoria_b.id,
                    tope_facturacion=Decimal("100.00"),
                ),
                CategoriaTope(
                    vigencia_id=vigencia_2025.id,
                    categoria_id=categoria_a.id,
                    tope_facturacion=Decimal("200.00"),
                ),
                CategoriaTope(
                    vigencia_id=vigencia_2025.id,
                    categoria_id=categoria_b.id,
                    tope_facturacion=Decimal("300.00"),
                ),
            ]
        )

        monotributista = Monotributista(
            razon_social="Demo SRL",
            cuit="20304050607",
            clave_fiscal="clave",
            categoria_actual_id=categoria_a.id,
            categoria_corresponde_id=categoria_a.id,
        )
        db.session.add(monotributista)
        db.session.commit()

        factura = Factura(
            monotributista_id=monotributista.id,
            fecha=date(2024, 9, 15),
            tipo_comp="C",
            numero_comp="00001-00000001",
            importe_total=Decimal("100.00"),
            fecha_desde=date(2024, 9, 1),
            fecha_hasta=date(2024, 9, 1),
        )
        db.session.add(factura)
        db.session.commit()

    login(client)
    response_2024 = client.get("/?tab=monotributistas&mono_anchor=2024-12")
    assert b'data-corresponde="B"' in response_2024.data

    response_2025 = client.get("/?tab=monotributistas&mono_anchor=2025-06")
    assert b'data-corresponde="A"' in response_2025.data


def test_pdf_import_falla_si_cuit_emisor_no_existe(monkeypatch, app):
    with app.app_context():
        factura_import = FacturaImport(
            status="pending",
            pdf_path="/tmp/demo.pdf",
            filename="demo.pdf",
            source="upload",
        )
        db.session.add(factura_import)
        db.session.commit()
        import_id = factura_import.id

    def fake_extract(_path):
        return {
            "fecha_emision": date(2025, 3, 10),
            "tipo_comp": "C",
            "punto_venta": "3",
            "numero_comp": "3",
            "importe_total": Decimal("1000.00"),
            "cuit_emisor": None,
            "fecha_desde": date(2025, 3, 10),
            "fecha_hasta": date(2025, 3, 10),
        }

    monkeypatch.setattr("website.pdf_jobs.extract_factura_data", fake_extract)
    monkeypatch.setattr("website.pdf_jobs.cleanup_pdf_path", lambda *_: None)

    with pytest.raises(Exception):
        process_factura_import(import_id)

    with app.app_context():
        db.session.remove()
        failed = db.session.get(FacturaImport, import_id)
        assert failed.status == "failed"
        assert failed.result_message == "Falta cuit_emisor del facturador"


def test_pdf_import_falla_por_duplicado(monkeypatch, app):
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

        existing = Factura(
            monotributista_id=monotributista.id,
            fecha=date(2025, 3, 10),
            tipo_comp="C",
            numero_comp="00003-00000010",
            importe_total=Decimal("1000.00"),
            fecha_desde=date(2025, 3, 10),
            fecha_hasta=date(2025, 3, 10),
        )
        db.session.add(existing)
        db.session.commit()

        factura_import = FacturaImport(
            monotributista_id=monotributista.id,
            status="pending",
            pdf_path="/tmp/demo.pdf",
            filename="demo.pdf",
            source="upload",
        )
        db.session.add(factura_import)
        db.session.commit()
        import_id = factura_import.id
        cuit_emisor = monotributista.cuit

    def fake_extract(_path):
        return {
            "fecha_emision": date(2025, 3, 10),
            "tipo_comp": "C",
            "punto_venta": "3",
            "numero_comp": "10",
            "importe_total": Decimal("1000.00"),
            "cuit_emisor": cuit_emisor,
            "fecha_desde": date(2025, 3, 10),
            "fecha_hasta": date(2025, 3, 10),
        }

    monkeypatch.setattr("website.pdf_jobs.extract_factura_data", fake_extract)
    monkeypatch.setattr("website.pdf_jobs.cleanup_pdf_path", lambda *_: None)

    with pytest.raises(Exception):
        process_factura_import(import_id)

    with app.app_context():
        db.session.remove()
        failed = db.session.get(FacturaImport, import_id)
        assert failed.status == "failed"
        assert (
            failed.result_message
            == "Ya existe una factura con ese numero y punto de venta para este monotributista."
        )


def test_pdf_import_factura_e_exige_exchange_rate(monkeypatch, app):
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

        factura_import = FacturaImport(
            monotributista_id=monotributista.id,
            status="pending",
            pdf_path="/tmp/demo.pdf",
            filename="demo.pdf",
            source="upload",
        )
        db.session.add(factura_import)
        db.session.commit()
        import_id = factura_import.id
        cuit_emisor = monotributista.cuit

    def fake_extract(_path):
        return {
            "fecha_emision": date(2025, 3, 10),
            "tipo_comp": "E",
            "punto_venta": "0",
            "numero_comp": "3",
            "importe_total_usd": Decimal("100.00"),
            "exchange_rate": None,
            "importe_total": Decimal("100.00"),
            "cuit_emisor": cuit_emisor,
            "fecha_desde": date(2025, 3, 10),
            "fecha_hasta": date(2025, 3, 10),
        }

    monkeypatch.setattr("website.pdf_jobs.extract_factura_data", fake_extract)
    monkeypatch.setattr("website.pdf_jobs.cleanup_pdf_path", lambda *_: None)

    with pytest.raises(Exception):
        process_factura_import(import_id)

    with app.app_context():
        db.session.remove()
        failed = db.session.get(FacturaImport, import_id)
        assert failed.status == "failed"
        assert (
            failed.result_message
            == "Faltan importe_total_usd o exchange_rate para factura E"
        )


def test_factura_imports_counts_usa_lote_activo_mas_reciente(client, app):
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

        base = datetime(2026, 2, 12, 9, 0, tzinfo=timezone.utc)
        db.session.add_all(
            [
                FacturaImport(
                    monotributista_id=monotributista.id,
                    batch_id="batch-old",
                    status="pending",
                    pdf_path="/tmp/old-pending.pdf",
                    source="upload",
                    created_at=base,
                ),
                FacturaImport(
                    monotributista_id=monotributista.id,
                    batch_id="batch-new",
                    status="done",
                    pdf_path="/tmp/new-done.pdf",
                    source="upload",
                    created_at=base.replace(minute=2),
                ),
                FacturaImport(
                    monotributista_id=monotributista.id,
                    batch_id="batch-new",
                    status="failed_viewed",
                    pdf_path="/tmp/new-failed.pdf",
                    source="upload",
                    created_at=base.replace(minute=3),
                ),
                FacturaImport(
                    monotributista_id=monotributista.id,
                    batch_id="batch-new",
                    status="pending",
                    pdf_path="/tmp/new-pending.pdf",
                    source="upload",
                    created_at=base.replace(minute=4),
                ),
                FacturaImport(
                    monotributista_id=monotributista.id,
                    batch_id="batch-new",
                    status="processing",
                    pdf_path="/tmp/new-processing.pdf",
                    source="upload",
                    created_at=base.replace(minute=5),
                ),
            ]
        )
        db.session.commit()

    login(client)
    response = client.get("/facturas/imports")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["has_active"] is True
    assert payload["active_batch_id"] == "batch-new"
    assert payload["counts"] == {
        "pending": 1,
        "processing": 1,
        "done": 1,
        "failed": 1,
    }


def test_factura_imports_counts_en_cero_sin_lote_activo(client, app):
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

        db.session.add_all(
            [
                FacturaImport(
                    monotributista_id=monotributista.id,
                    batch_id="batch-closed",
                    status="done",
                    pdf_path="/tmp/closed-done.pdf",
                    source="upload",
                ),
                FacturaImport(
                    monotributista_id=monotributista.id,
                    batch_id="batch-closed",
                    status="failed",
                    pdf_path="/tmp/closed-failed.pdf",
                    source="upload",
                ),
            ]
        )
        db.session.commit()

    login(client)
    response = client.get("/facturas/imports")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["has_active"] is False
    assert payload["active_batch_id"] is None
    assert payload["counts"] == {
        "pending": 0,
        "processing": 0,
        "done": 0,
        "failed": 0,
    }
