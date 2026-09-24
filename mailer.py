"""
mailer.py — Envío de correos (códigos de recuperación de contraseña y de registro de pacientes) por SMTP, si está configurado.

Variables en .env: SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM.
Con Gmail: activar verificación en dos pasos y crear una "contraseña de aplicación".
Sin configuración, configured() es False y la app muestra el código en pantalla (modo demostración).
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage


def configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD"))


def _send(to: str, subject: str, body: str) -> bool:
    if not configured() or not to:
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER")
    msg["To"] = to
    msg.set_content(body + "\n\nHospital Susana López de Valencia · Centro de mando")
    try:
        with smtplib.SMTP(os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT", "587")), timeout=10) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD"))
            server.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException):
        return False


def send_recovery_code(to: str, code: str, minutes: int) -> bool:
    return _send(to, "Código para restablecer tu contraseña · HSLV",
                 f"Tu código es {code}. Vence en {minutes} minutos y sirve una sola vez.\n\n"
                 "Si no pediste este cambio, ignora este mensaje y avisa al administrador.")


def send_signup_code(to: str, code: str, minutes: int) -> bool:
    return _send(to, "Crea tu cuenta del portal de pacientes · HSLV",
                 f"Tu código para crear la cuenta es {code}. Vence en {minutes} minutos y sirve una sola vez.\n\n"
                 "Si no fuiste tú, ignora este mensaje.")


def send_text(to: str, subject: str, body: str) -> bool:
    """Correo simple (p. ej. la cita para registrarse). False si no hay SMTP configurado."""
    return _send(to, subject, body)
