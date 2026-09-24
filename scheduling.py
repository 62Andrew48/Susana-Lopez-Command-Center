"""
scheduling.py — Agenda de citas y turnos de atención (fila de espera con código). clinico.db, sin Streamlit.

Citas
  * Los cupos salen de los turnos de trabajo del médico: franjas de consulta (8-12 y 14-17) dentro de su turno,
    cada CITA_MIN minutos. Un cupo ocupado o ya pasado no se ofrece.
  * La base impide la doble agenda (índice único médico + hora para citas programadas).
  * El paciente agenda y cancela solo las suyas; admisiones (citas.gestionar) las de cualquiera.

Turnos de atención (lo que el paciente ve en pantalla cuando llega: "C-007")
  * Un código por servicio y día (C consulta, F farmacia, L laboratorio, A admisiones), consecutivo.
  * Se llama primero a quien tiene prioridad (adulto mayor, gestante, discapacidad: Ley 1171 de 2007) y luego
    por orden de llegada. El paciente ve cuántas personas tiene antes.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

FMT = "%Y-%m-%d %H:%M:%S"
CITA_MIN = 20
CONSULT_WINDOWS = ((8, 12), (14, 17))
SERVICES = {"CONSULTA": "C", "FARMACIA": "F", "LABORATORIO": "L", "ADMISIONES": "A"}
SERVICE_LABEL = {"CONSULTA": "Consulta", "FARMACIA": "Farmacia", "LABORATORIO": "Laboratorio",
                 "ADMISIONES": "Admisiones y facturación"}
MOTIVOS = {"PRIMERA_VEZ": "Primera vez", "CONTROL": "Control"}


def _dt(s: str) -> datetime:
    return datetime.strptime(s, FMT)


# ---------------------------------------------------------------------------
# Citas
# ---------------------------------------------------------------------------
def specialties(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT u.especialidad FROM usuarios u JOIN roles r ON r.id = u.rol_id WHERE r.codigo = 'DOCTOR' "
        "AND u.estado_cuenta = 'ACTIVO' AND u.especialidad IS NOT NULL ORDER BY 1")]


def doctors(conn: sqlite3.Connection, especialidad: str | None = None) -> list[sqlite3.Row]:
    extra, args = ("AND u.especialidad = ?", [especialidad]) if especialidad else ("", [])
    return conn.execute(f"SELECT u.id, u.nombre_mostrado, u.especialidad FROM usuarios u JOIN roles r ON r.id = u.rol_id "
                        f"WHERE r.codigo = 'DOCTOR' AND u.estado_cuenta = 'ACTIVO' {extra} ORDER BY u.nombre_mostrado",
                        args).fetchall()


def free_slots(conn: sqlite3.Connection, *, especialidad: str | None = None, medico_id: int | None = None,
               day: str, now: str) -> list[dict]:
    """Cupos libres de un día: [{medico_id, medico, fecha_hora}]."""
    docs = [d for d in doctors(conn, especialidad) if medico_id in (None, d["id"])]
    out = []
    t_now = _dt(now)
    for d in docs:
        shifts = conn.execute("SELECT inicio, fin FROM turnos WHERE usuario_id = ? AND tipo = 'TURNO' "
                              "AND substr(inicio, 1, 10) = ?", (d["id"], day)).fetchall()
        taken = {r[0] for r in conn.execute("SELECT fecha_hora FROM citas WHERE medico_id = ? AND estado = 'PROGRAMADA' "
                                            "AND substr(fecha_hora, 1, 10) = ?", (d["id"], day))}
        for s in shifts:
            start, end = _dt(s["inicio"]), _dt(s["fin"])
            for h0, h1 in CONSULT_WINDOWS:
                slot = _dt(f"{day} {h0:02d}:00:00")
                stop = _dt(f"{day} {h1:02d}:00:00")
                while slot + timedelta(minutes=CITA_MIN) <= stop:
                    ts = slot.strftime(FMT)
                    if start <= slot and slot + timedelta(minutes=CITA_MIN) <= end and slot > t_now and ts not in taken:
                        out.append({"medico_id": d["id"], "medico": d["nombre_mostrado"], "fecha_hora": ts})
                    slot += timedelta(minutes=CITA_MIN)
    return sorted(out, key=lambda x: (x["fecha_hora"], x["medico"]))


def book(conn: sqlite3.Connection, *, id_paciente: int, medico_id: int, fecha_hora: str, motivo: str,
         creada_por: int, now: str, nota: str | None = None) -> int:
    if motivo not in MOTIVOS:
        raise sqlite3.IntegrityError("Motivo de cita no válido")
    if fecha_hora <= now:
        raise sqlite3.IntegrityError("No se puede agendar en una hora que ya pasó")
    doc = conn.execute("SELECT especialidad FROM usuarios WHERE id = ?", (medico_id,)).fetchone()
    if doc is None:
        raise sqlite3.IntegrityError("Médico inexistente")
    slot_ok = any(s["fecha_hora"] == fecha_hora for s in
                  free_slots(conn, medico_id=medico_id, day=fecha_hora[:10], now=now))
    if not slot_ok:
        raise sqlite3.IntegrityError("Ese cupo ya no está disponible; elige otro")
    if conn.execute("SELECT 1 FROM citas WHERE id_paciente = ? AND estado = 'PROGRAMADA' AND fecha_hora = ?",
                    (id_paciente, fecha_hora)).fetchone():
        raise sqlite3.IntegrityError("El paciente ya tiene otra cita a esa hora")
    with conn:
        cid = conn.execute("INSERT INTO citas(id_paciente, medico_id, especialidad, fecha_hora, motivo, creada_en, nota, "
                           "creada_por) VALUES (?,?,?,?,?,?,?,?)",
                           (id_paciente, medico_id, doc["especialidad"] or "MEDICINA GENERAL", fecha_hora, motivo, now,
                            (nota or "").strip() or None, creada_por)).lastrowid
        _hc_event(conn, id_paciente, f"Cita agendada: {MOTIVOS[motivo].lower()} con "
                  f"{_name(conn, medico_id)} el {fecha_hora[:16]}", creada_por, now)
    return cid


def cancel(conn: sqlite3.Connection, cita_id: int, by: int, motivo: str, now: str,
           only_patient: int | None = None) -> None:
    row = conn.execute("SELECT * FROM citas WHERE id = ?", (cita_id,)).fetchone()
    if row is None or (only_patient is not None and row["id_paciente"] != only_patient):
        raise sqlite3.IntegrityError("Cita inexistente")
    if row["estado"] != "PROGRAMADA":
        raise sqlite3.IntegrityError("Solo se cancelan citas programadas")
    if not (motivo or "").strip():
        raise sqlite3.IntegrityError("Indica el motivo de la cancelación")
    with conn:
        conn.execute("UPDATE citas SET estado = 'CANCELADA', motivo_cancelacion = ? WHERE id = ?", (motivo.strip(), cita_id))
        _hc_event(conn, row["id_paciente"], f"Cita del {row['fecha_hora'][:16]} cancelada: {motivo.strip()}", by, now)


def set_outcome(conn: sqlite3.Connection, cita_id: int, medico_id: int, attended: bool, now: str) -> None:
    """El médico marca la cita como cumplida o como inasistencia (solo las suyas)."""
    row = conn.execute("SELECT * FROM citas WHERE id = ? AND estado = 'PROGRAMADA'", (cita_id,)).fetchone()
    if row is None:
        raise sqlite3.IntegrityError("La cita no está programada")
    if row["medico_id"] not in (None, medico_id):
        raise sqlite3.IntegrityError("La cita es de otro médico")
    with conn:
        conn.execute("UPDATE citas SET estado = ?, medico_id = COALESCE(medico_id, ?) WHERE id = ?",
                     ("CUMPLIDA" if attended else "NO_ASISTIO", medico_id, cita_id))
        conn.execute("UPDATE turnos_atencion SET estado = ?, atendido_en = ?, atendido_por = ? WHERE cita_id = ? "
                     "AND estado IN ('EN_ESPERA','LLAMADO','EN_ATENCION')",
                     ("ATENDIDO" if attended else "NO_SE_PRESENTO", now, medico_id, cita_id))
        _hc_event(conn, row["id_paciente"], ("Cita cumplida" if attended else "No asistió a la cita") +
                  f" del {row['fecha_hora'][:16]}", medico_id, now)


def appointments(conn: sqlite3.Connection, *, day_from: str, day_to: str, medico_id: int | None = None,
                 id_paciente: int | None = None, states: tuple[str, ...] = ("PROGRAMADA", "CUMPLIDA", "NO_ASISTIO",
                                                                             "CANCELADA")) -> list[sqlite3.Row]:
    where = ["substr(c.fecha_hora, 1, 10) BETWEEN ? AND ?", f"c.estado IN ({','.join('?' * len(states))})"]
    args: list = [day_from, day_to, *states]
    if medico_id:
        where.append("c.medico_id = ?"); args.append(medico_id)
    if id_paciente:
        where.append("c.id_paciente = ?"); args.append(id_paciente)
    return conn.execute(f"""
        SELECT c.*, COALESCE(u.nombre_mostrado, 'Por asignar') AS medico,
               COALESCE(NULLIF(trim(pc.nombres || ' ' || pc.apellidos), ''), 'Paciente ' || c.id_paciente) AS paciente,
               pc.tipo_documento, pc.numero_documento,
               (SELECT t.codigo FROM turnos_atencion t WHERE t.cita_id = c.id ORDER BY t.id DESC LIMIT 1) AS turno,
               (SELECT t.estado FROM turnos_atencion t WHERE t.cita_id = c.id ORDER BY t.id DESC LIMIT 1) AS turno_estado
          FROM citas c LEFT JOIN usuarios u ON u.id = c.medico_id
          LEFT JOIN pacientes_clinicos pc ON pc.id_paciente = c.id_paciente
         WHERE {' AND '.join(where)} ORDER BY c.fecha_hora""", args).fetchall()


def _name(conn, uid) -> str:
    row = conn.execute("SELECT nombre_mostrado FROM usuarios WHERE id = ?", (uid,)).fetchone()
    return row[0] if row else "—"


def _hc_event(conn, id_paciente: int, text: str, by: int | None, now: str) -> None:
    hc = conn.execute("SELECT id FROM historias_clinicas WHERE id_paciente = ?", (id_paciente,)).fetchone()
    if hc:
        conn.execute("INSERT INTO historia_clinica_eventos(historia_id, tipo, descripcion, autor_id, fecha) "
                     "VALUES (?, 'CITA', ?, ?, ?)", (hc[0], text, by, now))


# ---------------------------------------------------------------------------
# Turnos de atención (fila de espera)
# ---------------------------------------------------------------------------
def issue_ticket(conn: sqlite3.Connection, *, servicio: str, id_paciente: int, by: int | None, now: str,
                 prioridad: bool = False, cita_id: int | None = None) -> str:
    if servicio not in SERVICES:
        raise sqlite3.IntegrityError("Servicio no válido")
    day = now[:10]
    active = conn.execute("SELECT codigo FROM turnos_atencion WHERE fecha = ? AND servicio = ? AND id_paciente = ? "
                          "AND estado IN ('EN_ESPERA','LLAMADO','EN_ATENCION')", (day, servicio, id_paciente)).fetchone()
    if active:
        raise sqlite3.IntegrityError(f"El paciente ya tiene el turno {active[0]} en {SERVICE_LABEL[servicio].lower()}")
    with conn:
        n = (conn.execute("SELECT MAX(numero) FROM turnos_atencion WHERE fecha = ? AND servicio = ?",
                          (day, servicio)).fetchone()[0] or 0) + 1
        code = f"{SERVICES[servicio]}-{n:03d}"
        conn.execute("INSERT INTO turnos_atencion(fecha, servicio, numero, codigo, id_paciente, cita_id, prioridad, "
                     "creado_por, creado_en) VALUES (?,?,?,?,?,?,?,?,?)",
                     (day, servicio, n, code, id_paciente, cita_id, int(prioridad), by, now))
    return code


def check_in(conn: sqlite3.Connection, cita_id: int, by: int | None, now: str, only_patient: int | None = None,
             prioridad: bool = False) -> str:
    """El paciente llega a su cita de hoy: recibe su turno de consulta."""
    c = conn.execute("SELECT * FROM citas WHERE id = ? AND estado = 'PROGRAMADA'", (cita_id,)).fetchone()
    if c is None or (only_patient is not None and c["id_paciente"] != only_patient):
        raise sqlite3.IntegrityError("Cita inexistente")
    if c["fecha_hora"][:10] != now[:10]:
        raise sqlite3.IntegrityError("Solo se registra la llegada el día de la cita")
    return issue_ticket(conn, servicio="CONSULTA", id_paciente=c["id_paciente"], by=by, now=now, cita_id=cita_id,
                        prioridad=prioridad)


def _queue_order() -> str:
    return "ORDER BY t.prioridad DESC, t.numero"


def call_next(conn: sqlite3.Connection, *, servicio: str, modulo: str, by: int, now: str,
              medico_id: int | None = None) -> sqlite3.Row | None:
    """Llama al siguiente (prioridad primero). En consulta, el médico llama solo a quienes tienen cita con él
    o turno sin cita."""
    extra, args = "", [now[:10], servicio]
    if medico_id and servicio == "CONSULTA":
        extra = "AND (t.cita_id IS NULL OR (SELECT medico_id FROM citas WHERE id = t.cita_id) = ?)"
        args.append(medico_id)
    row = conn.execute(f"SELECT t.* FROM turnos_atencion t WHERE t.fecha = ? AND t.servicio = ? AND t.estado = 'EN_ESPERA' "
                       f"{extra} {_queue_order()} LIMIT 1", args).fetchone()
    if row is None:
        return None
    with conn:
        conn.execute("UPDATE turnos_atencion SET estado = 'LLAMADO', llamado_en = ?, modulo = ?, atendido_por = ? "
                     "WHERE id = ?", (now, modulo, by, row["id"]))
    return conn.execute("SELECT * FROM turnos_atencion WHERE id = ?", (row["id"],)).fetchone()


def set_ticket_state(conn: sqlite3.Connection, ticket_id: int, state: str, by: int, now: str) -> None:
    if state not in ("EN_ATENCION", "ATENDIDO", "NO_SE_PRESENTO", "EN_ESPERA"):
        raise sqlite3.IntegrityError("Estado no válido")
    with conn:
        conn.execute("UPDATE turnos_atencion SET estado = ?, atendido_por = ?, "
                     "atendido_en = CASE WHEN ? IN ('ATENDIDO','NO_SE_PRESENTO') THEN ? ELSE atendido_en END "
                     "WHERE id = ?", (state, by, state, now, ticket_id))


def board(conn: sqlite3.Connection, day: str, servicio: str | None = None) -> list[sqlite3.Row]:
    extra, args = ("AND t.servicio = ?", [day, servicio]) if servicio else ("", [day])
    return conn.execute(f"""
        SELECT t.*, COALESCE(NULLIF(trim(pc.nombres || ' ' || pc.apellidos), ''), 'Paciente ' || t.id_paciente) AS paciente,
               c.fecha_hora AS cita_hora, u.nombre_mostrado AS medico
          FROM turnos_atencion t LEFT JOIN pacientes_clinicos pc ON pc.id_paciente = t.id_paciente
          LEFT JOIN citas c ON c.id = t.cita_id LEFT JOIN usuarios u ON u.id = c.medico_id
         WHERE t.fecha = ? {extra} ORDER BY t.estado <> 'LLAMADO', t.prioridad DESC, t.numero""", args).fetchall()


def patient_tickets(conn: sqlite3.Connection, id_paciente: int, now: str) -> list[dict]:
    """Turnos activos del paciente hoy, con cuántas personas tiene antes."""
    out = []
    for t in conn.execute("SELECT * FROM turnos_atencion WHERE id_paciente = ? AND fecha = ? AND estado IN "
                          "('EN_ESPERA','LLAMADO','EN_ATENCION') ORDER BY id", (id_paciente, now[:10])):
        ahead = conn.execute("""SELECT COUNT(*) FROM turnos_atencion WHERE fecha = ? AND servicio = ? AND estado = 'EN_ESPERA'
                                AND (prioridad > ? OR (prioridad = ? AND numero < ?))""",
                             (t["fecha"], t["servicio"], t["prioridad"], t["prioridad"], t["numero"])).fetchone()[0]
        out.append({**dict(t), "antes": ahead if t["estado"] == "EN_ESPERA" else 0})
    return out
