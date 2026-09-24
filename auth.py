"""
auth.py — Inicio de sesión contra la tabla usuarios de clinico.db (lógica pura, sin Streamlit).

  * Solo entran cuentas ACTIVAS.
  * 5 intentos fallidos bloquean la cuenta 15 minutos (columnas intentos_fallidos y bloqueado_hasta del esquema).
  * Cada intento queda en auditoria_accesos (LOGIN_OK / LOGIN_FALLIDO / LOGIN_BLOQUEADO / LOGOUT).
  * El mensaje de error no dice si falló el usuario o la contraseña (no revela qué cuentas existen).

Contraseñas nuevas o cambiadas: PBKDF2-SHA256 con sal aleatoria y 200.000 iteraciones (biblioteca estándar).
Las cuentas sembradas de la demo usan sha256 sin sal (contraseña "demo"); al cambiar la clave pasan a PBKDF2.

Recuperación de contraseña: código de 6 dígitos, válido 15 minutos, un solo uso, máximo 5 intentos y 3 códigos
por hora. Solo se guarda su huella. Si hay correo configurado (SMTP_* en .env) se envía por email; si no, la
pantalla lo muestra marcado como "modo demostración".
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
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


PBKDF2_ITERATIONS = 200_000
CODE_MINUTES, CODE_ATTEMPTS, CODES_PER_HOUR = 15, 5, 3


def _hash(password: str) -> str:
    return "sha256$" + hashlib.sha256(password.encode()).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS).hex()
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(stored: str, password: str) -> bool:
    if stored.startswith("pbkdf2$"):
        _, iterations, salt, digest = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", (password or "").encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(calc, digest)
    return hmac.compare_digest(stored, _hash(password or ""))


def password_problem(password: str) -> str | None:
    """Política mínima: 8 caracteres con letras y números."""
    if len(password or "") < 8:
        return "La contraseña debe tener al menos 8 caracteres"
    if not any(c.isdigit() for c in password) or not any(c.isalpha() for c in password):
        return "La contraseña debe combinar letras y números"
    return None


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
        if not verify_password(user["hash_password"], password or ""):
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


def change_password(conn: sqlite3.Connection, user_id: int, current: str, new: str, now: str) -> None:
    user = conn.execute("SELECT hash_password FROM usuarios WHERE id = ?", (user_id,)).fetchone()
    if user is None or not verify_password(user[0], current):
        raise sqlite3.IntegrityError("La contraseña actual no es correcta")
    _set_password(conn, user_id, new, now, "CLAVE_CAMBIADA")


def _set_password(conn: sqlite3.Connection, user_id: int, new: str, now: str, action: str) -> None:
    problem = password_problem(new)
    if problem:
        raise sqlite3.IntegrityError(problem)
    with conn:
        conn.execute("UPDATE usuarios SET hash_password = ?, debe_cambiar_clave = 0, intentos_fallidos = 0, "
                     "bloqueado_hasta = NULL WHERE id = ?", (hash_password(new), user_id))
        _audit(conn, user_id, action, "Contraseña actualizada", now)


def _code_hash(user_id: int, code: str) -> str:
    return hashlib.sha256(f"hslv-recuperacion-{user_id}-{code}".encode()).hexdigest()


def _find_user(conn: sqlite3.Connection, identifier: str):
    ident = (identifier or "").strip().lower()
    return conn.execute("SELECT * FROM usuarios WHERE (lower(usuario) = ? OR lower(correo) = ?) "
                        "AND estado_cuenta = 'ACTIVO'", (ident, ident)).fetchone()


def request_recovery(conn: sqlite3.Connection, identifier: str, now: str) -> tuple[int | None, str | None, str | None]:
    """Genera un código si la cuenta existe y está activa. Devuelve (user_id, código, correo) o (None, None, None).
    La pantalla responde igual en ambos casos para no revelar qué cuentas existen."""
    user = _find_user(conn, identifier)
    if user is None:
        with conn:
            _audit(conn, None, "RECUPERACION_SOLICITADA", f"Identificador no encontrado: {identifier[:40]}", now)
        return None, None, None
    hour_ago = (datetime.strptime(now, FMT) - timedelta(hours=1)).strftime(FMT)
    recent = conn.execute("SELECT COUNT(*) FROM recuperacion_clave WHERE usuario_id = ? AND creado_en > ?",
                          (user["id"], hour_ago)).fetchone()[0]
    if recent >= CODES_PER_HOUR:
        raise sqlite3.IntegrityError("Ya se enviaron varios códigos en la última hora. Espera un momento.")
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires = (datetime.strptime(now, FMT) + timedelta(minutes=CODE_MINUTES)).strftime(FMT)
    with conn:
        conn.execute("UPDATE recuperacion_clave SET usado_en = ? WHERE usuario_id = ? AND usado_en IS NULL",
                     (now, user["id"]))  # un código nuevo invalida los anteriores
        conn.execute("INSERT INTO recuperacion_clave(usuario_id, codigo_hash, creado_en, expira_en) VALUES (?,?,?,?)",
                     (user["id"], _code_hash(user["id"], code), now, expires))
        _audit(conn, user["id"], "RECUPERACION_SOLICITADA", "Código de recuperación generado", now)
    return user["id"], code, user["correo"]


def reset_with_code(conn: sqlite3.Connection, identifier: str, code: str, new: str, now: str) -> None:
    user = _find_user(conn, identifier)
    row = None if user is None else conn.execute(
        "SELECT * FROM recuperacion_clave WHERE usuario_id = ? AND usado_en IS NULL ORDER BY id DESC LIMIT 1",
        (user["id"],)).fetchone()
    if row is None or row["expira_en"] < now or row["intentos"] >= CODE_ATTEMPTS:
        raise sqlite3.IntegrityError("El código no es válido o ya venció. Solicita uno nuevo.")
    if not hmac.compare_digest(row["codigo_hash"], _code_hash(user["id"], (code or "").strip())):
        with conn:
            conn.execute("UPDATE recuperacion_clave SET intentos = intentos + 1 WHERE id = ?", (row["id"],))
            _audit(conn, user["id"], "RECUPERACION_FALLIDA", f"Código incorrecto ({row['intentos'] + 1}/{CODE_ATTEMPTS})", now)
        raise sqlite3.IntegrityError("El código no es válido o ya venció. Solicita uno nuevo.")
    problem = password_problem(new)
    if problem:
        raise sqlite3.IntegrityError(problem)
    with conn:
        conn.execute("UPDATE recuperacion_clave SET usado_en = ? WHERE id = ?", (now, row["id"]))
    _set_password(conn, user["id"], new, now, "CLAVE_RECUPERADA")


# ---------------------------------------------------------------------------
# Ingreso con Google (OpenID Connect vía st.login). Google ya verificó el correo; aquí solo se decide si ese
# correo corresponde a UNA cuenta activa del hospital. Nadie entra por existir en Google.
# ---------------------------------------------------------------------------
GOOGLE_NOT_LINKED = ("Tu cuenta de Google no está vinculada a ningún usuario del hospital. El personal debe pedirle "
                     "al administrador que registre ese correo; los pacientes pueden crear su cuenta abajo.")


def login_google(conn: sqlite3.Connection, email: str, now: str, verified: bool = True) -> LoginResult:
    email = (email or "").strip().lower()
    if not email or not verified:
        return LoginResult(None, "Google no confirmó el correo de la cuenta.")
    with conn:
        rows = conn.execute("SELECT * FROM usuarios WHERE lower(correo) = ?", (email,)).fetchall()
        if len(rows) != 1:
            detail = "sin usuario" if not rows else "correo repetido en varias cuentas"
            _audit(conn, None, "LOGIN_FALLIDO", f"Google: {email[:60]} ({detail})", now)
            return LoginResult(None, GOOGLE_NOT_LINKED if not rows else
                               "Ese correo está en varias cuentas; pide al administrador que lo corrija.")
        user = rows[0]
        if user["estado_cuenta"] != "ACTIVO":
            _audit(conn, user["id"], "LOGIN_FALLIDO", f"Google: cuenta {user['estado_cuenta'].lower()}", now)
            return LoginResult(None, f"La cuenta está {user['estado_cuenta'].lower().replace('_', ' ')}. "
                                     "Contacta al administrador.")
        conn.execute("UPDATE usuarios SET intentos_fallidos = 0, bloqueado_hasta = NULL, ultimo_acceso = ? "
                     "WHERE id = ?", (now, user["id"]))
        _audit(conn, user["id"], "LOGIN_OK", "Inicio de sesión con Google", now)
    return LoginResult(user["id"], "ok")


# ---------------------------------------------------------------------------
# Cuenta del paciente en el portal: documento + el correo que registró admisiones. El código llega a ESE correo,
# así nadie puede abrir la cuenta de otro solo con saber su documento.
# ---------------------------------------------------------------------------
SIGNUP_GENERIC = ("Si el documento y el correo coinciden con los registrados en admisiones, te enviamos un código de "
                  f"6 dígitos que vence en {CODE_MINUTES} minutos.")


def _signup_hash(id_paciente: int, code: str) -> str:
    return hashlib.sha256(f"hslv-registro-{id_paciente}-{code}".encode()).hexdigest()


def _signup_patient(conn: sqlite3.Connection, documento: str, correo: str):
    doc = "".join((documento or "").split())
    mail = (correo or "").strip().lower()
    if not doc or not mail:
        return None
    return conn.execute("SELECT * FROM pacientes_clinicos WHERE numero_documento = ? AND lower(correo) = ? "
                        "AND estado = 'ACTIVO'", (doc, mail)).fetchone()


def request_signup(conn: sqlite3.Connection, documento: str, correo: str, now: str) -> tuple[int | None, str | None]:
    """Devuelve (id_paciente, código) si procede; (None, None) si no (la pantalla responde igual en ambos casos).
    Errores explícitos solo cuando ya hay cuenta o se excede el límite de códigos."""
    patient = _signup_patient(conn, documento, correo)
    if patient is None:
        with conn:
            _audit(conn, None, "REGISTRO_PACIENTE", "Documento/correo sin coincidencia", now)
        return None, None
    pid = patient["id_paciente"]
    if conn.execute("SELECT 1 FROM usuarios WHERE id_paciente = ? OR lower(correo) = ?",
                    (pid, patient["correo"].lower())).fetchone():
        raise sqlite3.IntegrityError("Ya existe una cuenta con esos datos. Usa \"¿Olvidaste tu contraseña?\".")
    hour_ago = (datetime.strptime(now, FMT) - timedelta(hours=1)).strftime(FMT)
    if conn.execute("SELECT COUNT(*) FROM registro_pacientes WHERE id_paciente = ? AND creado_en > ?",
                    (pid, hour_ago)).fetchone()[0] >= CODES_PER_HOUR:
        raise sqlite3.IntegrityError("Ya se enviaron varios códigos en la última hora. Espera un momento.")
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires = (datetime.strptime(now, FMT) + timedelta(minutes=CODE_MINUTES)).strftime(FMT)
    with conn:
        conn.execute("UPDATE registro_pacientes SET usado_en = ? WHERE id_paciente = ? AND usado_en IS NULL",
                     (now, pid))
        conn.execute("INSERT INTO registro_pacientes(id_paciente, correo, codigo_hash, creado_en, expira_en) "
                     "VALUES (?,?,?,?,?)", (pid, patient["correo"].lower(), _signup_hash(pid, code), now, expires))
        _audit(conn, None, "REGISTRO_PACIENTE", f"Código de registro generado (paciente {pid})", now)
    return pid, code


def complete_signup(conn: sqlite3.Connection, documento: str, correo: str, code: str, password: str,
                    now: str) -> int:
    """Valida el código y crea la cuenta PACIENTE (usuario = correo) vinculada a su registro. Devuelve el id."""
    patient = _signup_patient(conn, documento, correo)
    row = None if patient is None else conn.execute(
        "SELECT * FROM registro_pacientes WHERE id_paciente = ? AND usado_en IS NULL ORDER BY id DESC LIMIT 1",
        (patient["id_paciente"],)).fetchone()
    if row is None or row["expira_en"] < now or row["intentos"] >= CODE_ATTEMPTS:
        raise sqlite3.IntegrityError("El código no es válido o ya venció. Solicita uno nuevo.")
    if not hmac.compare_digest(row["codigo_hash"], _signup_hash(patient["id_paciente"], (code or "").strip())):
        with conn:
            conn.execute("UPDATE registro_pacientes SET intentos = intentos + 1 WHERE id = ?", (row["id"],))
        raise sqlite3.IntegrityError("El código no es válido o ya venció. Solicita uno nuevo.")
    problem = password_problem(password)
    if problem:
        raise sqlite3.IntegrityError(problem)
    mail = patient["correo"].lower()
    if conn.execute("SELECT 1 FROM usuarios WHERE id_paciente = ? OR lower(usuario) = ? OR lower(correo) = ?",
                    (patient["id_paciente"], mail, mail)).fetchone():
        raise sqlite3.IntegrityError("Ya existe una cuenta con esos datos. Usa \"¿Olvidaste tu contraseña?\".")
    name = " ".join(filter(None, [patient["nombres"].split()[0], (patient["apellidos"] or "").split()[0]
                                  if patient["apellidos"] else None]))
    with conn:
        role = conn.execute("SELECT id FROM roles WHERE codigo = 'PACIENTE'").fetchone()[0]
        uid = conn.execute("INSERT INTO usuarios(usuario, hash_password, nombre_mostrado, rol_id, estado_cuenta, "
                           "id_paciente, correo, creado_en) VALUES (?,?,?,?, 'ACTIVO', ?, ?, ?)",
                           (mail, hash_password(password), name, role, patient["id_paciente"], mail, now)).lastrowid
        conn.execute("UPDATE registro_pacientes SET usado_en = ? WHERE id = ?", (now, row["id"]))
        _audit(conn, uid, "CUENTA_PACIENTE_CREADA", "Autorregistro verificado por correo", now)
    return uid
