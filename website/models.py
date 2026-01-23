from __future__ import annotations

from datetime import datetime

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
    topes = db.relationship(
        "CategoriaTope",
        back_populates="categoria",
        cascade="all, delete-orphan",
    )


class Vigencia(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fecha_desde = db.Column(db.Date, nullable=False)
    fecha_hasta = db.Column(db.Date)
    topes = db.relationship(
        "CategoriaTope",
        back_populates="vigencia",
        cascade="all, delete-orphan",
    )


class CategoriaTope(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    vigencia_id = db.Column(db.Integer, db.ForeignKey("vigencia.id"), nullable=False)
    categoria_id = db.Column(db.Integer, db.ForeignKey("categoria.id"), nullable=False)
    tope_facturacion = db.Column(db.Numeric(14, 2), nullable=False)

    categoria = db.relationship("Categoria", back_populates="topes")
    vigencia = db.relationship("Vigencia", back_populates="topes")

    __table_args__ = (
        db.UniqueConstraint("vigencia_id", "categoria_id", name="uq_vigencia_categoria"),
    )


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
    return
