"""
pharmacy_service.py — Servicios de la base clínica (clinico.db): inicialización, caducidad de
fórmulas con retorno a stock, semáforo de inventario y autorización RBAC con auditoría.

La lógica transaccional vive en los triggers de sql/schema_clinico.sql; este módulo solo:
  1. crea la base y sincroniza el catálogo desde hospital.db (consumo real),
  2. ejecuta el job de caducidad (SQLite no tiene planificador interno),
  3. decide permisos y deja rastro en auditoria_accesos.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SCHEMA_FILE = BASE_DIR / "sql" / "schema_clinico.sql"
CLINICAL_DB = BASE_DIR / "clinico.db"

# Grupos ATC cuya suspensión abrupta es riesgosa: no se bloquean, se alertan
CONTINUITY_ATC_PREFIXES = ("A10A", "B01A", "N03A", "J05A", "H02AB", "C01AA", "L04A")
DATETIME_FMT = "%Y-%m-%d %H:%M:%S"
SCHEMA_VERSION = 11          # debe coincidir con el PRAGMA user_version del final de schema_clinico.sql
RESERVE_DAYS = 30           # las unidades formuladas quedan apartadas para el paciente durante 30 días
RESERVE_HOURS = RESERVE_DAYS * 24


def _now(now: datetime | str | None) -> str:
    if now is None:
        return datetime.now().strftime(DATETIME_FMT)
    return now if isinstance(now, str) else now.strftime(DATETIME_FMT)


def connect(path: Path | str = CLINICAL_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_clinical_db(analytics_db: Path | str, path: Path | str = CLINICAL_DB) -> sqlite3.Connection:
    """Crea clinico.db (si no existe) y carga el catálogo y el saldo inicial desde hospital.db."""
    path = Path(path)
    if path.exists():
        conn = connect(path)
        found = conn.execute("PRAGMA user_version").fetchone()[0]
        if found == SCHEMA_VERSION:
            return conn
        # Base de una versión anterior: se guarda un respaldo y se recrea (es la base de la demo).
        conn.close()
        backup = path.with_name(f"{path.stem}_respaldo_v{found}{path.suffix}")
        try:
            backup.unlink(missing_ok=True)
            path.rename(backup)
        except PermissionError as exc:  # Windows: otra app (o una ventana vieja de Streamlit) la tiene abierta
            raise RuntimeError("clinico.db es de una versión anterior y está en uso: cierra todas las ventanas "
                               "de Streamlit y vuelve a ejecutar la app") from exc
        for suffix in ("-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
    conn = connect(path)
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    src = sqlite3.connect(f"{Path(analytics_db).resolve().as_uri()}?mode=ro", uri=True)
    rows = src.execute("SELECT codigo, nombre, tipo_item, consumo_diario_promedio, stock_actual "
                       "FROM inventario_farmacia").fetchall()
    src.close()
    with conn:
        conn.executemany(
            "INSERT INTO productos_farmacia(codigo, nombre, tipo_item, consumo_diario_promedio, "
            "critico_continuidad) VALUES (?,?,?,?,?)",
            [(c, n, t, cdp or 0, int(c.upper().startswith(CONTINUITY_ATC_PREFIXES))) for c, n, t, cdp, _ in rows])
        conn.executemany(
            "INSERT INTO inventario_movimientos(codigo_producto, tipo, delta_disponible, delta_reservado, nota) "
            "VALUES (?, 'SALDO_INICIAL', ?, 0, 'Saldo inicial (stock simulado del MVP)')",
            [(c, int(s or 0)) for c, _, _, _, s in rows])
    return conn


# ---------------------------------------------------------------------------
# Job de caducidad: idempotente y seguro ante ejecuciones concurrentes
# ---------------------------------------------------------------------------
def expire_prescriptions(conn: sqlite3.Connection, now: datetime | str | None = None) -> list[int]:
    """Caduca las fórmulas ambulatorias no reclamadas a tiempo. Los triggers devuelven las unidades
    reservadas al inventario, registran el evento en la HC y marcan la reevaluación obligatoria.
    Ejecutarlo dos veces no duplica nada: el WHERE excluye lo ya caducado y un índice único impide
    una segunda liberación de la misma fórmula."""
    ts = _now(now)
    with conn:  # una transacción: o se aplica todo o nada
        expired = [r["id"] for r in conn.execute(
            "SELECT id FROM prescripciones WHERE ambito = 'AMBULATORIA' AND estado IN ('VIGENTE','PARCIAL') "
            "AND fecha_limite_reclamo < ?", (ts,))]
        if not expired:
            return []
        marks = ",".join("?" * len(expired))
        conn.execute(f"UPDATE prescripciones SET estado = 'CADUCADA' WHERE id IN ({marks}) "
                     "AND estado IN ('VIGENTE','PARCIAL')", expired)
        # Medicamentos de continuidad crítica: además del bloqueo normal, alerta para búsqueda activa
        conn.execute(f"""
            INSERT INTO historia_clinica_eventos(historia_id, oid_ingreso, tipo, descripcion, autor_id, fecha)
            SELECT p.historia_id, p.oid_ingreso, 'ALERTA',
                   'Medicamento de continuidad crítica no reclamado: contactar al paciente', NULL, ?
              FROM prescripciones p JOIN productos_farmacia f ON f.codigo = p.codigo_producto
             WHERE p.id IN ({marks}) AND f.critico_continuidad = 1""", [ts, *expired])
    return expired


def stock_semaphore(conn: sqlite3.Connection, only: tuple[str, ...] = ("ROJO", "AMARILLO")) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(only))
    return conn.execute(f"SELECT s.*, f.critico_continuidad FROM v_semaforo_stock s "
                        f"JOIN productos_farmacia f ON f.codigo = s.codigo WHERE s.semaforo IN ({marks}) "
                        "ORDER BY s.dias_cobertura", only).fetchall()


def purchase_order_csv(conn: sqlite3.Connection) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["codigo", "nombre", "disponible", "consumo_diario", "dias_cobertura", "cantidad_a_pedir"])
    for r in stock_semaphore(conn, ("ROJO",)):
        writer.writerow([r["codigo"], r["nombre"], r["disponible"], r["consumo_diario_promedio"],
                         r["dias_cobertura"], r["orden_sugerida_15d"]])
    return buf.getvalue().encode("utf-8-sig")  # BOM: Excel en español abre bien las tildes


# ---------------------------------------------------------------------------
# Autorización (RBAC + estado de cuenta + contexto de turno) con auditoría
# ---------------------------------------------------------------------------
@dataclass
class Decision:
    allowed: bool
    reason: str
    emergency: bool = False


CLINICAL_PERMS = {"hc.ver_completa", "hc.ver_notas", "prescripcion.crear", "dispensacion.registrar",
                  "dosis.registrar", "hc.registrar"}


def authorize(conn: sqlite3.Connection, user_id: int, permission: str, id_paciente: int | None = None,
              emergency_justification: str | None = None, now: datetime | str | None = None,
              ip: str | None = None) -> Decision:
    """Reglas, en orden:
    1. La cuenta debe estar ACTIVA (SUSPENDIDO/INACTIVO/PENDIENTE no entran).
    2. El rol debe tener el permiso.
    3. PACIENTE solo accede a su propio id_paciente.
    4. Acciones clínicas exigen estar EN TURNO o DE GUARDIA; fuera de turno solo con
       'romper el vidrio' (hc.acceso_emergencia + justificación escrita).
    Toda decisión sobre datos de un paciente queda en auditoria_accesos."""
    ts = _now(now)
    user = conn.execute("SELECT u.*, r.codigo AS rol FROM usuarios u JOIN roles r ON r.id = u.rol_id "
                        "WHERE u.id = ?", (user_id,)).fetchone()

    def perm(code: str) -> bool:
        return conn.execute("SELECT 1 FROM rol_permisos rp JOIN permisos p ON p.id = rp.permiso_id "
                            "WHERE rp.rol_id = ? AND p.codigo = ?", (user["rol_id"], code)).fetchone() is not None

    if user is None:
        decision = Decision(False, "Usuario inexistente")
    elif user["estado_cuenta"] != "ACTIVO":
        decision = Decision(False, f"Cuenta {user['estado_cuenta'].lower()}")
    elif not perm(permission):
        decision = Decision(False, f"El rol {user['rol']} no tiene el permiso {permission}")
    elif user["rol"] == "PACIENTE" and id_paciente is not None and id_paciente != user["id_paciente"]:
        decision = Decision(False, "Un paciente solo puede consultar su propia información")
    elif permission in CLINICAL_PERMS and user["rol"] in ("DOCTOR", "ENFERMERIA"):
        on_duty = conn.execute("SELECT 1 FROM turnos WHERE usuario_id = ? AND ? BETWEEN inicio AND fin",
                               (user_id, ts)).fetchone() is not None
        if on_duty:
            decision = Decision(True, "En turno")
        elif emergency_justification and perm("hc.acceso_emergencia"):
            decision = Decision(True, "Acceso de emergencia justificado", emergency=True)
        else:
            decision = Decision(False, "Fuera de turno: use acceso de emergencia con justificación")
    else:
        decision = Decision(True, "Permitido")

    if id_paciente is not None or not decision.allowed:
        with conn:
            conn.execute(
                "INSERT INTO auditoria_accesos(fecha, usuario_id, accion, recurso, id_paciente, acceso_emergencia, "
                "justificacion, ip) VALUES (?,?,?,?,?,?,?,?)",
                (ts, user_id if user else None, ("PERMITIDO " if decision.allowed else "DENEGADO ") + permission,
                 decision.reason, id_paciente, int(decision.emergency),
                 emergency_justification if decision.emergency else None, ip))
    return decision


# ---------------------------------------------------------------------------
# Operaciones para la interfaz (la vista no escribe SQL)
# ---------------------------------------------------------------------------
def user_permissions(conn: sqlite3.Connection, user_id: int) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT p.codigo FROM usuarios u JOIN rol_permisos rp ON rp.rol_id = u.rol_id "
        "JOIN permisos p ON p.id = rp.permiso_id WHERE u.id = ? AND u.estado_cuenta = 'ACTIVO'", (user_id,))}


def current_shift(conn: sqlite3.Connection, user_id: int, now: datetime | str | None = None) -> sqlite3.Row | None:
    return conn.execute("SELECT tipo, servicio, inicio, fin FROM turnos WHERE usuario_id = ? "
                        "AND ? BETWEEN inicio AND fin ORDER BY fin DESC LIMIT 1", (user_id, _now(now))).fetchone()


def clinical_patients(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT h.id AS historia_id, h.id_paciente, h.estado, "
                        "(SELECT COUNT(*) FROM prescripciones p WHERE p.historia_id = h.id "
                        " AND p.estado IN ('VIGENTE','PARCIAL')) AS formulas_activas "
                        "FROM historias_clinicas h ORDER BY h.id_paciente").fetchall()


def patient_timeline(conn: sqlite3.Connection, id_paciente: int) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT e.fecha, e.tipo, e.descripcion, e.oid_ingreso,
               COALESCE(u.nombre_mostrado, 'Sistema (proceso automático)') AS autor
          FROM historia_clinica_eventos e JOIN historias_clinicas h ON h.id = e.historia_id
          LEFT JOIN usuarios u ON u.id = e.autor_id
         WHERE h.id_paciente = ? ORDER BY e.fecha DESC, e.id DESC""", (id_paciente,)).fetchall()


def patient_prescriptions(conn: sqlite3.Connection, id_paciente: int) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT p.id, f.nombre AS producto, p.codigo_producto, p.dosis, p.frecuencia_horas, p.duracion_dias,
               p.dosis_prescritas, p.dosis_entregadas, p.ambito, p.fecha_prescripcion, p.fecha_limite_reclamo,
               p.estado, p.requiere_reevaluacion, f.critico_continuidad, u.nombre_mostrado AS medico,
               p.fecha_apartado,
               (SELECT MIN(o.fecha_estimada_llegada) FROM pedidos_compra o WHERE o.codigo_producto = p.codigo_producto
                   AND o.estado = 'EN_CAMINO') AS llegada_estimada
          FROM prescripciones p JOIN productos_farmacia f ON f.codigo = p.codigo_producto
          JOIN usuarios u ON u.id = p.medico_id
         WHERE p.id_paciente = ? ORDER BY p.fecha_prescripcion DESC""", (id_paciente,)).fetchall()


def product_status(conn: sqlite3.Connection, codigo: str, id_paciente: int | None = None) -> dict:
    """Stock, semáforo y si el producto está bloqueado para el paciente (regla R1)."""
    row = conn.execute("SELECT s.*, f.critico_continuidad FROM v_semaforo_stock s "
                       "JOIN productos_farmacia f ON f.codigo = s.codigo WHERE s.codigo = ?", (codigo,)).fetchone()
    blocked = bool(id_paciente is not None and conn.execute(
        "SELECT 1 FROM prescripciones WHERE id_paciente = ? AND codigo_producto = ? AND requiere_reevaluacion = 1",
        (id_paciente, codigo)).fetchone())
    info = dict(row) if row else {}
    info["bloqueado_reevaluacion"] = blocked
    return info


def prescribe(conn: sqlite3.Connection, *, medico_id: int, id_paciente: int, codigo: str, dosis: str,
              frecuencia_horas: int, duracion_dias: int, dosis_prescritas: int, ambito: str,
              horas_ventana: int = RESERVE_HOURS, now: datetime | str | None = None) -> int:
    """Crea la fórmula; los triggers reservan stock, registran la HC y aplican el bloqueo R1.
    Lanza sqlite3.IntegrityError con un mensaje legible si una regla lo impide."""
    ts = _now(now)
    hc = conn.execute("SELECT id FROM historias_clinicas WHERE id_paciente = ?", (id_paciente,)).fetchone()
    if hc is None:
        raise sqlite3.IntegrityError("El paciente no tiene historia clínica abierta")
    oid = conn.execute("SELECT oid_ingreso FROM historia_clinica_eventos WHERE historia_id = ? "
                       "AND oid_ingreso IS NOT NULL ORDER BY fecha DESC LIMIT 1", (hc["id"],)).fetchone()
    # Ambulatoria sin existencias suficientes: no se rechaza; queda en espera y se aparta al llegar el pedido
    available = conn.execute("SELECT disponible FROM v_stock WHERE codigo = ?", (codigo,)).fetchone()
    state = ("PENDIENTE_STOCK" if ambito == "AMBULATORIA" and (available is None or available[0] < dosis_prescritas)
             else "VIGENTE")
    with conn:
        cur = conn.execute(
            "INSERT INTO prescripciones(historia_id, id_paciente, oid_ingreso, medico_id, codigo_producto, dosis, "
            "frecuencia_horas, duracion_dias, dosis_prescritas, ambito, fecha_prescripcion, horas_ventana_reclamo, "
            "estado) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (hc["id"], id_paciente, oid["oid_ingreso"] if oid else None, medico_id, codigo, dosis,
             frecuencia_horas, duracion_dias, dosis_prescritas, ambito, ts, horas_ventana, state))
    return cur.lastrowid


def dispensing_queue(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT p.id, p.id_paciente, f.nombre AS producto, p.codigo_producto, p.ambito, p.dosis,
               p.dosis_prescritas, p.dosis_entregadas, p.dosis_prescritas - p.dosis_entregadas AS pendientes,
               p.fecha_prescripcion, p.fecha_limite_reclamo, p.estado, f.critico_continuidad,
               u.nombre_mostrado AS medico
          FROM prescripciones p JOIN productos_farmacia f ON f.codigo = p.codigo_producto
          JOIN usuarios u ON u.id = p.medico_id
         WHERE p.estado IN ('VIGENTE','PARCIAL') ORDER BY p.fecha_limite_reclamo""").fetchall()


def dispense(conn: sqlite3.Connection, prescripcion_id: int, usuario_id: int, cantidad: int,
             now: datetime | str | None = None) -> None:
    with conn:
        conn.execute("INSERT INTO dispensaciones(prescripcion_id, usuario_id, cantidad, fecha) VALUES (?,?,?,?)",
                     (prescripcion_id, usuario_id, cantidad, _now(now)))


def stock_of(conn: sqlite3.Connection, codigos: list[str]) -> dict[str, sqlite3.Row]:
    if not codigos:
        return {}
    marks = ",".join("?" * len(codigos))
    return {r["codigo"]: r for r in conn.execute(f"SELECT * FROM v_stock WHERE codigo IN ({marks})", codigos)}


def expiry_details(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return conn.execute(f"""
        SELECT p.id, p.id_paciente, p.codigo_producto, f.nombre AS producto, p.dosis_prescritas - p.dosis_entregadas AS devueltas,
               f.critico_continuidad, p.fecha_limite_reclamo
          FROM prescripciones p JOIN productos_farmacia f ON f.codigo = p.codigo_producto
         WHERE p.id IN ({marks})""", ids).fetchall()


def patient_appointments(conn: sqlite3.Connection, id_paciente: int) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT c.id, c.fecha_hora, c.especialidad, c.motivo, c.estado, c.prescripcion_origen_id,
               COALESCE(u.nombre_mostrado, 'Por asignar') AS medico, f.nombre AS producto_origen
          FROM citas c LEFT JOIN usuarios u ON u.id = c.medico_id
          LEFT JOIN prescripciones p ON p.id = c.prescripcion_origen_id
          LEFT JOIN productos_farmacia f ON f.codigo = p.codigo_producto
         WHERE c.id_paciente = ? ORDER BY c.fecha_hora DESC""", (id_paciente,)).fetchall()


def request_reevaluation(conn: sqlite3.Connection, id_paciente: int, prescripcion_id: int,
                         fecha_hora: str, now: datetime | str | None = None) -> int:
    """El paciente solicita la cita que levanta el bloqueo (solo sobre SUS fórmulas caducadas)."""
    presc = conn.execute("SELECT id FROM prescripciones WHERE id = ? AND id_paciente = ? AND estado = 'CADUCADA' "
                         "AND requiere_reevaluacion = 1", (prescripcion_id, id_paciente)).fetchone()
    if presc is None:
        raise sqlite3.IntegrityError("Solo se puede pedir reevaluación de una fórmula propia caducada")
    if conn.execute("SELECT 1 FROM citas WHERE prescripcion_origen_id = ? AND estado = 'PROGRAMADA'",
                    (prescripcion_id,)).fetchone():
        raise sqlite3.IntegrityError("Ya existe una cita de reevaluación programada para esta fórmula")
    ts = _now(now)
    with conn:
        cur = conn.execute(
            "INSERT INTO citas(id_paciente, especialidad, fecha_hora, motivo, prescripcion_origen_id, creada_en) "
            "VALUES (?, 'MEDICINA GENERAL', ?, 'REEVALUACION_FORMULA', ?, ?)",
            (id_paciente, fecha_hora, prescripcion_id, ts))
        hc = conn.execute("SELECT id FROM historias_clinicas WHERE id_paciente = ?", (id_paciente,)).fetchone()
        conn.execute("INSERT INTO historia_clinica_eventos(historia_id, tipo, descripcion, fecha) "
                     "VALUES (?, 'CITA', 'Paciente solicita cita de reevaluación por fórmula caducada (cita: ' || ? || ')', ?)",
                     (hc["id"], fecha_hora[:16], ts))
    return cur.lastrowid


def complete_appointment(conn: sqlite3.Connection, cita_id: int, medico_id: int,
                         now: datetime | str | None = None) -> None:
    """Marca la cita como cumplida; si es de reevaluación, el trigger R5 levanta el bloqueo."""
    ts = _now(now)
    with conn:
        conn.execute("UPDATE citas SET estado = 'CUMPLIDA', medico_id = COALESCE(medico_id, ?) WHERE id = ? "
                     "AND estado = 'PROGRAMADA'", (medico_id, cita_id))
        conn.execute("""INSERT INTO historia_clinica_eventos(historia_id, tipo, descripcion, autor_id, fecha)
                        SELECT h.id, 'CITA', 'Cita cumplida: ' || CASE c.motivo
                                   WHEN 'REEVALUACION_FORMULA' THEN 'reevaluación de fórmula caducada; se levanta el bloqueo'
                                   WHEN 'PRIMERA_VEZ' THEN 'primera vez' WHEN 'CONTROL' THEN 'control'
                                   ELSE 'interconsulta' END, ?, ?
                          FROM citas c JOIN historias_clinicas h ON h.id_paciente = c.id_paciente
                         WHERE c.id = ?""", (medico_id, ts, cita_id))


def audit_log(conn: sqlite3.Connection, limit: int = 500) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT a.fecha, COALESCE(u.nombre_mostrado, '—') AS usuario, r.nombre AS rol, a.accion, a.recurso,
               a.id_paciente, a.acceso_emergencia, a.justificacion
          FROM auditoria_accesos a LEFT JOIN usuarios u ON u.id = a.usuario_id
          LEFT JOIN roles r ON r.id = u.rol_id ORDER BY a.id DESC LIMIT ?""", (limit,)).fetchall()


# ---------------------------------------------------------------------------
# Gestión de inventario (permiso inventario.auditar). El stock nunca se edita: se registran movimientos.
# ---------------------------------------------------------------------------
def inventory(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Existencias actuales por ítem con su semáforo y el pedido recomendado a 15 días."""
    return conn.execute("""
        SELECT s.codigo, s.nombre, s.tipo_item, s.disponible, s.reservado, s.consumo_diario_promedio,
               s.dias_cobertura, s.semaforo, s.orden_sugerida_15d, f.critico_continuidad
          FROM v_semaforo_stock s JOIN productos_farmacia f ON f.codigo = s.codigo
         ORDER BY CASE s.semaforo WHEN 'ROJO' THEN 0 WHEN 'AMARILLO' THEN 1 WHEN 'VERDE' THEN 2 ELSE 3 END,
                  s.consumo_diario_promedio DESC""").fetchall()


def receive_stock(conn: sqlite3.Connection, codigo: str, cantidad: int, usuario_id: int, nota: str = "",
                  now: datetime | str | None = None) -> list[int]:
    """Llegada de un pedido: suma unidades disponibles (ENTRADA_COMPRA), cierra el pedido en camino más antiguo
    y aparta las fórmulas que esperaban este producto. Devuelve las fórmulas apartadas."""
    if cantidad <= 0:
        raise sqlite3.IntegrityError("La cantidad recibida debe ser mayor que cero")
    ts = _now(now)
    with conn:
        conn.execute("INSERT INTO inventario_movimientos(codigo_producto, tipo, delta_disponible, delta_reservado, "
                     "usuario_id, nota, fecha) VALUES (?, 'ENTRADA_COMPRA', ?, 0, ?, ?, ?)",
                     (codigo, int(cantidad), usuario_id, nota or "Llegada de pedido", ts))
        order = conn.execute("SELECT id FROM pedidos_compra WHERE codigo_producto = ? AND estado = 'EN_CAMINO' "
                             "ORDER BY fecha_estimada_llegada, id LIMIT 1", (codigo,)).fetchone()
        if order:
            conn.execute("UPDATE pedidos_compra SET estado = 'RECIBIDO', fecha_recibido = ? WHERE id = ?",
                         (ts, order[0]))
    return allocate_backorders(conn, codigo, ts)


def allocate_backorders(conn: sqlite3.Connection, codigo: str, now: datetime | str | None = None) -> list[int]:
    """Aparta las fórmulas en espera de este producto, en orden de llegada, mientras alcance el stock.
    Cada una pasa a VIGENTE y su plazo de 30 días empieza ahora (trigger R2c)."""
    ts = _now(now)
    done = []
    with conn:
        for row in conn.execute("SELECT id, dosis_prescritas FROM prescripciones WHERE codigo_producto = ? "
                                "AND estado = 'PENDIENTE_STOCK' ORDER BY fecha_prescripcion, id", (codigo,)).fetchall():
            free = conn.execute("SELECT disponible FROM v_stock WHERE codigo = ?", (codigo,)).fetchone()[0]
            if free < row["dosis_prescritas"]:
                break  # FIFO estricto: no se salta a quien llegó primero
            conn.execute("UPDATE prescripciones SET estado = 'VIGENTE', fecha_apartado = ? WHERE id = ?", (ts, row["id"]))
            done.append(row["id"])
    return done


def create_order(conn: sqlite3.Connection, codigo: str, cantidad: int, fecha_estimada: str, usuario_id: int,
                 proveedor: str = "", now: datetime | str | None = None) -> int:
    """Pedido a proveedor en camino: su fecha estimada es la que se le informa al paciente."""
    if cantidad <= 0:
        raise sqlite3.IntegrityError("La cantidad pedida debe ser mayor que cero")
    ts = _now(now)
    if fecha_estimada < ts[:10]:
        raise sqlite3.IntegrityError("La fecha estimada de llegada no puede ser anterior a hoy")
    with conn:
        return conn.execute("INSERT INTO pedidos_compra(codigo_producto, cantidad, proveedor, fecha_pedido, "
                            "fecha_estimada_llegada, creado_por) VALUES (?,?,?,?,?,?)",
                            (codigo, int(cantidad), proveedor.strip() or None, ts, fecha_estimada, usuario_id)).lastrowid


def open_orders(conn: sqlite3.Connection, codigo: str | None = None) -> list[sqlite3.Row]:
    where, args = ("AND o.codigo_producto = ?", [codigo]) if codigo else ("", [])
    return conn.execute(f"""
        SELECT o.*, f.nombre AS producto,
               (SELECT COUNT(*) FROM prescripciones p WHERE p.codigo_producto = o.codigo_producto
                   AND p.estado = 'PENDIENTE_STOCK') AS pacientes_esperando
          FROM pedidos_compra o JOIN productos_farmacia f ON f.codigo = o.codigo_producto
         WHERE o.estado = 'EN_CAMINO' {where} ORDER BY o.fecha_estimada_llegada""", args).fetchall()


def expected_arrival(conn: sqlite3.Connection, codigo: str) -> str | None:
    row = conn.execute("SELECT MIN(fecha_estimada_llegada) FROM pedidos_compra WHERE codigo_producto = ? "
                       "AND estado = 'EN_CAMINO'", (codigo,)).fetchone()
    return row[0] if row else None


def backorders(conn: sqlite3.Connection, codigo: str | None = None) -> list[sqlite3.Row]:
    """Fórmulas en espera de existencias, con la fecha estimada de llegada del pedido (si lo hay)."""
    where, args = ("AND p.codigo_producto = ?", [codigo]) if codigo else ("", [])
    return conn.execute(f"""
        SELECT p.id, p.id_paciente, p.codigo_producto, f.nombre AS producto, p.dosis_prescritas, p.fecha_prescripcion,
               u.nombre_mostrado AS medico,
               COALESCE(NULLIF(trim(pc.nombres || ' ' || pc.apellidos), ''), 'Paciente ' || p.id_paciente) AS paciente,
               (SELECT MIN(o.fecha_estimada_llegada) FROM pedidos_compra o WHERE o.codigo_producto = p.codigo_producto
                   AND o.estado = 'EN_CAMINO') AS llegada_estimada
          FROM prescripciones p JOIN productos_farmacia f ON f.codigo = p.codigo_producto
          JOIN usuarios u ON u.id = p.medico_id LEFT JOIN pacientes_clinicos pc ON pc.id_paciente = p.id_paciente
         WHERE p.estado = 'PENDIENTE_STOCK' {where} ORDER BY p.fecha_prescripcion""", args).fetchall()


def adjust_stock(conn: sqlite3.Connection, codigo: str, conteo_fisico: int, usuario_id: int, motivo: str,
                 now: datetime | str | None = None) -> int:
    """Conteo físico: registra un AJUSTE por la diferencia con lo que dice el sistema. Devuelve la diferencia."""
    if conteo_fisico < 0:
        raise sqlite3.IntegrityError("El conteo físico no puede ser negativo")
    if len((motivo or "").strip()) < 5:
        raise sqlite3.IntegrityError("Escribe el motivo del ajuste")
    current = conn.execute("SELECT disponible FROM v_stock WHERE codigo = ?", (codigo,)).fetchone()
    if current is None:
        raise sqlite3.IntegrityError("Producto inexistente")
    delta = int(conteo_fisico) - int(current["disponible"])
    if delta:
        with conn:
            conn.execute("INSERT INTO inventario_movimientos(codigo_producto, tipo, delta_disponible, delta_reservado, "
                         "usuario_id, nota, fecha) VALUES (?, 'AJUSTE', ?, 0, ?, ?, ?)",
                         (codigo, delta, usuario_id, motivo.strip(), _now(now)))
    return delta


def movements(conn: sqlite3.Connection, codigo: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
    where, params = ("WHERE m.codigo_producto = ?", (codigo, limit)) if codigo else ("", (limit,))
    return conn.execute(f"""
        SELECT m.fecha, f.nombre AS producto, m.tipo, m.delta_disponible, m.delta_reservado,
               COALESCE(u.nombre_mostrado, 'Sistema') AS usuario, m.nota
          FROM inventario_movimientos m JOIN productos_farmacia f ON f.codigo = m.codigo_producto
          LEFT JOIN usuarios u ON u.id = m.usuario_id {where}
         ORDER BY m.id DESC LIMIT ?""", params).fetchall()


def reservations(conn: sqlite3.Connection, codigo: str | None = None) -> list[sqlite3.Row]:
    """Unidades apartadas para pacientes (fórmulas ambulatorias vigentes con dosis por reclamar)."""
    where, args = ("AND p.codigo_producto = ?", [codigo]) if codigo else ("", [])
    return conn.execute(f"""
        SELECT p.id, p.id_paciente, p.codigo_producto, f.nombre AS producto,
               p.dosis_prescritas - p.dosis_entregadas AS apartadas, p.fecha_prescripcion, p.fecha_limite_reclamo,
               p.estado, u.nombre_mostrado AS medico,
               COALESCE(NULLIF(trim(pc.nombres || ' ' || pc.apellidos), ''), 'Paciente ' || p.id_paciente) AS paciente
          FROM prescripciones p JOIN productos_farmacia f ON f.codigo = p.codigo_producto
          JOIN usuarios u ON u.id = p.medico_id
          LEFT JOIN pacientes_clinicos pc ON pc.id_paciente = p.id_paciente
         WHERE p.ambito = 'AMBULATORIA' AND p.estado IN ('VIGENTE','PARCIAL') {where}
         ORDER BY p.fecha_limite_reclamo""", args).fetchall()


def audit(conn: sqlite3.Connection, user_id: int, accion: str, recurso: str, id_paciente: int | None = None,
          now: datetime | str | None = None) -> None:
    """Deja rastro de una acción (búsquedas, descargas de adjuntos) en la bitácora inmutable."""
    with conn:
        conn.execute("INSERT INTO auditoria_accesos(fecha, usuario_id, accion, recurso, id_paciente) "
                     "VALUES (?,?,?,?,?)", (_now(now), user_id, accion, recurso[:300], id_paciente))
