"""
auth.py — Inicio de sesión contra la tabla usuarios de clinico.db (lógica pura, sin Streamlit).

  * Solo entran cuentas ACTIVAS.
  * 5 intentos fallidos bloquean la cuenta 15 minutos (columnas intentos_fallidos y bloqueado_hasta del esquema).
  * Cada intento queda en auditoria_accesos (LOGIN_OK / LOGIN_FALLIDO / LOGIN_BLOQUEADO / LOGOUT).
  * El mensaje de error no dice si falló el usuario o la contraseña (no revela qué cuentas existen).

Las contraseñas de la demo se guardan como sha256 (ver demo_seed._hash); en producción: argon2/bcrypt con sal.
"""
from __future__ import annotations

import hashlib
import hmac
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

FMT = "%Y-%m-%d %H:%M:%S"
MAX_ATTEMPTS = 5
LOCK_MINUTES = 15
GENERIC_ERROR = "Usuario o contraseña incorrectos."


@dataclass
class LoginResult:
    user_id: int | None
    message: str


def _hash(password: str) -> str:
    return "sha256$" + hashlib.sha256(password.encode()).hexdigest()


def _audit(conn: sqlite3.Connection, user_id: int | None, action: str, detail: str, now: str) -> None:
    conn.execute("INSERT INTO auditoria_accesos(fecha, usuario_id, accion, recurso) VALUES (?,?,?,?)",
                 (now, user_id, action, detail))


def login(conn: sqlite3.Connection, username: str, password: str, now: str) -> LoginResult:
    username = (username or "").strip().lower()
    with conn:
        user = conn.execute("SELECT * FROM usuarios WHERE lower(usuario) = ?", (username,)).fetchone()
        if user is None:
            _audit(conn, None, "LOGIN_FALLIDO", f"Usuario inexistente: {username[:40]}", now)
            return LoginResult(None, GENERIC_ERROR)
        if user["bloqueado_hasta"] and user["bloqueado_hasta"] > now:
            _audit(conn, user["id"], "LOGIN_BLOQUEADO", "Intento con la cuenta bloqueada", now)
            until = datetime.strptime(user["bloqueado_hasta"], FMT)
            return LoginResult(None, f"Cuenta bloqueada por intentos fallidos hasta las {until:%H:%M}.")
        if not hmac.compare_digest(user["hash_password"], _hash(password or "")):
            attempts = user["intentos_fallidos"] + 1
            lock = (datetime.strptime(now, FMT) + timedelta(minutes=LOCK_MINUTES)).strftime(FMT) \
                if attempts >= MAX_ATTEMPTS else None
            conn.execute("UPDATE usuarios SET intentos_fallidos = ?, bloqueado_hasta = ? WHERE id = ?",
                         (0 if lock else attempts, lock, user["id"]))
            _audit(conn, user["id"], "LOGIN_FALLIDO", f"Contraseña incorrecta ({attempts}/{MAX_ATTEMPTS})", now)
            if lock:
                return LoginResult(None, f"Demasiados intentos. La cuenta queda bloqueada {LOCK_MINUTES} minutos.")
            return LoginResult(None, GENERIC_ERROR)
        if user["estado_cuenta"] != "ACTIVO":
            _audit(conn, user["id"], "LOGIN_FALLIDO", f"Cuenta {user['estado_cuenta'].lower()}", now)
            return LoginResult(None, f"La cuenta está {user['estado_cuenta'].lower().replace('_', ' ')}. "
                                     "Contacta al administrador.")
        conn.execute("UPDATE usuarios SET intentos_fallidos = 0, bloqueado_hasta = NULL, ultimo_acceso = ? "
                     "WHERE id = ?", (now, user["id"]))
        _audit(conn, user["id"], "LOGIN_OK", "Inicio de sesión", now)
    return LoginResult(user["id"], "ok")


def logout(conn: sqlite3.Connection, user_id: int, now: str) -> None:
    with conn:
        _audit(conn, user_id, "LOGOUT", "Cierre de sesión", now)


def log_denied(conn: sqlite3.Connection, user_id: int, permission: str, detail: str, now: str) -> None:
    """Registra en la bitácora un intento de usar algo fuera del alcance del rol (p. ej. en el asistente)."""
    with conn:
        _audit(conn, user_id, f"DENEGADO {permission}", detail[:200], now)
