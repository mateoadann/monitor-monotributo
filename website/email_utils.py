from __future__ import annotations

import base64
import hashlib
import smtplib
import socket
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from email import encoders

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

from .models import SmtpConfig


def _get_fernet() -> Fernet:
    secret = current_app.config["SECRET_KEY"]
    digest = hashlib.sha256(secret.encode()).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_password(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_password(ciphertext: str) -> str:
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return ""


def get_smtp_config() -> SmtpConfig | None:
    return SmtpConfig.get_config()


def send_email(
    to: str | list[str],
    subject: str,
    body_html: str,
    body_text: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> tuple[bool, str]:
    config = get_smtp_config()
    if config is None:
        raise ValueError("No hay configuración SMTP. Configure el servidor de correo primero.")

    password = decrypt_password(config.password_encrypted)

    msg_alternative = MIMEMultipart("alternative")
    if body_text:
        msg_alternative.attach(MIMEText(body_text, "plain", "utf-8"))
    msg_alternative.attach(MIMEText(body_html, "html", "utf-8"))

    if attachments:
        msg = MIMEMultipart("mixed")
        msg.attach(msg_alternative)
        for filename, data, mimetype in attachments:
            maintype, subtype = mimetype.split("/", 1)
            part = MIMEBase(maintype, subtype)
            part.set_payload(data)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)
    else:
        msg = msg_alternative

    msg["Subject"] = subject
    msg["From"] = formataddr((config.from_name, config.from_email))
    recipients = [to] if isinstance(to, str) else to
    msg["To"] = ", ".join(recipients)

    try:
        with smtplib.SMTP(config.host, config.port, timeout=10) as server:
            if config.use_tls:
                server.starttls()
            server.login(config.username, password)
            server.sendmail(config.from_email, recipients, msg.as_string())
        return (True, "Email enviado correctamente")
    except smtplib.SMTPAuthenticationError:
        return (False, "Error de autenticación SMTP. Verifique usuario y contraseña.")
    except smtplib.SMTPException as e:
        return (False, str(e))
    except socket.timeout:
        return (False, "Tiempo de conexión agotado. Verifique host y puerto SMTP.")
    except Exception as e:
        return (False, str(e))
