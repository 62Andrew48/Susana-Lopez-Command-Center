"""
beds_service.py — Ocupar y liberar camas desde la app (clinico.db), encima del censo del extracto.

El censo del día de corte (hospital.db, solo lectura) dice qué camas estaban ocupadas. Lo que el personal hace
después (asignar una cama con su estancia estimada, dar de alta o trasladar) queda como movimientos en
camas_asignaciones: nunca se editan ni se borran. El estado actual de una cama = su último movimiento; si no
tiene movimientos, manda el censo.
No depende de Streamlit.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pandas as pd

FMT = "%Y-%m-%d %H:%M:%S"
RELEASE_REASONS = ("Alta médica", "Traslado a otra unidad", "Remisión a otra institución", "Alta voluntaria",
                   "Fallecimiento", "Corrección de registro")


def overrides(conn: sqlite3.Connection) -> pd.DataFrame:
    """Último movimiento de cada cama tocada en la app."""
    return pd.read_sql_query("""
        SELECT a.codigo_cama, a.accion, a.id_paciente, a.dias_estimados, a.fecha, a.motivo,
               COALESCE(NULLIF(trim(pc.nombres || ' ' || pc.apellidos), ''), 'Paciente ' || a.id_paciente) AS paciente
          FROM camas_asignaciones a LEFT JOIN pacientes_clinicos pc ON pc.id_paciente = a.id_paciente
         WHERE a.id = (SELECT MAX(b.id) FROM camas_asignaciones b WHERE b.codigo_cama = a.codigo_cama)""", conn)


def apply(beds: pd.DataFrame, conn: sqlite3.Connection, now: str) -> pd.DataFrame:
    """Aplica los movimientos de la app sobre el mapa del censo (copia; no toca el caché)."""
    ov = overrides(conn)
    out = beds.copy()
    out["paciente_app"] = None
    out["salida_estimada"] = None
    if ov.empty:
        return out
    t = datetime.strptime(now, FMT)
    for r in ov.itertuples():
        mask = out["codigo_cama"] == r.codigo_cama
        if not mask.any():
            continue
        if r.accion == "OCUPAR":
            since = datetime.strptime(r.fecha, FMT)
            out.loc[mask, ["ocupada", "pacientes"]] = [1, 1]
            out.loc[mask, "dias_estancia"] = round((t - since).total_seconds() / 86400, 1)
            out.loc[mask, "paciente_app"] = r.paciente
            out.loc[mask, "salida_estimada"] = (since + timedelta(days=int(r.dias_estimados))).strftime("%d/%m/%Y")
        else:
            out.loc[mask, ["ocupada", "pacientes"]] = [0, 0]
            out.loc[mask, "dias_estancia"] = 0.0
    return out


SURGICAL_DAYS_BACK = 7   # coordinación de quirófanos: pacientes operados en la última semana o ya programados


def surgical_patients(conn: sqlite3.Connection, now: str) -> list[sqlite3.Row]:
    """Pacientes que le corresponden a coordinación de quirófanos: cirugía programada o realizada hace ≤ 7 días."""
    since = (datetime.strptime(now[:10], "%Y-%m-%d") - timedelta(days=SURGICAL_DAYS_BACK)).strftime("%Y-%m-%d")
    return conn.execute("""
        SELECT s.id_paciente, MIN(s.fecha_programada) AS fecha, MIN(s.hora_programada) AS hora,
               COALESCE(NULLIF(trim(pc.nombres || ' ' || pc.apellidos), ''), 'Paciente ' || s.id_paciente) AS paciente,
               group_concat(DISTINCT s.estado) AS estados
          FROM cirugias_solicitudes s LEFT JOIN pacientes_clinicos pc ON pc.id_paciente = s.id_paciente
         WHERE s.estado = 'PROGRAMADA' OR (s.estado = 'REALIZADA' AND COALESCE(s.fecha_programada, '') >= ?)
         GROUP BY s.id_paciente ORDER BY fecha, hora""", (since,)).fetchall()


def occupy(conn: sqlite3.Connection, *, codigo_cama: str, id_paciente: int, dias_estimados: int, usuario_id: int,
           cama_ocupada: bool, now: str, surgical_only: bool = False) -> None:
    """Asigna la cama. `cama_ocupada` es el estado actual que ve la pantalla (censo + movimientos).
    `surgical_only`: quien solo tiene camas.quirurgicas (coordinación de quirófanos) únicamente asigna camas a
    pacientes con cirugía programada o realizada en la última semana."""
    if surgical_only and id_paciente not in {r["id_paciente"] for r in surgical_patients(conn, now)}:
        raise sqlite3.IntegrityError("Solo puedes asignar camas a pacientes con cirugía programada o realizada "
                                     f"en los últimos {SURGICAL_DAYS_BACK} días")
    if cama_ocupada:
        raise sqlite3.IntegrityError("La cama ya está ocupada; libérala primero")
    held = conn.execute("""
        SELECT a.codigo_cama FROM camas_asignaciones a WHERE a.id_paciente = ? AND a.accion = 'OCUPAR'
           AND a.id = (SELECT MAX(b.id) FROM camas_asignaciones b WHERE b.codigo_cama = a.codigo_cama)""",
                        (id_paciente,)).fetchone()
    if held:
        raise sqlite3.IntegrityError(f"El paciente ya tiene asignada la cama {held[0]}: libérala antes de trasladarlo")
    if not 1 <= int(dias_estimados) <= 90:
        raise sqlite3.IntegrityError("La estancia estimada debe estar entre 1 y 90 días")
    with conn:
        conn.execute("INSERT INTO camas_asignaciones(codigo_cama, accion, id_paciente, dias_estimados, usuario_id, "
                     "fecha) VALUES (?, 'OCUPAR', ?, ?, ?, ?)", (codigo_cama, id_paciente, int(dias_estimados),
                                                                   usuario_id, now))


def release(conn: sqlite3.Connection, *, codigo_cama: str, motivo: str, usuario_id: int, cama_ocupada: bool,
            now: str) -> None:
    if not cama_ocupada:
        raise sqlite3.IntegrityError("La cama ya está libre")
    if not (motivo or "").strip():
        raise sqlite3.IntegrityError("Indica el motivo (alta, traslado…)")
    last = conn.execute("SELECT accion, id_paciente FROM camas_asignaciones WHERE codigo_cama = ? "
                        "ORDER BY id DESC LIMIT 1", (codigo_cama,)).fetchone()
    patient = last[1] if last and last[0] == "OCUPAR" else None
    with conn:
        conn.execute("INSERT INTO camas_asignaciones(codigo_cama, accion, id_paciente, motivo, usuario_id, fecha) "
                     "VALUES (?, 'LIBERAR', ?, ?, ?, ?)", (codigo_cama, patient, motivo.strip(), usuario_id, now))


def history(conn: sqlite3.Connection, limit: int = 100) -> pd.DataFrame:
    return pd.read_sql_query("""
        SELECT a.fecha, a.codigo_cama, a.accion, a.dias_estimados, a.motivo, u.nombre_mostrado AS usuario,
               COALESCE(NULLIF(trim(pc.nombres || ' ' || pc.apellidos), ''), 'Paciente ' || a.id_paciente) AS paciente
          FROM camas_asignaciones a JOIN usuarios u ON u.id = a.usuario_id
          LEFT JOIN pacientes_clinicos pc ON pc.id_paciente = a.id_paciente
         ORDER BY a.id DESC LIMIT ?""", conn, params=(limit,))
