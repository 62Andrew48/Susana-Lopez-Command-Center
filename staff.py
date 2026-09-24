"""
staff.py — Usuarios y turnos de trabajo del personal (administración). clinico.db, sin Streamlit.

  * Crear usuarios con contraseña temporal (se obliga a cambiarla al entrar), suspender/reactivar, restablecer.
  * Asignar turnos (mañana, tarde, noche, día completo o guardia) a médicos, enfermería y admisiones; no se
    permiten turnos cruzados para la misma persona; solo se borran turnos que aún no empiezan.
  * El turno es lo que habilita el acceso clínico (fuera de turno = "romper el vidrio"), así que asignarlo
    bien es parte de la seguridad.
"""
from __future__ import annotations

import secrets
import sqlite3
import string
from datetime import datetime, timedelta

import auth

FMT = "%Y-%m-%d %H:%M:%S"
SHIFT_TEMPLATES = {  # nombre: (hora inicio, horas de duración, tipo)
    "Mañana (07:00-13:00)": (7, 6, "TURNO"),
    "Tarde (13:00-19:00)": (13, 6, "TURNO"),
    "Noche (19:00-07:00)": (19, 12, "TURNO"),
    "Día completo (07:00-19:00)": (7, 12, "TURNO"),
    "Guardia localizable 24 h (07:00-07:00)": (7, 24, "GUARDIA"),
}
SERVICES = ("Urgencias", "Hospitalización", "UCI", "Pediatría", "Gineco-obstetricia", "Cirugía", "Consulta externa",
            "Farmacia", "Admisiones")
ROLE_IDS = {"ADMIN": 1, "DOCTOR": 2, "ENFERMERIA": 3, "PACIENTE": 4, "FACTURACION": 5}


def temp_password() -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(10))
        if any(c.isdigit() for c in pwd) and any(c.isalpha() for c in pwd):
            return pwd


def users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT u.id, u.usuario, u.nombre_mostrado, r.codigo AS rol, r.nombre AS rol_nombre, u.estado_cuenta,
               u.registro_profesional, u.especialidad, u.correo, u.id_paciente, u.ultimo_acceso, u.debe_cambiar_clave
          FROM usuarios u JOIN roles r ON r.id = u.rol_id ORDER BY r.id, u.nombre_mostrado""").fetchall()


def create_user(conn: sqlite3.Connection, *, admin_id: int, usuario: str, nombre: str, rol: str, correo: str | None,
                registro: str | None = None, especialidad: str | None = None, id_paciente: int | None = None,
                now: str) -> str:
    """Crea la cuenta ACTIVA con contraseña temporal. Devuelve la contraseña temporal (mostrarla una sola vez)."""
    usuario = (usuario or "").strip().lower()
    if len(usuario) < 3 or not all(c.isalnum() or c in "._-" for c in usuario):
        raise sqlite3.IntegrityError("Usuario: mínimo 3 caracteres (letras, números, punto, guion)")
    if rol not in ROLE_IDS:
        raise sqlite3.IntegrityError("Rol no válido")
    if conn.execute("SELECT 1 FROM usuarios WHERE lower(usuario) = ?", (usuario,)).fetchone():
        raise sqlite3.IntegrityError("Ese usuario ya existe")
    if rol == "PACIENTE" and not conn.execute("SELECT 1 FROM pacientes_clinicos WHERE id_paciente = ?",
                                              (id_paciente,)).fetchone():
        raise sqlite3.IntegrityError("Vincula la cuenta a un paciente registrado")
    pwd = temp_password()
    with conn:
        conn.execute("INSERT INTO usuarios(usuario, hash_password, nombre_mostrado, rol_id, estado_cuenta, "
                     "registro_profesional, especialidad, id_paciente, correo, creado_en, debe_cambiar_clave) "
                     "VALUES (?,?,?,?, 'ACTIVO', ?,?,?,?,?, 1)",
                     (usuario, auth.hash_password(pwd), (nombre or "").strip() or usuario, ROLE_IDS[rol],
                      (registro or "").strip() or None, (especialidad or "").strip().upper() or None,
                      id_paciente if rol == "PACIENTE" else None, (correo or "").strip() or None, now))
        auth._audit(conn, admin_id, "USUARIO_CREADO", f"{usuario} ({rol})", now)
    return pwd


def set_status(conn: sqlite3.Connection, admin_id: int, user_id: int, estado: str, motivo: str, now: str) -> None:
    if estado not in ("ACTIVO", "SUSPENDIDO", "INACTIVO"):
        raise sqlite3.IntegrityError("Estado no válido")
    if user_id == admin_id and estado != "ACTIVO":
        raise sqlite3.IntegrityError("No puedes suspender tu propia cuenta")
    with conn:
        conn.execute("UPDATE usuarios SET estado_cuenta = ?, motivo_estado = ? WHERE id = ?",
                     (estado, (motivo or "").strip() or None, user_id))
        auth._audit(conn, admin_id, "USUARIO_ESTADO", f"usuario {user_id} → {estado}: {motivo}", now)


def reset_password(conn: sqlite3.Connection, admin_id: int, user_id: int, now: str) -> str:
    pwd = temp_password()
    with conn:
        conn.execute("UPDATE usuarios SET hash_password = ?, debe_cambiar_clave = 1, intentos_fallidos = 0, "
                     "bloqueado_hasta = NULL WHERE id = ?", (auth.hash_password(pwd), user_id))
        auth._audit(conn, admin_id, "CLAVE_RESTABLECIDA", f"usuario {user_id} (temporal, cambio obligatorio)", now)
    return pwd


# ---------------------------------------------------------------------------
# Turnos del personal
# ---------------------------------------------------------------------------
def staff_members(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""SELECT u.id, u.nombre_mostrado, r.codigo AS rol, u.especialidad FROM usuarios u
                           JOIN roles r ON r.id = u.rol_id WHERE r.codigo IN ('DOCTOR','ENFERMERIA','FACTURACION')
                           AND u.estado_cuenta = 'ACTIVO' ORDER BY r.id, u.nombre_mostrado""").fetchall()


def assign_shifts(conn: sqlite3.Connection, *, admin_id: int, user_id: int, servicio: str, template: str,
                  days: list[str], now: str, skip_conflicts: bool = False) -> int | tuple[int, list[str]]:
    """Asigna el mismo turno en varios días. Varias personas pueden cubrir la misma área a la vez; lo que no se
    permite es que UNA persona quede en dos turnos que se cruzan. Por defecto es todo o nada; con
    skip_conflicts=True se asignan los días libres y se devuelven (asignados, días omitidos)."""
    if template not in SHIFT_TEMPLATES:
        raise sqlite3.IntegrityError("Tipo de turno no válido")
    hour, length, kind = SHIFT_TEMPLATES[template]
    person = conn.execute("SELECT nombre_mostrado FROM usuarios WHERE id = ?", (user_id,)).fetchone()
    who = person[0] if person else "La persona"
    rows, skipped = [], []
    for d in sorted(set(days)):
        start = datetime.strptime(d, "%Y-%m-%d").replace(hour=hour)
        end = start + timedelta(hours=length)
        s, e = start.strftime(FMT), end.strftime(FMT)
        if e <= now:
            raise sqlite3.IntegrityError(f"El turno del {d} ya pasó")
        clash = conn.execute("SELECT inicio, fin, servicio FROM turnos WHERE usuario_id = ? AND inicio < ? AND fin > ?",
                             (user_id, e, s)).fetchone()
        if clash:
            if skip_conflicts:
                skipped.append(d)
                continue
            raise sqlite3.IntegrityError(
                f"{who} ya tiene turno el {clash[0][8:10]}/{clash[0][5:7]} de {clash[0][11:16]} a {clash[1][11:16]} "
                f"({clash[2]}). Varias personas sí pueden cubrir la misma área; lo que no se permite es que la misma "
                "persona esté en dos turnos a la vez. Marca “Saltar los días que ya tiene turno” para asignar el resto.")
        rows.append((user_id, servicio, kind, s, e))
    with conn:
        conn.executemany("INSERT INTO turnos(usuario_id, servicio, tipo, inicio, fin) VALUES (?,?,?,?,?)", rows)
        if rows:
            auth._audit(conn, admin_id, "TURNOS_ASIGNADOS", f"usuario {user_id}: {len(rows)} × {template} en {servicio}", now)
    return (len(rows), skipped) if skip_conflicts else len(rows)


def on_duty(conn: sqlite3.Connection, now: str) -> list[sqlite3.Row]:
    """Quién está de turno ahora mismo, por servicio y rol."""
    return conn.execute("""
        SELECT t.servicio, r.codigo AS rol, u.nombre_mostrado, t.tipo, t.fin
          FROM turnos t JOIN usuarios u ON u.id = t.usuario_id JOIN roles r ON r.id = u.rol_id
         WHERE ? BETWEEN t.inicio AND t.fin AND u.estado_cuenta = 'ACTIVO'
         ORDER BY t.servicio, r.id, u.nombre_mostrado""", (now,)).fetchall()


def delete_shift(conn: sqlite3.Connection, admin_id: int, shift_id: int, now: str) -> None:
    row = conn.execute("SELECT * FROM turnos WHERE id = ?", (shift_id,)).fetchone()
    if row is None:
        raise sqlite3.IntegrityError("Turno inexistente")
    if row["inicio"] <= now:
        raise sqlite3.IntegrityError("Solo se eliminan turnos que aún no han empezado")
    with conn:
        conn.execute("DELETE FROM turnos WHERE id = ?", (shift_id,))
        auth._audit(conn, admin_id, "TURNO_ELIMINADO", f"usuario {row['usuario_id']} {row['inicio'][:16]}", now)


def shifts(conn: sqlite3.Connection, day_from: str, day_to: str, user_id: int | None = None) -> list[sqlite3.Row]:
    extra, args = ("AND t.usuario_id = ?", [day_from, day_to, user_id]) if user_id else ("", [day_from, day_to])
    return conn.execute(f"""
        SELECT t.id, t.usuario_id, u.nombre_mostrado, r.codigo AS rol, t.servicio, t.tipo, t.inicio, t.fin
          FROM turnos t JOIN usuarios u ON u.id = t.usuario_id JOIN roles r ON r.id = u.rol_id
         WHERE substr(t.inicio, 1, 10) BETWEEN ? AND ? {extra} ORDER BY t.inicio, u.nombre_mostrado""", args).fetchall()


# ---------------------------------------------------------------------------
# Cobertura ahora: pacientes por servicio frente al personal de turno (para reasignar)
# ---------------------------------------------------------------------------
SERVICE_BEDS = {  # servicio del turno -> servicios de cama que atiende
    "Urgencias": ("Urgencias",),
    "Hospitalización": ("Hospitalización",),
    "UCI": ("UCI", "Cuidado Intermedio"),
    "Pediatría": ("Pediatría", "Cuidado Básico Neonatal"),
    "Gineco-obstetricia": ("Gineco-obstetricia", "Sala de partos"),
    "Cirugía": ("Recuperación",),
}


def coverage(conn: sqlite3.Connection, beds, now: str) -> list[dict]:
    """Por servicio con camas: pacientes (camas físicas + expansión en uso), médicos y enfermería de turno y
    pacientes por persona de enfermería. Solo conteos: la app no asume un estándar de dotación."""
    duty = on_duty(conn, now)
    out = []
    for service, bed_services in SERVICE_BEDS.items():
        patients = int(beds[beds["servicio"].isin(bed_services)]["ocupada"].sum())
        docs = [d["nombre_mostrado"] for d in duty if d["servicio"] == service and d["rol"] == "DOCTOR"]
        nurses = [d["nombre_mostrado"] for d in duty if d["servicio"] == service and d["rol"] == "ENFERMERIA"]
        out.append({"servicio": service, "pacientes": patients, "medicos": docs, "enfermeria": nurses,
                    "por_enfermera": round(patients / len(nurses), 1) if nurses else None})
    return out


def reassignment_hint(rows: list[dict]) -> str | None:
    """Sugerencia simple: del servicio con menos pacientes por enfermera al que tiene más (o no tiene nadie)."""
    uncovered = [r for r in rows if r["pacientes"] and not r["enfermeria"]]
    staffed = [r for r in rows if r["enfermeria"]]
    if not staffed:
        return None
    donor = min(staffed, key=lambda r: r["por_enfermera"])
    if uncovered:
        target = max(uncovered, key=lambda r: r["pacientes"])
        if len(donor["enfermeria"]) > 1:
            return (f"{target['servicio']} tiene {target['pacientes']} pacientes y nadie de enfermería de turno: "
                    f"mover a una persona desde {donor['servicio']} ({donor['por_enfermera']} pacientes por persona) "
                    "o asignar un turno.")
        return f"{target['servicio']} tiene {target['pacientes']} pacientes y nadie de enfermería de turno: asigna un turno."
    busiest = max(staffed, key=lambda r: r["por_enfermera"])
    if busiest is donor or busiest["por_enfermera"] < 1.5 * donor["por_enfermera"] or len(donor["enfermeria"]) < 2:
        return None
    return (f"{busiest['servicio']} carga {busiest['por_enfermera']} pacientes por persona de enfermería y "
            f"{donor['servicio']} {donor['por_enfermera']}: considera mover a una persona de {donor['servicio']} "
            f"a {busiest['servicio']}.")
