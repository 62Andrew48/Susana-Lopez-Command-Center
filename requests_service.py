"""
requests_service.py — Solicitudes que llegan desde afuera (lógica pura, sin Streamlit).

Solicitudes de cita (paciente → facturación)
  * El paciente NO elige médico ni hora: cuenta qué le pasa (síntomas), qué tipo de atención cree necesitar y en
    qué jornada puede ir. Lo hace desde el portal o escribiéndole al asistente.
  * Facturación ve la fila de solicitudes, agenda la cita con los cupos reales de los médicos y le escribe al
    paciente por WhatsApp (enlace wa.me con el mensaje ya armado) o cierra la solicitud con una respuesta.
  * Una sola solicitud pendiente por paciente. Nada se borra.
  * Si lo que cuenta suena a urgencia (dolor en el pecho, falta de aire…), se le dice que vaya a urgencias o
    llame al 123 en vez de esperar una cita.

Solicitudes de registro (persona nueva → gerencia)
  * Quien no está registrado en el hospital deja sus datos. Gerencia lo cita (día, hora y lugar) para que vaya en
    persona con su documento y allí admisiones lo registra y le crea la cuenta; o la rechaza con un motivo.
  * La persona consulta la respuesta con su documento y su correo (sin cuenta todavía).
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from urllib.parse import quote

import scheduling as sch

TYPES = {"MEDICINA_GENERAL": "Medicina general", "ESPECIALISTA": "Consulta con especialista",
         "CONTROL": "Control", "RESULTADOS": "Revisión de resultados", "OTRO": "Otro"}
PREFERENCES = {"MANANA": "En la mañana", "TARDE": "En la tarde", "CUALQUIERA": "Cualquier hora"}
STATE = {"PENDIENTE": "Pendiente", "AGENDADA": "Agendada", "CERRADA": "Respondida", "CANCELADA": "Cancelada"}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$")
RED_FLAGS = ("dolor en el pecho", "dolor de pecho", "no puedo respirar", "dificultad para respirar", "falta de aire",
             "me ahogo", "desmay", "convuls", "sangrado abundante", "vomito con sangre", "perdi el conocimiento",
             "no puedo mover", "cara torcida", "quemadura grave", "intoxic", "me quiero morir", "suicid")
EMERGENCY_TEXT = ("Lo que describes puede ser una urgencia: no esperes una cita. Ve a urgencias del hospital o "
                  "llama a la línea de emergencias 123.")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def is_emergency(text: str) -> bool:
    t = _norm(text)
    return any(flag in t for flag in RED_FLAGS)


def whatsapp_link(phone: str | None, text: str) -> str | None:
    """Enlace wa.me. Celulares colombianos de 10 dígitos (3xx) se completan con el indicativo 57."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10 and digits.startswith("3"):
        digits = "57" + digits
    if len(digits) < 11:
        return None
    return f"https://wa.me/{digits}?text={quote(text)}"


# ---------------------------------------------------------------------------
# Solicitudes de cita
# ---------------------------------------------------------------------------
def create_request(conn: sqlite3.Connection, *, id_paciente: int, tipo: str, sintomas: str, preferencia: str,
                   telefono: str | None, canal: str, now: str) -> int:
    if tipo not in TYPES:
        raise sqlite3.IntegrityError("Elige el tipo de atención")
    if preferencia not in PREFERENCES:
        raise sqlite3.IntegrityError("Elige la jornada")
    text = " ".join((sintomas or "").split())
    if len(text) < 10:
        raise sqlite3.IntegrityError("Cuéntanos qué te pasa (mínimo 10 caracteres)")
    if conn.execute("SELECT 1 FROM solicitudes_cita WHERE id_paciente = ? AND estado = 'PENDIENTE'",
                    (id_paciente,)).fetchone():
        raise sqlite3.IntegrityError("Ya tienes una solicitud pendiente: facturación te contactará pronto")
    phone = re.sub(r"[^\d+ ]", "", telefono or "").strip() or None
    with conn:
        rid = conn.execute("INSERT INTO solicitudes_cita(id_paciente, tipo, sintomas, preferencia, telefono, canal, "
                           "creada_en) VALUES (?,?,?,?,?,?,?)",
                           (id_paciente, tipo, text[:600], preferencia, phone, canal, now)).lastrowid
        sch._hc_event(conn, id_paciente, f"Solicitud de cita ({TYPES[tipo].lower()}) enviada a facturación", None, now)
    return rid


def pending_requests(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT s.*, COALESCE(NULLIF(trim(pc.nombres || ' ' || pc.apellidos), ''), 'Paciente ' || s.id_paciente)
                    AS paciente, pc.nombres, pc.tipo_documento, pc.numero_documento, pc.edad,
               COALESCE(s.telefono, pc.telefono) AS contacto
          FROM solicitudes_cita s LEFT JOIN pacientes_clinicos pc ON pc.id_paciente = s.id_paciente
         WHERE s.estado = 'PENDIENTE' ORDER BY s.creada_en""").fetchall()


def patient_requests(conn: sqlite3.Connection, id_paciente: int) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT s.*, c.fecha_hora, c.especialidad, COALESCE(u.nombre_mostrado, '') AS medico
          FROM solicitudes_cita s LEFT JOIN citas c ON c.id = s.cita_id LEFT JOIN usuarios u ON u.id = c.medico_id
         WHERE s.id_paciente = ? ORDER BY s.id DESC""", (id_paciente,)).fetchall()


def _pending(conn, request_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM solicitudes_cita WHERE id = ?", (request_id,)).fetchone()
    if row is None or row["estado"] != "PENDIENTE":
        raise sqlite3.IntegrityError("La solicitud ya fue atendida")
    return row


def schedule_request(conn: sqlite3.Connection, request_id: int, *, medico_id: int, fecha_hora: str, motivo: str,
                     by: int, now: str) -> int:
    """Facturación agenda la cita pedida (mismas reglas de scheduling.book) y la enlaza a la solicitud."""
    row = _pending(conn, request_id)
    cita = sch.book(conn, id_paciente=row["id_paciente"], medico_id=medico_id, fecha_hora=fecha_hora, motivo=motivo,
                    creada_por=by, now=now, nota=row["sintomas"][:200])
    with conn:
        conn.execute("UPDATE solicitudes_cita SET estado = 'AGENDADA', cita_id = ?, atendida_por = ?, atendida_en = ? "
                     "WHERE id = ?", (cita, by, now, request_id))
    return cita


def close_request(conn: sqlite3.Connection, request_id: int, *, respuesta: str, by: int, now: str) -> None:
    """Responder sin agendar (p. ej. «acérquese a consulta externa el lunes a las 7:00»)."""
    _pending(conn, request_id)
    text = (respuesta or "").strip()
    if len(text) < 10:
        raise sqlite3.IntegrityError("Escribe la respuesta para el paciente (mínimo 10 caracteres)")
    with conn:
        conn.execute("UPDATE solicitudes_cita SET estado = 'CERRADA', respuesta = ?, atendida_por = ?, atendida_en = ? "
                     "WHERE id = ?", (text, by, now, request_id))


def mark_contacted(conn: sqlite3.Connection, request_id: int, now: str) -> None:
    with conn:
        conn.execute("UPDATE solicitudes_cita SET contactado_en = COALESCE(contactado_en, ?) WHERE id = ?",
                     (now, request_id))


def cancel_request(conn: sqlite3.Connection, request_id: int, id_paciente: int, now: str) -> None:
    row = _pending(conn, request_id)
    if row["id_paciente"] != id_paciente:
        raise sqlite3.IntegrityError("Solicitud inexistente")
    with conn:
        conn.execute("UPDATE solicitudes_cita SET estado = 'CANCELADA', atendida_en = ? WHERE id = ?", (now, request_id))


def suggested_specialty(row, specialties: list[str]) -> str:
    """Punto de partida para facturación: menores de 18 → pediatría; lo demás → medicina general."""
    if row["edad"] is not None and row["edad"] < 18 and "PEDIATRIA" in specialties:
        return "PEDIATRIA"
    if row["tipo"] in ("MEDICINA_GENERAL", "OTRO", "CONTROL", "RESULTADOS") and "MEDICINA GENERAL" in specialties:
        return "MEDICINA GENERAL"
    return specialties[0]


# ---------------------------------------------------------------------------
# Mensajes paciente ↔ facturación (dentro de la solicitud)
# ---------------------------------------------------------------------------
def send_message(conn: sqlite3.Connection, request_id: int, *, lado: str, autor_id: int | None, texto: str, now: str,
                 id_paciente: int | None = None) -> int:
    """El paciente (solo en SUS solicitudes) o facturación escriben en la conversación de una solicitud abierta."""
    row = conn.execute("SELECT * FROM solicitudes_cita WHERE id = ?", (request_id,)).fetchone()
    if row is None or (lado == "PACIENTE" and row["id_paciente"] != id_paciente):
        raise sqlite3.IntegrityError("Solicitud inexistente")
    if lado not in ("PACIENTE", "FACTURACION"):
        raise sqlite3.IntegrityError("Remitente no válido")
    if row["estado"] == "CANCELADA":
        raise sqlite3.IntegrityError("La solicitud fue retirada; envía una nueva")
    text = " ".join((texto or "").split())
    if len(text) < 2:
        raise sqlite3.IntegrityError("Escribe el mensaje")
    with conn:
        mid = conn.execute("INSERT INTO solicitudes_cita_mensajes(solicitud_id, autor_id, lado, texto, fecha) "
                           "VALUES (?,?,?,?,?)", (request_id, autor_id, lado, text[:800], now)).lastrowid
        if lado == "FACTURACION" and row["contactado_en"] is None:
            conn.execute("UPDATE solicitudes_cita SET contactado_en = ? WHERE id = ?", (now, request_id))
    return mid


def messages(conn: sqlite3.Connection, request_id: int) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT m.*, COALESCE(u.nombre_mostrado, '') AS autor FROM solicitudes_cita_mensajes m
          LEFT JOIN usuarios u ON u.id = m.autor_id WHERE m.solicitud_id = ? ORDER BY m.id""", (request_id,)).fetchall()


def mark_read(conn: sqlite3.Connection, request_id: int, reader: str) -> None:
    """Marca como leídos los mensajes del OTRO lado (reader = quien lee)."""
    other = "FACTURACION" if reader == "PACIENTE" else "PACIENTE"
    with conn:
        conn.execute("UPDATE solicitudes_cita_mensajes SET leido = 1 WHERE solicitud_id = ? AND lado = ? AND leido = 0",
                     (request_id, other))


def unread(conn: sqlite3.Connection, lado: str, id_paciente: int | None = None) -> int:
    """Mensajes sin leer que le llegan a `lado` (facturación: de todos los pacientes; paciente: los suyos)."""
    other = "FACTURACION" if lado == "PACIENTE" else "PACIENTE"
    extra, args = ("AND s.id_paciente = ?", [id_paciente]) if id_paciente else ("", [])
    return conn.execute(f"SELECT COUNT(*) FROM solicitudes_cita_mensajes m JOIN solicitudes_cita s "
                        f"ON s.id = m.solicitud_id WHERE m.lado = ? AND m.leido = 0 {extra}", [other, *args]).fetchone()[0]


def whatsapp_message(row, cita: sqlite3.Row | None = None) -> str:
    """Mensaje que facturación envía por WhatsApp."""
    name = (row["nombres"] or "").split()[0] if row["nombres"] else ""
    minor = "edad" in row.keys() and row["edad"] is not None and row["edad"] < 18
    if minor:  # a un menor se le escribe a su acudiente
        full = " ".join((row["nombres"] or "").split()[:2])
        hello = f"Hola, le escribimos del Hospital Susana López de Valencia sobre {full}"
    else:
        hello = f"Hola{(' ' + name) if name else ''}, le escribimos del Hospital Susana López de Valencia"
    if cita is not None:
        return (f"{hello}. {'La' if minor else 'Su'} cita de {str(cita['especialidad']).title()} quedó para el "
                f"{cita['fecha_hora'][8:10]}/{cita['fecha_hora'][5:7]} a las {cita['fecha_hora'][11:16]} con "
                f"{cita['medico']}. Lleguen 20 minutos antes con el documento del paciente. También la ven en el portal.")
    return (f"{hello}, por la solicitud de cita ({TYPES[row['tipo']].lower()}). ¿Nos confirma en qué horario "
            f"puede asistir?")


# ---------------------------------------------------------------------------
# Solicitudes de registro
# ---------------------------------------------------------------------------
def create_registration(conn: sqlite3.Connection, *, nombres: str, apellidos: str, tipo_documento: str,
                        numero_documento: str, correo: str, telefono: str | None, now: str) -> int:
    doc = "".join((numero_documento or "").split())
    mail = (correo or "").strip().lower()
    if len((nombres or "").strip()) < 2 or len((apellidos or "").strip()) < 2:
        raise sqlite3.IntegrityError("Escribe tus nombres y apellidos")
    if not doc.isalnum() or len(doc) < 5:
        raise sqlite3.IntegrityError("Número de documento no válido")
    if not EMAIL.match(mail):
        raise sqlite3.IntegrityError("El correo no es válido")
    if conn.execute("SELECT 1 FROM solicitudes_registro WHERE numero_documento = ? AND estado = 'PENDIENTE'",
                    (doc,)).fetchone():
        raise sqlite3.IntegrityError("Ya hay una solicitud pendiente con ese documento. Consulta su estado abajo.")
    with conn:
        return conn.execute("INSERT INTO solicitudes_registro(nombres, apellidos, tipo_documento, numero_documento, "
                            "correo, telefono, creada_en) VALUES (?,?,?,?,?,?,?)",
                            (" ".join(nombres.split()), " ".join(apellidos.split()), tipo_documento, doc, mail,
                             re.sub(r"[^\d+ ]", "", telefono or "").strip() or None, now)).lastrowid


def pending_registrations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT r.*, (SELECT COUNT(*) FROM pacientes_clinicos p WHERE p.numero_documento = r.numero_documento)
                    AS ya_existe
          FROM solicitudes_registro r WHERE r.estado = 'PENDIENTE' ORDER BY r.creada_en""").fetchall()


def cite_registration(conn: sqlite3.Connection, reg_id: int, *, fecha_hora: str, lugar: str, nota: str, by: int,
                      now: str) -> None:
    """Gerencia cita a la persona para registrarla en persona (día, hora y lugar)."""
    row = conn.execute("SELECT * FROM solicitudes_registro WHERE id = ?", (reg_id,)).fetchone()
    if row is None or row["estado"] != "PENDIENTE":
        raise sqlite3.IntegrityError("La solicitud ya fue atendida")
    if fecha_hora <= now:
        raise sqlite3.IntegrityError("La cita debe ser en una fecha y hora futuras")
    if len((lugar or "").strip()) < 3:
        raise sqlite3.IntegrityError("Indica el lugar")
    with conn:
        conn.execute("UPDATE solicitudes_registro SET estado = 'CITADA', cita_fecha_hora = ?, cita_lugar = ?, "
                     "respuesta = ?, atendida_por = ?, atendida_en = ? WHERE id = ?",
                     (fecha_hora, lugar.strip(), (nota or "").strip() or None, by, now, reg_id))


def reject_registration(conn: sqlite3.Connection, reg_id: int, *, motivo: str, by: int, now: str) -> None:
    if len((motivo or "").strip()) < 10:
        raise sqlite3.IntegrityError("Explica el motivo (mínimo 10 caracteres)")
    with conn:
        cur = conn.execute("UPDATE solicitudes_registro SET estado = 'RECHAZADA', respuesta = ?, atendida_por = ?, "
                           "atendida_en = ? WHERE id = ? AND estado = 'PENDIENTE'", (motivo.strip(), by, now, reg_id))
    if not cur.rowcount:
        raise sqlite3.IntegrityError("La solicitud ya fue atendida")


def registration_status(conn: sqlite3.Connection, numero_documento: str, correo: str) -> sqlite3.Row | None:
    """La persona consulta su solicitud con documento + correo (los dos deben coincidir)."""
    return conn.execute("SELECT * FROM solicitudes_registro WHERE numero_documento = ? AND correo = ? "
                        "ORDER BY id DESC LIMIT 1",
                        ("".join((numero_documento or "").split()), (correo or "").strip().lower())).fetchone()


def registration_message(row) -> str:
    when = row["cita_fecha_hora"]
    return (f"Hola {row['nombres'].split()[0]}, le escribimos del Hospital Susana López de Valencia por su solicitud "
            f"de registro. Lo esperamos el {when[8:10]}/{when[5:7]} a las {when[11:16]} en {row['cita_lugar']} con su "
            "documento de identidad original para crear su registro y su cuenta en el portal."
            + (f" {row['respuesta']}" if row["respuesta"] else ""))
