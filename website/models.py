from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash


db = SQLAlchemy()


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)


class Categoria(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(40), unique=True, nullable=False)
    orden = db.Column(db.Integer, nullable=False)
    tope_facturacion = db.Column(db.Numeric(14, 2), nullable=False)


class Monotributista(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    razon_social = db.Column(db.String(160), nullable=False)
    cuit = db.Column(db.String(20), nullable=False)
    clave_fiscal = db.Column(db.String(120), nullable=False)
    categoria_actual_id = db.Column(db.Integer, db.ForeignKey("categoria.id"))
    categoria_corresponde_id = db.Column(db.Integer, db.ForeignKey("categoria.id"))

    categoria_actual = db.relationship(
        "Categoria", foreign_keys=[categoria_actual_id], lazy="joined"
    )
    categoria_corresponde = db.relationship(
        "Categoria", foreign_keys=[categoria_corresponde_id], lazy="joined"
    )
    facturas = db.relationship(
        "Factura", back_populates="monotributista", cascade="all, delete-orphan"
    )


class Factura(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    monotributista_id = db.Column(db.Integer, db.ForeignKey("monotributista.id"))
    fecha = db.Column(db.Date, nullable=False)
    tipo_comp = db.Column(db.String(10), nullable=False)
    numero_comp = db.Column(db.String(30), nullable=False)
    cuit_receptor = db.Column(db.String(20))
    razon_social_receptor = db.Column(db.String(160))
    importe_total = db.Column(db.Numeric(14, 2), nullable=False)
    fecha_desde = db.Column(db.Date)
    fecha_hasta = db.Column(db.Date)
    concepto = db.Column(db.String(240))

    monotributista = db.relationship("Monotributista", back_populates="facturas")


class FacturaImport(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    status = db.Column(db.String(20), nullable=False, default="pending")
    error = db.Column(db.Text)
    pdf_path = db.Column(db.String(255), nullable=False)
    extracted_data = db.Column(db.Text)
    monotributista_id = db.Column(db.Integer, db.ForeignKey("monotributista.id"))
    factura_id = db.Column(db.Integer, db.ForeignKey("factura.id"))

    monotributista = db.relationship("Monotributista")
    factura = db.relationship("Factura")


def authenticate(username: str, password: str) -> User | None:
    user = User.query.filter_by(username=username).first()
    if not user:
        return None
    if not check_password_hash(user.password_hash, password):
        return None
    return user


def get_user_by_id(user_id: str) -> User | None:
    if not user_id:
        return None
    return User.query.get(int(user_id))


def create_admin_if_missing() -> None:
    if User.query.filter_by(username="admin").first():
        return
    user = User(username="admin", password_hash=generate_password_hash("admin"))
    db.session.add(user)
    db.session.commit()


def seed_data() -> None:
    if Categoria.query.first():
        return

    categorias = [
        Categoria(nombre="A", orden=1, tope_facturacion=Decimal("8500000.00")),
        Categoria(nombre="B", orden=2, tope_facturacion=Decimal("11000000.00")),
        Categoria(nombre="C", orden=3, tope_facturacion=Decimal("13000000.00")),
        Categoria(nombre="D", orden=4, tope_facturacion=Decimal("15000000.00")),
        Categoria(nombre="E", orden=5, tope_facturacion=Decimal("17000000.00")),
        Categoria(nombre="F", orden=6, tope_facturacion=Decimal("19000000.00")),
    ]
    db.session.add_all(categorias)
    db.session.flush()

    monotributistas = [
        Monotributista(
            razon_social="Gomez Servicios SRL",
            cuit="30-71234567-2",
            clave_fiscal="12345678",
            categoria_actual_id=categorias[0].id,
            categoria_corresponde_id=categorias[1].id,
        ),
        Monotributista(
            razon_social="Lucia Perez",
            cuit="27-28765432-9",
            clave_fiscal="87654321",
            categoria_actual_id=categorias[2].id,
            categoria_corresponde_id=categorias[2].id,
        ),
        Monotributista(
            razon_social="Estudio Valdez",
            cuit="30-70111222-8",
            clave_fiscal="55667788",
            categoria_actual_id=categorias[5].id,
            categoria_corresponde_id=categorias[4].id,
        ),
    ]
    db.session.add_all(monotributistas)
    db.session.flush()

    facturas = [
        Factura(
            monotributista_id=monotributistas[0].id,
            fecha=date(2025, 2, 5),
            tipo_comp="B",
            numero_comp="0002-00000147",
            cuit_receptor="30-70999888-1",
            razon_social_receptor="Insumos Atlas SA",
            importe_total=Decimal("1245300.50"),
            fecha_desde=date(2025, 2, 1),
            fecha_hasta=date(2025, 2, 28),
            concepto="Servicios de mantenimiento",
        ),
        Factura(
            monotributista_id=monotributistas[1].id,
            fecha=date(2025, 3, 18),
            tipo_comp="NCB",
            numero_comp="0003-00000412",
            cuit_receptor="27-10101010-7",
            razon_social_receptor="Taller Sur",
            importe_total=Decimal("-92000.00"),
            fecha_desde=date(2025, 3, 15),
            fecha_hasta=date(2025, 3, 20),
            concepto="Nota de credito parcial",
        ),
        Factura(
            monotributista_id=monotributistas[2].id,
            fecha=date(2025, 4, 12),
            tipo_comp="B",
            numero_comp="0001-00007801",
            cuit_receptor="30-60606060-2",
            razon_social_receptor="Consultora Norte",
            importe_total=Decimal("540000.00"),
            fecha_desde=date(2025, 4, 1),
            fecha_hasta=date(2025, 4, 30),
            concepto="Asesoria fiscal",
        ),
    ]
    db.session.add_all(facturas)
    db.session.commit()
