"""
demo_seed.py — Escenario de demostración del módulo clínico (clinico.db).

El extracto del HIS no trae usuarios, prescripciones ni citas, así que este módulo los SIEMBRA sobre
pacientes e ingresos reales de hospital.db. Todo lo sembrado es sintético y así se declara en la UI.

También administra el RELOJ CLÍNICO de la demo (persistido en clinico.db): permite simular un turno
nocturno o el paso de los 30 días de apartado sin depender de la hora real del servidor.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import clinical_records as cr
import pharmacy_service as ps

FMT = "%Y-%m-%d %H:%M:%S"
CLOCK_START = "2026-09-21 10:00:00"          # alineado con el corte analítico ("hoy" = 21/09/2026)

DEMO_USERS = [  # id, usuario, nombre, rol_id, registro, especialidad, id_paciente
    (1, "admin", "Admin · Gerencia", 1, None, None, None),
    (2, "dra.ruiz", "Dra. Ruiz", 2, "RM-19-4481", "MEDICINA GENERAL", None),
    (3, "enf.gomez", "Enf. Gómez", 3, "RE-19-2207", None, None),
    (4, "paciente.110", "Paciente 110", 4, None, None, 110),
]


def _hash(password: str) -> str:
    # Solo para la demo. En producción: argon2/bcrypt con sal por usuario.
    return "sha256$" + hashlib.sha256(password.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Reloj clínico
# ---------------------------------------------------------------------------
def _ensure_clock_table(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS demo_estado (clave TEXT PRIMARY KEY, valor TEXT NOT NULL)")


def get_clock(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT valor FROM demo_estado WHERE clave = 'reloj'").fetchone()
    return row[0] if row else CLOCK_START


def set_clock(conn: sqlite3.Connection, ts: str) -> None:
    with conn:
        conn.execute("INSERT INTO demo_estado(clave, valor) VALUES ('reloj', ?) "
                     "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor", (ts,))


def shift_clock(conn: sqlite3.Connection, hours: int) -> str:
    new = (datetime.strptime(get_clock(conn), FMT) + timedelta(hours=hours)).strftime(FMT)
    set_clock(conn, new)
    return new


def set_clock_hour(conn: sqlite3.Connection, hour: int) -> str:
    new = datetime.strptime(get_clock(conn), FMT).replace(hour=hour, minute=0, second=0).strftime(FMT)
    set_clock(conn, new)
    return new


# ---------------------------------------------------------------------------
# Siembra
# ---------------------------------------------------------------------------
def _pick_product(conn, pattern: str, needed: int, critical: bool | None = None) -> str:
    """Producto real que coincida con el patrón y tenga stock suficiente (con respaldo)."""
    crit = "" if critical is None else f"AND f.critico_continuidad = {int(critical)}"
    for pat in (pattern, "%"):
        row = conn.execute(f"""
            SELECT s.codigo FROM v_stock s JOIN productos_farmacia f ON f.codigo = s.codigo
             WHERE f.nombre LIKE ? AND f.tipo_item = 'Medicamento' AND s.disponible >= ? {crit}
             ORDER BY f.consumo_diario_promedio DESC LIMIT 1""", (pat, needed)).fetchone()
        if row:
            return row[0]
    raise RuntimeError(f"No hay producto con stock para {pattern}")


def _patients_from_analytics(analytics_db: Path) -> list[dict]:
    """Paciente 110 (portal) + un paciente real hospitalizado 'hoy' por servicio."""
    src = sqlite3.connect(f"{Path(analytics_db).resolve().as_uri()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    out = []
    row = src.execute("SELECT id_paciente, oid_ingreso, servicio, codigo_diagnostico, nombre_diagnostico, "
                      "fecha_ingreso FROM ingresos WHERE id_paciente = 110 ORDER BY fecha_ingreso DESC").fetchone()
    if row:
        out.append(dict(row))
    for service in ("UCI", "Hospitalización", "Pediatría", "Gineco-obstetricia", "Urgencias"):
        row = src.execute("""
            SELECT id_paciente, oid_ingreso, servicio, codigo_diagnostico, nombre_diagnostico, fecha_ingreso
              FROM ingresos WHERE servicio = ? AND fecha_fin_estimada >= '2026-09-21'
               AND codigo_diagnostico IS NOT NULL AND id_paciente <> 110
             ORDER BY fecha_ingreso DESC LIMIT 1""", (service,)).fetchone()
        if row:
            out.append(dict(row))
    src.close()
    return out


def seed(conn: sqlite3.Connection, analytics_db: Path) -> None:
    """Idempotente: si ya hay usuarios, no hace nada."""
    _ensure_clock_table(conn)
    if conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]:
        return
    patients = _patients_from_analytics(analytics_db)
    with conn:
        for uid, user, name, rol, reg, esp, pac in DEMO_USERS:
            conn.execute("INSERT INTO usuarios(id, usuario, hash_password, nombre_mostrado, rol_id, estado_cuenta, "
                         "registro_profesional, especialidad, id_paciente, creado_en) "
                         "VALUES (?,?,?,?,?, 'ACTIVO', ?,?,?, '2026-09-01 08:00:00')",
                         (uid, user, _hash("demo"), name, rol, reg, esp, pac))
        # Turnos diurnos 07:00–19:00 durante seis semanas (de noche nadie está en turno → "romper el vidrio")
        day = datetime(2026, 9, 14)
        while day <= datetime(2026, 10, 31):
            for uid, service in ((2, "Urgencias"), (3, "Urgencias")):
                conn.execute("INSERT INTO turnos(usuario_id, servicio, tipo, inicio, fin) VALUES (?,?, 'TURNO', ?, ?)",
                             (uid, service, day.replace(hour=7).strftime(FMT), day.replace(hour=19).strftime(FMT)))
            day += timedelta(days=1)
        for p in patients:
            hc = conn.execute("INSERT INTO historias_clinicas(id_paciente, creada_en) VALUES (?, ?)",
                              (p["id_paciente"], p["fecha_ingreso"])).lastrowid
            p["historia_id"] = hc
            conn.execute("INSERT INTO historia_clinica_eventos(historia_id, oid_ingreso, tipo, descripcion, autor_id, "
                         "fecha) VALUES (?,?, 'NOTA_EVOLUCION', ?, 2, ?)",
                         (hc, p["oid_ingreso"], f"Ingreso por {p['servicio']}: {p['codigo_diagnostico']} · "
                          f"{p['nombre_diagnostico'].capitalize()}", p["fecha_ingreso"]))
        set_clock(conn, CLOCK_START)

    by_service = {p["servicio"]: p for p in patients}
    pac110 = next((p for p in patients if p["id_paciente"] == 110), None)

    # Registro de pacientes: los sembrados vienen del extracto del HIS (anonimizados: sin nombre)
    for p in patients:
        cr.import_his_patient(conn, analytics_db, p["id_paciente"], None, p["fecha_ingreso"])

    def presc(pac, code, dosis, freq, dias, units, ambito, when, window=ps.RESERVE_HOURS):
        return ps.prescribe(conn, medico_id=2, id_paciente=pac["id_paciente"], codigo=code, dosis=dosis,
                            frecuencia_horas=freq, duracion_dias=dias, dosis_prescritas=units, ambito=ambito,
                            horas_ventana=window, now=when)

    # Paciente 110 (portal): control por contusión de muñeca → analgésico, entrega parcial. Las 15 tabletas que
    # faltan quedan apartadas 30 días; si no las reclama, vuelven a disponibles.
    if pac110:
        with conn:
            conn.execute("INSERT INTO citas(id_paciente, medico_id, especialidad, fecha_hora, motivo, estado, creada_en) "
                         "VALUES (110, 2, 'MEDICINA GENERAL', '2026-09-20 08:30:00', 'CONTROL', 'CUMPLIDA', "
                         "'2026-09-15 10:00:00')")
            conn.execute("INSERT INTO citas(id_paciente, medico_id, especialidad, fecha_hora, motivo, creada_en) "
                         "VALUES (110, 2, 'MEDICINA GENERAL', '2026-10-05 08:00:00', 'CONTROL', '2026-09-20 08:40:00')")
        cr.create_record(conn, id_paciente=110, autor_id=2, tipo="CONSULTA", titulo="Control por contusión de muñeca",
                         contenido="Paciente refiere dolor leve en muñeca derecha tras caída hace 5 días. Sin "
                                   "deformidad ni limitación funcional. Radiografía sin fractura.",
                         diagnostico_cie10="S600", diagnostico_nombre="Contusión de muñeca",
                         plan="Analgesia oral 7 días, hielo local y control en 2 semanas.", now="2026-09-20 08:45:00")
        code = _pick_product(conn, "ACETAMINOFEN 500 mg TABLETA%", 21)
        pid = presc(pac110, code, "500 mg vía oral", 8, 7, 21, "AMBULATORIA", "2026-09-20 09:00:00")
        ps.dispense(conn, pid, 3, 6, "2026-09-20 11:15:00")
    # UCI: fractura de fémur → anticoagulante al egreso (continuidad crítica). Si caduca, genera ALERTA.
    if "UCI" in by_service:
        code = _pick_product(conn, "ENOXAPARINA SODICA 40 MG%", 10, critical=True)
        presc(by_service["UCI"], code, "40 mg subcutánea", 24, 10, 10, "AMBULATORIA", "2026-09-21 08:00:00")
    # Hospitalización: insuficiencia cardiaca → diurético al egreso
    if "Hospitalización" in by_service:
        code = _pick_product(conn, "FUROSEMIDA%TABLETA%", 14)
        presc(by_service["Hospitalización"], code, "40 mg vía oral", 12, 7, 14, "AMBULATORIA",
              "2026-09-21 09:00:00")
    # Pediatría: influenza → antipirético intrahospitalario (no caduca: se administra en piso)
    if "Pediatría" in by_service:
        code = _pick_product(conn, "ACETAMINOFEN%JARABE%", 12)
        pid = presc(by_service["Pediatría"], code, "15 mg/kg (5 ml) vía oral", 6, 3, 12, "HOSPITALARIA",
                    "2026-09-21 06:00:00")
        ps.dispense(conn, pid, 3, 4, "2026-09-21 06:30:00")


def reset(clinical_db: Path, analytics_db: Path) -> sqlite3.Connection:
    """Borra clinico.db (y sus archivos WAL) y vuelve a sembrar el escenario inicial."""
    for suffix in ("", "-wal", "-shm"):
        Path(f"{clinical_db}{suffix}").unlink(missing_ok=True)
    conn = ps.init_clinical_db(analytics_db, clinical_db)
    seed(conn, analytics_db)
    return conn
