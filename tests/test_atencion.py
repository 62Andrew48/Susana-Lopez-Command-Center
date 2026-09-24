"""Citas, turnos de atención, usuarios, turnos del personal y recuperación de contraseña."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auth  # noqa: E402
import config  # noqa: E402
import demo_seed as ds  # noqa: E402
import pharmacy_service as ps  # noqa: E402
import scheduling as sch  # noqa: E402
import staff  # noqa: E402

NOW = "2026-09-21 10:00:00"
ADMIN, DRA, DR_PAREDES, ADMISIONES = 1, 2, 5, 7


@pytest.fixture()
def clin(tmp_path):
    c = ps.init_clinical_db(config.DB_PATH, tmp_path / "clinico.db")
    ds.seed(c, config.DB_PATH)
    yield c
    c.close()


def _pid(clin, doc):
    return clin.execute("SELECT id_paciente FROM pacientes_clinicos WHERE numero_documento = ?", (doc,)).fetchone()[0]


# --- Citas ------------------------------------------------------------------------------
def test_slots_come_from_doctor_shifts_and_skip_booked_and_past(clin):
    slots = sch.free_slots(clin, especialidad="MEDICINA GENERAL", day="2026-09-21", now=NOW)
    times = {s["fecha_hora"][11:16] for s in slots}
    assert "10:20" not in times and "11:00" not in times          # ya agendadas en la demo
    assert "09:00" not in times                                     # ya pasó
    assert {"10:40", "14:00", "16:40"} <= times and "17:00" not in times
    assert all(s["medico_id"] == DRA for s in slots)


def test_booking_prevents_double_booking(clin):
    laura = _pid(clin, "1061700001")
    cid = sch.book(clin, id_paciente=laura, medico_id=DRA, fecha_hora="2026-09-22 08:00:00", motivo="CONTROL",
                   creada_por=20, now=NOW, nota="Control de neumonía")  # 20 = usuario paciente.laura
    with pytest.raises(sqlite3.IntegrityError, match="no está disponible"):
        sch.book(clin, id_paciente=110, medico_id=DRA, fecha_hora="2026-09-22 08:00:00", motivo="CONTROL",
                 creada_por=ADMISIONES, now=NOW)
    with pytest.raises(sqlite3.IntegrityError):  # la base lo impide aunque se salte la validación
        with clin:
            clin.execute("INSERT INTO citas(id_paciente, medico_id, especialidad, fecha_hora, motivo) "
                         "VALUES (110, 2, 'MEDICINA GENERAL', '2026-09-22 08:00:00', 'CONTROL')")
    with pytest.raises(sqlite3.IntegrityError):  # un paciente no cancela citas ajenas
        sch.cancel(clin, cid, 110, "no puedo", NOW, only_patient=110)
    sch.cancel(clin, cid, 20, "Viaje familiar", NOW, only_patient=laura)
    assert any(s["fecha_hora"] == "2026-09-22 08:00:00"
               for s in sch.free_slots(clin, medico_id=DRA, day="2026-09-22", now=NOW))


def test_check_in_queue_priority_and_position(clin):
    carlos, rosa, laura = _pid(clin, "10542001"), _pid(clin, "25270444"), _pid(clin, "1061700001")
    assert sch.patient_tickets(clin, carlos, NOW)[0]["codigo"] == "C-001"
    t2 = sch.issue_ticket(clin, servicio="CONSULTA", id_paciente=laura, by=ADMISIONES, now=NOW)
    t3 = sch.issue_ticket(clin, servicio="CONSULTA", id_paciente=110, by=ADMISIONES, now=NOW, prioridad=True)
    assert (t2, t3) == ("C-002", "C-003")
    assert sch.patient_tickets(clin, laura, NOW)[0]["antes"] == 2     # C-003 tiene prioridad y C-001 llegó antes
    with pytest.raises(sqlite3.IntegrityError, match="ya tiene el turno"):
        sch.issue_ticket(clin, servicio="CONSULTA", id_paciente=laura, by=ADMISIONES, now=NOW)
    called = sch.call_next(clin, servicio="CONSULTA", modulo="Consultorio 2", by=DRA, now=NOW)
    assert called["codigo"] == "C-003" and called["estado"] == "LLAMADO"
    assert sch.patient_tickets(clin, laura, NOW)[0]["antes"] == 1
    cita = clin.execute("SELECT cita_id FROM turnos_atencion WHERE codigo = 'C-001'").fetchone()[0]
    sch.set_outcome(clin, cita, DRA, True, NOW)
    assert clin.execute("SELECT estado FROM turnos_atencion WHERE codigo = 'C-001'").fetchone()[0] == "ATENDIDO"
    assert rosa and sch.board(clin, "2026-09-21", "FARMACIA")[0]["prioridad"] == 1


def test_check_in_only_on_the_day(clin):
    juan = _pid(clin, "1061900333")
    cita = sch.appointments(clin, day_from="2026-09-22", day_to="2026-09-22", id_paciente=juan)[0]["id"]
    with pytest.raises(sqlite3.IntegrityError, match="día de la cita"):
        sch.check_in(clin, cita, juan, NOW, only_patient=juan)


# --- Usuarios y turnos del personal ------------------------------------------------------
def test_admin_creates_user_with_temporary_password(clin):
    pwd = staff.create_user(clin, admin_id=ADMIN, usuario="enf.nueva", nombre="Enf. Nueva", rol="ENFERMERIA",
                            correo="rojas@hslv.demo", registro="RE-20-1111", now=NOW)
    res = auth.login(clin, "enf.nueva", pwd, NOW)
    assert res.user_id and clin.execute("SELECT debe_cambiar_clave FROM usuarios WHERE id = ?",
                                        (res.user_id,)).fetchone()[0] == 1
    auth.change_password(clin, res.user_id, pwd, "NuevaClave2026", NOW)
    assert auth.login(clin, "enf.nueva", "NuevaClave2026", NOW).user_id == res.user_id
    stored = clin.execute("SELECT hash_password FROM usuarios WHERE id = ?", (res.user_id,)).fetchone()[0]
    assert stored.startswith("pbkdf2$") and "NuevaClave2026" not in stored
    with pytest.raises(sqlite3.IntegrityError, match="registro profesional"):
        staff.create_user(clin, admin_id=ADMIN, usuario="dr.sin", nombre="X", rol="DOCTOR", correo=None, now=NOW)
    with pytest.raises(sqlite3.IntegrityError, match="ya existe"):
        staff.create_user(clin, admin_id=ADMIN, usuario="ENF.ROJAS", nombre="X", rol="FACTURACION", correo=None, now=NOW)


def test_suspended_user_cannot_log_in(clin):
    staff.set_status(clin, ADMIN, DR_PAREDES, "SUSPENDIDO", "Vacaciones", NOW)
    assert auth.login(clin, "dr.paredes", "demo", NOW).user_id is None
    with pytest.raises(sqlite3.IntegrityError, match="propia"):
        staff.set_status(clin, ADMIN, ADMIN, "SUSPENDIDO", "x", NOW)


def test_shift_assignment_blocks_overlaps_and_past(clin):
    n = staff.assign_shifts(clin, admin_id=ADMIN, user_id=3, servicio="UCI", template="Noche (19:00-07:00)",
                            days=["2026-11-02", "2026-11-03"], now=NOW)
    assert n == 2
    with pytest.raises(sqlite3.IntegrityError, match="ya tiene turno"):
        staff.assign_shifts(clin, admin_id=ADMIN, user_id=3, servicio="UCI",
                            template="Guardia localizable 24 h (07:00-07:00)", days=["2026-11-04", "2026-11-03"], now=NOW)
    assert len(staff.shifts(clin, "2026-11-04", "2026-11-04", 3)) == 0            # todo o nada
    n, skipped = staff.assign_shifts(clin, admin_id=ADMIN, user_id=3, servicio="UCI",
                                     template="Guardia localizable 24 h (07:00-07:00)",
                                     days=["2026-11-04", "2026-11-03"], now=NOW, skip_conflicts=True)
    assert (n, skipped) == (1, ["2026-11-03"])
    # otra persona sí puede cubrir la misma área y hora
    assert staff.assign_shifts(clin, admin_id=ADMIN, user_id=6, servicio="UCI", template="Noche (19:00-07:00)",
                               days=["2026-11-02"], now=NOW) == 1
    with pytest.raises(sqlite3.IntegrityError, match="ya pasó"):
        staff.assign_shifts(clin, admin_id=ADMIN, user_id=3, servicio="UCI", template="Mañana (07:00-13:00)",
                            days=["2026-09-01"], now=NOW)
    sid = staff.shifts(clin, "2026-11-02", "2026-11-02", 3)[0]["id"]
    staff.delete_shift(clin, ADMIN, sid, NOW)
    started = staff.shifts(clin, "2026-09-21", "2026-09-21", DRA)[0]["id"]
    with pytest.raises(sqlite3.IntegrityError, match="aún no han empezado"):
        staff.delete_shift(clin, ADMIN, started, NOW)


def test_assigned_shift_grants_clinical_access(clin):
    assert not ps.authorize(clin, DRA, "hc.ver_completa", 110, now="2026-11-05 21:00:00").allowed
    staff.assign_shifts(clin, admin_id=ADMIN, user_id=DRA, servicio="Urgencias", template="Noche (19:00-07:00)",
                        days=["2026-11-05"], now=NOW)
    assert ps.authorize(clin, DRA, "hc.ver_completa", 110, now="2026-11-05 21:00:00").allowed


# --- Recuperación de contraseña ------------------------------------------------------------
def test_recovery_code_flow(clin):
    uid, code, mail = auth.request_recovery(clin, "dra.ruiz@hslv.demo", NOW)
    assert uid == DRA and len(code) == 6 and mail == "dra.ruiz@hslv.demo"
    stored = clin.execute("SELECT codigo_hash FROM recuperacion_clave WHERE usuario_id = ?", (DRA,)).fetchone()[0]
    assert code not in stored
    with pytest.raises(sqlite3.IntegrityError, match="letras y números"):
        auth.reset_with_code(clin, "dra.ruiz", code, "solotexto", NOW)
    wrong = "000000" if code != "000000" else "111111"
    with pytest.raises(sqlite3.IntegrityError, match="no es válido"):
        auth.reset_with_code(clin, "dra.ruiz", wrong, "ClaveNueva123", NOW)
    auth.reset_with_code(clin, "dra.ruiz", code, "ClaveNueva123", "2026-09-21 10:05:00")
    assert auth.login(clin, "dra.ruiz", "ClaveNueva123", NOW).user_id == DRA
    with pytest.raises(sqlite3.IntegrityError):                                   # un solo uso
        auth.reset_with_code(clin, "dra.ruiz", code, "OtraClave123", NOW)


def test_recovery_code_expires_and_unknown_user_is_silent(clin):
    _, code, _ = auth.request_recovery(clin, "enf.gomez", NOW)
    with pytest.raises(sqlite3.IntegrityError, match="venció"):
        auth.reset_with_code(clin, "enf.gomez", code, "ClaveNueva123", "2026-09-21 10:16:00")
    assert auth.request_recovery(clin, "no.existe", NOW) == (None, None, None)


def test_recovery_rate_limit(clin):
    for i in range(auth.CODES_PER_HOUR):
        auth.request_recovery(clin, "admin", f"2026-09-21 10:0{i}:00")
    with pytest.raises(sqlite3.IntegrityError, match="varios códigos"):
        auth.request_recovery(clin, "admin", "2026-09-21 10:09:00")


def test_billing_role_permissions(clin):
    perms = ps.user_permissions(clin, ADMISIONES)
    assert {"citas.gestionar", "turnos_atencion.gestionar", "pacientes.registrar"} <= perms
    assert not perms & {"hc.ver_completa", "hc.ver_notas", "prescripcion.crear", "hc.buscar"}


def test_delete_user_only_without_history(clin):
    pwd = staff.create_user(clin, admin_id=ADMIN, usuario="error.creado", nombre="X", rol="FACTURACION",
                            correo=None, now=NOW)
    uid = clin.execute("SELECT id FROM usuarios WHERE usuario = 'error.creado'").fetchone()[0]
    staff.delete_user(clin, ADMIN, uid, NOW)
    assert clin.execute("SELECT 1 FROM usuarios WHERE id = ?", (uid,)).fetchone() is None
    with pytest.raises(sqlite3.IntegrityError, match="historial"):
        staff.delete_user(clin, ADMIN, DRA, NOW)                      # escribió historias y fórmulas
    staff.create_user(clin, admin_id=ADMIN, usuario="usada", nombre="Y", rol="FACTURACION", correo=None, now=NOW)
    used = clin.execute("SELECT id FROM usuarios WHERE usuario = 'usada'").fetchone()[0]
    auth.login(clin, "usada", "mala", NOW)                           # un intento de ingreso ya deja rastro
    with pytest.raises(sqlite3.IntegrityError, match="auditoria_accesos"):
        staff.delete_user(clin, ADMIN, used, NOW)
    with pytest.raises(sqlite3.IntegrityError, match="propia"):
        staff.delete_user(clin, ADMIN, ADMIN, NOW)
    assert pwd
