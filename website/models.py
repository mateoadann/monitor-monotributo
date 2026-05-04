from __future__ import annotations

from datetime import datetime, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash


db = SQLAlchemy()


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="visor")
    is_active_user = db.Column(db.Boolean, nullable=False, default=True)
    nombre = db.Column(db.String(160), nullable=False, default="")

    ROLES = ("admin", "contador", "visor")
    ROLE_LABELS = {"admin": "Administrador", "contador": "Contador", "visor": "Visor"}

    @property
    def is_active(self):
        return self.is_active_user

    def can_edit_data(self) -> bool:
        return self.role in ("admin", "contador")

    def can_manage_config(self) -> bool:
        return self.role == "admin"

    def can_manage_users(self) -> bool:
        return self.role == "admin"

    def can_run_rpa(self) -> bool:
        return self.role in ("admin", "contador")


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
    cuit = db.Column(db.String(20), nullable=False, unique=True)
    clave_fiscal = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), nullable=True)
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
    concepto = db.Column(db.Text)

    monotributista = db.relationship("Monotributista", back_populates="facturas")


class SmtpConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    host = db.Column(db.String(255), nullable=False)
    port = db.Column(db.Integer, nullable=False, default=587)
    username = db.Column(db.String(255), nullable=False)
    password_encrypted = db.Column(db.Text, nullable=False)
    use_tls = db.Column(db.Boolean, nullable=False, default=True)
    from_email = db.Column(db.String(255), nullable=False)
    from_name = db.Column(db.String(255), nullable=False, default="Monitor Monotributo")
    updated_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    @classmethod
    def get_config(cls) -> SmtpConfig | None:
        return cls.query.first()


class EmailTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    subject = db.Column(
        db.String(255),
        nullable=False,
        default="Situación Monotributo - {periodo}",
    )
    body_html = db.Column(
        db.Text,
        nullable=False,
        default=(
            "Hola {nombre},\n"
            "\n"
            "Te adjuntamos el informe de tu situación de monotributo "
            "correspondiente al período {periodo}.\n"
            "\n"
            "Ante cualquier duda, no dudes en consultarnos.\n"
            "\n"
            "Saludos,\n"
            "Monitor Monotributo"
        ),
    )
    updated_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    @classmethod
    def get_template(cls) -> EmailTemplate | None:
        return cls.query.first()

    @classmethod
    def get_or_default(cls) -> EmailTemplate:
        tpl = cls.query.first()
        if tpl:
            return tpl
        return cls(
            subject="Situación Monotributo - {periodo}",
            body_html=(
                "<p>Hola {nombre},</p>"
                "<p>Te adjuntamos el informe de tu situación de monotributo "
                "correspondiente al período <strong>{periodo}</strong>.</p>"
                "<p>Ante cualquier duda, no dudes en consultarnos.</p>"
                "<p>Saludos,<br>Monitor Monotributo</p>"
            ),
        )


class RpaSchedule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    day_of_week = db.Column(db.String(20), nullable=False)  # comma-separated: "1,3,5"
    hour = db.Column(db.Integer, nullable=False, default=8)
    minute = db.Column(db.Integer, nullable=False, default=0)
    monotributista_ids = db.Column(db.Text, nullable=False, default="[]")  # JSON list
    lookback_days = db.Column(db.Integer, nullable=False, default=365)
    send_report = db.Column(db.Boolean, nullable=False, default=True)
    report_email = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    last_run_at = db.Column(db.DateTime, nullable=True)
    last_run_status = db.Column(db.String(20), nullable=True)

    @classmethod
    def get_active_schedules(cls):
        return cls.query.filter_by(is_active=True).all()

    def get_monotributista_ids(self) -> list[int]:
        import json
        try:
            return json.loads(self.monotributista_ids)
        except (json.JSONDecodeError, TypeError):
            return []

    def set_monotributista_ids(self, ids: list[int]) -> None:
        import json
        self.monotributista_ids = json.dumps(ids)


class EmailMassSchedule(db.Model):
    __tablename__ = "email_mass_schedules"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    days_of_month = db.Column(db.String(255), nullable=False, default="")  # CSV "1,15"
    hour = db.Column(db.Integer, nullable=False, default=8)
    minute = db.Column(db.Integer, nullable=False, default=0)
    monos_mode = db.Column(
        db.String(20), nullable=False, default="manual"
    )  # "todos" | "por_categoria" | "manual"
    monotributista_ids = db.Column(db.Text, nullable=False, default="[]")  # JSON list
    categoria_id = db.Column(
        db.Integer, db.ForeignKey("categoria.id"), nullable=True
    )
    lookback_days = db.Column(db.Integer, nullable=False, default=365)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    send_report = db.Column(db.Boolean, nullable=False, default=True)
    report_email = db.Column(db.String(255), nullable=True)
    last_run_at = db.Column(db.DateTime, nullable=True)
    last_run_status = db.Column(db.String(20), nullable=True)
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    categoria = db.relationship("Categoria")

    @classmethod
    def get_active_schedules(cls):
        return cls.query.filter_by(is_active=True).all()

    def get_days_of_month(self) -> list[int]:
        days: list[int] = []
        for raw in (self.days_of_month or "").split(","):
            raw = raw.strip()
            if raw.isdigit():
                d = int(raw)
                if 1 <= d <= 31:
                    days.append(d)
        return days

    def set_days_of_month(self, days: list[int]) -> None:
        clean = sorted({int(d) for d in days if 1 <= int(d) <= 31})
        self.days_of_month = ",".join(str(d) for d in clean)

    def get_monotributista_ids(self) -> list[int]:
        import json
        try:
            data = json.loads(self.monotributista_ids or "[]")
            return [int(x) for x in data if str(x).lstrip("-").isdigit()]
        except (json.JSONDecodeError, TypeError, ValueError):
            return []

    def set_monotributista_ids(self, ids: list[int]) -> None:
        import json
        self.monotributista_ids = json.dumps([int(i) for i in ids])


def resolve_monos_for_email_schedule(schedule: "EmailMassSchedule") -> list[int]:
    """Resuelve la lista efectiva de IDs de monotributistas según monos_mode."""
    if schedule.monos_mode == "todos":
        return [m.id for m in Monotributista.query.order_by(Monotributista.id).all()]
    if schedule.monos_mode == "por_categoria" and schedule.categoria_id:
        return [
            m.id
            for m in Monotributista.query.filter_by(
                categoria_actual_id=schedule.categoria_id
            )
            .order_by(Monotributista.id)
            .all()
        ]
    # manual (default)
    return schedule.get_monotributista_ids()


class FacturaImport(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.String(32), index=True)
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    processed_at = db.Column(db.DateTime)
    status = db.Column(db.String(20), nullable=False, default="pending")
    error = db.Column(db.Text)
    result_message = db.Column(db.Text)
    pdf_path = db.Column(db.String(255), nullable=False)
    filename = db.Column(db.String(255))
    source = db.Column(db.String(30), nullable=False, default="upload")
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
    return db.session.get(User, int(user_id))


def create_admin_if_missing() -> None:
    admin = User.query.filter_by(username="admin").first()
    if admin:
        if admin.role != "admin":
            admin.role = "admin"
            db.session.commit()
        return
    user = User(
        username="admin",
        password_hash=generate_password_hash("admin"),
        role="admin",
        nombre="Administrador",
    )
    db.session.add(user)
    db.session.commit()


def seed_data() -> None:
    return
