"""
clinical_records.py — Pacientes, registros de historia clínica y adjuntos (clinico.db).

CRUD con las reglas de la Resolución 1995 de 1999 (la historia clínica no se destruye):
  * Crear   : registro nuevo (consulta, evolución, antecedentes, resultado de examen, epicrisis) y adjuntos.
  * Leer    : por paciente, con versiones anteriores; búsqueda por texto, tipo, fechas, médico y diagnóstico.
  * Modificar: solo el autor; la versión anterior se guarda (trigger) con quién, cuándo y por qué.
  * Eliminar: no se borra; se ANULA con motivo y sigue visible como anulado.
La autorización (rol, turno, "romper el vidrio") se decide antes, en pharmacy_service.authorize().
No depende de Streamlit.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path

DATETIME_FMT = "%Y-%m-%d %H:%M:%S"
NEW_PATIENT_START = 1_000_000          # los ids del extracto HIS llegan a ~635.000
MAX_FILE_BYTES = 5 * 1024 * 1024
RECORD_TYPES = {"CONSULTA": "Consulta", "EVOLUCION": "Evolución", "ANTECEDENTES": "Antecedentes",
                "RESULTADO_EXAMEN": "Resultado de examen", "EPICRISIS": "Epicrisis"}
DOC_TYPES = {"CC": "Cédula de ciudadanía", "TI": "Tarjeta de identidad", "RC": "Registro civil",
             "CE": "Cédula de extranjería", "PPT": "Permiso por protección temporal", "PA": "Pasaporte"}
SEXES = ("Femenino", "Masculino", "Intersexual")
PATIENT_FIELDS = ("tipo_documento", "numero_documento", "nombres", "apellidos", "fecha_nacimiento", "sexo",
                  "telefono", "direccion", "asegurador", "regimen", "municipio")
_MAGIC = {"application/pdf": (b"%PDF",), "image/png": (b"\x89PNG\r\n\x1a\n",), "image/jpeg": (b"\xff\xd8\xff",)}


def _now(now: datetime | str | None) -> str:
    if now is None:
        return datetime.now().strftime(DATETIME_FMT)
    return now if isinstance(now, str) else now.strftime(DATETIME_FMT)


def _clean(value) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


# ---------------------------------------------------------------------------
# Pacientes
# ---------------------------------------------------------------------------
def ensure_history(conn: sqlite3.Connection, id_paciente: int, now: datetime | str | None = None) -> int:
    """Devuelve la historia clínica del paciente; la abre si no existe (una por paciente)."""
    row = conn.execute("SELECT id FROM historias_clinicas WHERE id_paciente = ?", (id_paciente,)).fetchone()
    if row:
        return row[0]
    return conn.execute("INSERT INTO historias_clinicas(id_paciente, creada_en) VALUES (?, ?)",
                        (id_paciente, _now(now))).lastrowid


def register_patient(conn: sqlite3.Connection, user_id: int, data: dict, now: datetime | str | None = None) -> int:
    """Paciente nuevo (no está en el extracto): documento y nombres obligatorios. Abre su historia clínica."""
    d = {k: _clean(data.get(k)) for k in PATIENT_FIELDS}
    if not d["numero_documento"] or not d["tipo_documento"]:
        raise sqlite3.IntegrityError("Tipo y número de documento son obligatorios")
    if not d["nombres"] or not d["apellidos"]:
        raise sqlite3.IntegrityError("Nombres y apellidos son obligatorios")
    if conn.execute("SELECT 1 FROM pacientes_clinicos WHERE tipo_documento = ? AND numero_documento = ?",
                    (d["tipo_documento"], d["numero_documento"])).fetchone():
        raise sqlite3.IntegrityError("Ya existe un paciente con ese documento")
    ts = _now(now)
    with conn:
        new_id = max(NEW_PATIENT_START, (conn.execute("SELECT MAX(id_paciente) FROM pacientes_clinicos")
                                         .fetchone()[0] or 0) + 1)
        conn.execute(f"INSERT INTO pacientes_clinicos(id_paciente, origen, {', '.join(PATIENT_FIELDS)}, edad, "
                     f"creado_por, creado_en) VALUES (?, 'REGISTRO', {', '.join('?' * len(PATIENT_FIELDS))}, ?, ?, ?)",
                     (new_id, *[d[k] for k in PATIENT_FIELDS], _age(d["fecha_nacimiento"], ts), user_id, ts))
        hc = ensure_history(conn, new_id, ts)
        conn.execute("INSERT INTO historia_clinica_eventos(historia_id, tipo, descripcion, autor_id, fecha) "
                     "VALUES (?, 'DATOS_PACIENTE', 'Paciente registrado y apertura de historia clínica', ?, ?)",
                     (hc, user_id, ts))
    return new_id


def import_his_patient(conn: sqlite3.Connection, analytics_db: Path | str, id_paciente: int, user_id: int | None,
                       now: datetime | str | None = None) -> int:
    """Trae un paciente del extracto del HIS (anonimizado: sin nombre) y le abre la historia clínica."""
    if conn.execute("SELECT 1 FROM pacientes_clinicos WHERE id_paciente = ?", (id_paciente,)).fetchone():
        ensure_history(conn, id_paciente, now)
        conn.commit()
        return id_paciente
    src = sqlite3.connect(f"{Path(analytics_db).resolve().as_uri()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    row = src.execute("SELECT * FROM pacientes WHERE id_paciente = ?", (id_paciente,)).fetchone()
    src.close()
    if row is None:
        raise sqlite3.IntegrityError(f"El paciente {id_paciente} no está en el extracto del HIS")
    ts = _now(now)
    with conn:
        conn.execute("INSERT INTO pacientes_clinicos(id_paciente, origen, tipo_documento, nombres, apellidos, edad, "
                     "sexo, asegurador, regimen, municipio, creado_por, creado_en) "
                     "VALUES (?, 'HIS', ?, ?, '', ?, ?, ?, ?, ?, ?, ?)",
                     (id_paciente, row["tipo_documento"], f"Paciente {id_paciente}", row["edad"],
                      row["sexo"] if row["sexo"] in SEXES else None, row["asegurador"], row["regimen"],
                      row["municipio"], user_id, ts))
        ensure_history(conn, id_paciente, ts)
    return id_paciente


def _age(birth: str | None, ts: str) -> int | None:
    if not birth:
        return None
    try:
        b, t = datetime.strptime(birth[:10], "%Y-%m-%d"), datetime.strptime(ts[:10], "%Y-%m-%d")
    except ValueError:
        raise sqlite3.IntegrityError("Fecha de nacimiento inválida (use AAAA-MM-DD)") from None
    if b > t:
        raise sqlite3.IntegrityError("La fecha de nacimiento no puede ser futura")
    return t.year - b.year - ((t.month, t.day) < (b.month, b.day))


def update_patient(conn: sqlite3.Connection, id_paciente: int, user_id: int, data: dict,
                   now: datetime | str | None = None) -> list[str]:
    """Actualiza datos de contacto e identificación. Devuelve los campos cambiados (quedan en la HC)."""
    current = get_patient(conn, id_paciente)
    if current is None:
        raise sqlite3.IntegrityError("Paciente inexistente")
    changes = {k: _clean(v) for k, v in data.items() if k in PATIENT_FIELDS and _clean(v) != current[k]}
    if current["origen"] == "REGISTRO" and ("numero_documento" in changes and not changes["numero_documento"]):
        raise sqlite3.IntegrityError("El número de documento es obligatorio")
    if not changes:
        return []
    ts = _now(now)
    sets = ", ".join(f"{k} = ?" for k in changes)
    with conn:
        extra = []
        if "fecha_nacimiento" in changes:
            sets += ", edad = ?"
            extra.append(_age(changes["fecha_nacimiento"], ts))
        conn.execute(f"UPDATE pacientes_clinicos SET {sets}, actualizado_por = ?, actualizado_en = ? "
                     "WHERE id_paciente = ?", (*changes.values(), *extra, user_id, ts, id_paciente))
        hc = ensure_history(conn, id_paciente, ts)
        conn.execute("INSERT INTO historia_clinica_eventos(historia_id, tipo, descripcion, autor_id, fecha) "
                     "VALUES (?, 'DATOS_PACIENTE', ?, ?, ?)",
                     (hc, "Datos del paciente actualizados: " + ", ".join(k.replace("_", " ") for k in changes),
                      user_id, ts))
    return list(changes)


def set_patient_status(conn: sqlite3.Connection, id_paciente: int, user_id: int, active: bool,
                       now: datetime | str | None = None) -> None:
    ts = _now(now)
    with conn:
        conn.execute("UPDATE pacientes_clinicos SET estado = ?, actualizado_por = ?, actualizado_en = ? "
                     "WHERE id_paciente = ?", ("ACTIVO" if active else "INACTIVO", user_id, ts, id_paciente))


def get_patient(conn: sqlite3.Connection, id_paciente: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM pacientes_clinicos WHERE id_paciente = ?", (id_paciente,)).fetchone()


def display_name(row) -> str:
    if row is None:
        return "Paciente"
    name = f"{row['nombres']} {row['apellidos']}".strip()
    return name or f"Paciente {row['id_paciente']}"


def search_patients(conn: sqlite3.Connection, query: str = "", limit: int = 50,
                    include_inactive: bool = False) -> list[sqlite3.Row]:
    """Por nombre, apellido, documento o id. Sin texto: los más recientes."""
    q = _clean(query) or ""
    where = [] if include_inactive else ["pc.estado = 'ACTIVO'"]
    args: list = []
    for word in q.split():
        where.append("(pc.nombres LIKE ? OR pc.apellidos LIKE ? OR pc.numero_documento LIKE ? "
                     "OR CAST(pc.id_paciente AS TEXT) = ?)")
        args += [f"%{word}%", f"%{word}%", f"{word}%", word]
    sql = f"""
        SELECT pc.*, h.id AS historia_id,
               (SELECT COUNT(*) FROM hc_registros r WHERE r.historia_id = h.id AND r.estado = 'ACTIVO') AS registros,
               (SELECT COUNT(*) FROM prescripciones p WHERE p.historia_id = h.id
                   AND p.estado IN ('VIGENTE','PARCIAL')) AS formulas_activas
          FROM pacientes_clinicos pc LEFT JOIN historias_clinicas h ON h.id_paciente = pc.id_paciente
         {'WHERE ' + ' AND '.join(where) if where else ''}
         ORDER BY COALESCE(pc.actualizado_en, pc.creado_en) DESC LIMIT ?"""
    return conn.execute(sql, [*args, limit]).fetchall()


# ---------------------------------------------------------------------------
# Registros de historia clínica
# ---------------------------------------------------------------------------
def create_record(conn: sqlite3.Connection, *, id_paciente: int, autor_id: int, tipo: str, titulo: str,
                  contenido: str, diagnostico_cie10: str | None = None, diagnostico_nombre: str | None = None,
                  plan: str | None = None, now: datetime | str | None = None) -> int:
    if tipo not in RECORD_TYPES:
        raise sqlite3.IntegrityError("Tipo de registro no válido")
    if get_patient(conn, id_paciente) is None:
        raise sqlite3.IntegrityError("Registra primero al paciente")
    ts = _now(now)
    with conn:
        hc = ensure_history(conn, id_paciente, ts)
        cur = conn.execute(
            "INSERT INTO hc_registros(historia_id, tipo, titulo, contenido, diagnostico_cie10, diagnostico_nombre, "
            "plan, autor_id, creado_en) VALUES (?,?,?,?,?,?,?,?,?)",
            (hc, tipo, (titulo or "").strip(), (contenido or "").strip(), _clean(diagnostico_cie10),
             _clean(diagnostico_nombre), (plan or "").strip() or None, autor_id, ts))
    return cur.lastrowid


def _own_active_record(conn, registro_id: int, user_id: int) -> sqlite3.Row:
    rec = conn.execute("SELECT * FROM hc_registros WHERE id = ?", (registro_id,)).fetchone()
    if rec is None:
        raise sqlite3.IntegrityError("Registro inexistente")
    if rec["autor_id"] != user_id:
        raise sqlite3.IntegrityError("Solo el profesional que escribió el registro puede corregirlo o anularlo")
    if rec["estado"] != "ACTIVO":
        raise sqlite3.IntegrityError("El registro está anulado")
    return rec


def update_record(conn: sqlite3.Connection, registro_id: int, user_id: int, changes: dict, motivo: str,
                  now: datetime | str | None = None) -> int:
    """Corrige un registro propio. La versión anterior queda guardada. Devuelve la nueva versión."""
    rec = _own_active_record(conn, registro_id, user_id)
    fields = ("titulo", "contenido", "diagnostico_cie10", "diagnostico_nombre", "plan")
    diff = {k: (changes[k] or "").strip() or None for k in fields if k in changes
            and ((changes[k] or "").strip() or None) != rec[k]}
    if not diff:
        raise sqlite3.IntegrityError("No hay cambios para guardar")
    sets = ", ".join(f"{k} = ?" for k in diff)
    with conn:
        conn.execute(f"UPDATE hc_registros SET {sets}, version = version + 1, actualizado_por = ?, "
                     "actualizado_en = ?, motivo_cambio = ? WHERE id = ?",
                     (*diff.values(), user_id, _now(now), (motivo or "").strip(), registro_id))
    return rec["version"] + 1


def annul_record(conn: sqlite3.Connection, registro_id: int, user_id: int, motivo: str,
                 now: datetime | str | None = None) -> None:
    _own_active_record(conn, registro_id, user_id)
    if len((motivo or "").strip()) < 10:
        raise sqlite3.IntegrityError("Escribe el motivo de la anulación (mínimo 10 caracteres)")
    with conn:
        conn.execute("UPDATE hc_registros SET estado = 'ANULADO', motivo_anulacion = ?, actualizado_por = ?, "
                     "actualizado_en = ? WHERE id = ?", (motivo.strip(), user_id, _now(now), registro_id))


_RECORD_COLS = """r.id, r.tipo, r.titulo, r.contenido, r.diagnostico_cie10, r.diagnostico_nombre, r.plan,
                  r.autor_id, u.nombre_mostrado AS autor, r.creado_en, r.version, r.actualizado_en,
                  r.motivo_cambio, r.estado, r.motivo_anulacion,
                  (SELECT COUNT(*) FROM hc_adjuntos a WHERE a.registro_id = r.id AND a.estado = 'ACTIVO') AS adjuntos"""


def records(conn: sqlite3.Connection, id_paciente: int, include_annulled: bool = True) -> list[sqlite3.Row]:
    extra = "" if include_annulled else "AND r.estado = 'ACTIVO'"
    return conn.execute(f"""
        SELECT {_RECORD_COLS} FROM hc_registros r JOIN historias_clinicas h ON h.id = r.historia_id
          JOIN usuarios u ON u.id = r.autor_id
         WHERE h.id_paciente = ? {extra} ORDER BY r.creado_en DESC, r.id DESC""", (id_paciente,)).fetchall()


def record_versions(conn: sqlite3.Connection, registro_id: int) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT v.*, u.nombre_mostrado AS reemplazada_por_nombre FROM hc_registros_versiones v
          LEFT JOIN usuarios u ON u.id = v.reemplazada_por
         WHERE v.registro_id = ? ORDER BY v.version DESC""", (registro_id,)).fetchall()


def search_records(conn: sqlite3.Connection, *, text: str = "", tipo: str | None = None, desde: str | None = None,
                   hasta: str | None = None, autor_id: int | None = None, id_paciente: int | None = None,
                   include_annulled: bool = False, limit: int = 200) -> list[sqlite3.Row]:
    """Búsqueda transversal. Devuelve también el contenido: la vista decide qué mostrar según el rol."""
    where, args = [], []
    for word in (_clean(text) or "").split():
        where.append("(r.titulo LIKE ? OR r.contenido LIKE ? OR r.diagnostico_cie10 LIKE ? "
                     "OR r.diagnostico_nombre LIKE ? OR pc.nombres LIKE ? OR pc.apellidos LIKE ? "
                     "OR pc.numero_documento LIKE ?)")
        args += [f"%{word}%"] * 4 + [f"%{word}%", f"%{word}%", f"{word}%"]
    if tipo:
        where.append("r.tipo = ?"); args.append(tipo)
    if desde:
        where.append("r.creado_en >= ?"); args.append(f"{desde} 00:00:00")
    if hasta:
        where.append("r.creado_en <= ?"); args.append(f"{hasta} 23:59:59")
    if autor_id:
        where.append("r.autor_id = ?"); args.append(autor_id)
    if id_paciente:
        where.append("h.id_paciente = ?"); args.append(id_paciente)
    if not include_annulled:
        where.append("r.estado = 'ACTIVO'")
    return conn.execute(f"""
        SELECT {_RECORD_COLS}, h.id_paciente,
               COALESCE(NULLIF(trim(pc.nombres || ' ' || pc.apellidos), ''), 'Paciente ' || h.id_paciente) AS paciente,
               pc.tipo_documento, pc.numero_documento
          FROM hc_registros r JOIN historias_clinicas h ON h.id = r.historia_id
          JOIN usuarios u ON u.id = r.autor_id
          LEFT JOIN pacientes_clinicos pc ON pc.id_paciente = h.id_paciente
         {'WHERE ' + ' AND '.join(where) if where else ''}
         ORDER BY r.creado_en DESC LIMIT ?""", [*args, limit]).fetchall()


def authors(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT DISTINCT u.id, u.nombre_mostrado FROM hc_registros r JOIN usuarios u "
                        "ON u.id = r.autor_id ORDER BY u.nombre_mostrado").fetchall()


# ---------------------------------------------------------------------------
# Adjuntos
# ---------------------------------------------------------------------------
def detect_mime(data: bytes) -> str | None:
    """Tipo real por la firma del archivo (no por la extensión, que se puede falsear)."""
    return next((mime for mime, sigs in _MAGIC.items() if any(data.startswith(s) for s in sigs)), None)


def add_attachment(conn: sqlite3.Connection, *, id_paciente: int, user_id: int, filename: str, data: bytes,
                   registro_id: int | None = None, descripcion: str | None = None,
                   now: datetime | str | None = None) -> int:
    if not data:
        raise sqlite3.IntegrityError("El archivo está vacío")
    if len(data) > MAX_FILE_BYTES:
        raise sqlite3.IntegrityError("El archivo supera 5 MB")
    mime = detect_mime(data)
    if mime is None:
        raise sqlite3.IntegrityError("Solo se aceptan PDF, PNG o JPG")
    ts = _now(now)
    with conn:
        hc = ensure_history(conn, id_paciente, ts)
        if registro_id is not None and not conn.execute(
                "SELECT 1 FROM hc_registros WHERE id = ? AND historia_id = ?", (registro_id, hc)).fetchone():
            raise sqlite3.IntegrityError("El registro no pertenece a este paciente")
        cur = conn.execute(
            "INSERT INTO hc_adjuntos(historia_id, registro_id, nombre_archivo, tipo_mime, tamano_bytes, sha256, "
            "contenido, descripcion, subido_por, subido_en) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (hc, registro_id, Path(filename).name[:120] or "adjunto", mime, len(data),
             hashlib.sha256(data).hexdigest(), sqlite3.Binary(data), _clean(descripcion), user_id, ts))
    return cur.lastrowid


def attachments(conn: sqlite3.Connection, id_paciente: int, include_annulled: bool = True) -> list[sqlite3.Row]:
    extra = "" if include_annulled else "AND a.estado = 'ACTIVO'"
    return conn.execute(f"""
        SELECT a.id, a.registro_id, a.nombre_archivo, a.tipo_mime, a.tamano_bytes, a.sha256, a.descripcion,
               a.subido_en, a.subido_por, u.nombre_mostrado AS subido_por_nombre, a.estado, a.motivo_anulacion
          FROM hc_adjuntos a JOIN historias_clinicas h ON h.id = a.historia_id
          JOIN usuarios u ON u.id = a.subido_por
         WHERE h.id_paciente = ? {extra} ORDER BY a.subido_en DESC""", (id_paciente,)).fetchall()


def attachment_content(conn: sqlite3.Connection, adjunto_id: int, id_paciente: int) -> tuple[str, str, bytes]:
    row = conn.execute("""SELECT a.nombre_archivo, a.tipo_mime, a.contenido FROM hc_adjuntos a
                          JOIN historias_clinicas h ON h.id = a.historia_id
                          WHERE a.id = ? AND h.id_paciente = ?""", (adjunto_id, id_paciente)).fetchone()
    if row is None:
        raise sqlite3.IntegrityError("Adjunto inexistente")
    return row[0], row[1], bytes(row[2])


def annul_attachment(conn: sqlite3.Connection, adjunto_id: int, user_id: int, motivo: str,
                     now: datetime | str | None = None) -> None:
    row = conn.execute("SELECT subido_por, estado FROM hc_adjuntos WHERE id = ?", (adjunto_id,)).fetchone()
    if row is None:
        raise sqlite3.IntegrityError("Adjunto inexistente")
    if row["subido_por"] != user_id:
        raise sqlite3.IntegrityError("Solo quien subió el adjunto puede anularlo")
    if row["estado"] != "ACTIVO":
        raise sqlite3.IntegrityError("El adjunto ya está anulado")
    if len((motivo or "").strip()) < 10:
        raise sqlite3.IntegrityError("Escribe el motivo de la anulación (mínimo 10 caracteres)")
    with conn:
        conn.execute("UPDATE hc_adjuntos SET estado = 'ANULADO', motivo_anulacion = ?, anulado_por = ?, "
                     "anulado_en = ? WHERE id = ?", (motivo.strip(), user_id, _now(now), adjunto_id))
