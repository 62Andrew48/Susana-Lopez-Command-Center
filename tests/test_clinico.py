"""Pruebas del módulo clínico (clinico.db). Ejecutar: python -m pytest -q tests/test_clinico.py"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402
import pharmacy_service as ps  # noqa: E402

T0 = "2026-09-21 08:00:00"


@pytest.fixture()
def db(tmp_path):
    conn = ps.init_clinical_db(config.DB_PATH, tmp_path / "clinico.db")
    with conn:
        conn.executescript("""
            INSERT INTO usuarios(id, usuario, hash_password, nombre_mostrado, rol_id, estado_cuenta, registro_profesional)
            VALUES (1,'admin','x','Admin',1,'ACTIVO',NULL), (2,'dra.ruiz','x','Dra. Ruiz',2,'ACTIVO','RM-1'),
                   (3,'enf.gomez','x','Enf. Gómez',3,'ACTIVO','RE-9'), (5,'dr.susp','x','Dr. S',2,'SUSPENDIDO','RM-2');
            INSERT INTO usuarios(id, usuario, hash_password, nombre_mostrado, rol_id, estado_cuenta, id_paciente)
            VALUES (4,'pac.110','x','Paciente 110',4,'ACTIVO',110);
            INSERT INTO turnos(usuario_id, servicio, tipo, inicio, fin)
            VALUES (2,'Urgencias','TURNO','2026-09-21 07:00:00','2026-09-21 19:00:00'),
                   (3,'Urgencias','TURNO','2026-09-21 07:00:00','2026-09-21 19:00:00');
            INSERT INTO historias_clinicas(id, id_paciente) VALUES (1, 110);
            -- producto de prueba con saldo conocido
            INSERT INTO productos_farmacia VALUES ('TEST001','Amoxicilina 500 mg','Medicamento',10,0);
            INSERT INTO productos_farmacia VALUES ('A10AB01X','Insulina rápida','Medicamento',2,1);
            INSERT INTO inventario_movimientos(codigo_producto,tipo,delta_disponible,delta_reservado)
            VALUES ('TEST001','SALDO_INICIAL',100,0), ('A10AB01X','SALDO_INICIAL',20,0);
        """)
    return conn


def stock(conn, code):
    r = conn.execute("SELECT disponible, reservado FROM v_stock WHERE codigo = ?", (code,)).fetchone()
    return r["disponible"], r["reservado"]


def prescribe(conn, code="TEST001", units=21, hours=72, when=T0):
    with conn:
        cur = conn.execute(
            "INSERT INTO prescripciones(historia_id,id_paciente,medico_id,codigo_producto,dosis,frecuencia_horas,"
            "duracion_dias,dosis_prescritas,ambito,fecha_prescripcion,horas_ventana_reclamo) "
            "VALUES (1,110,2,?,'500 mg VO',8,7,?,'AMBULATORIA',?,?)", (code, units, when, hours))
    return cur.lastrowid


def test_catalog_synced_from_real_analytics(db):
    n = db.execute("SELECT COUNT(*) FROM productos_farmacia").fetchone()[0]
    assert n > 1000  # 1.327 ítems reales + 2 de prueba


def test_full_cycle_reserve_partial_expire_return(db):
    pid = prescribe(db)
    assert stock(db, "TEST001") == (79, 21)                       # reserva
    with db:
        db.execute("INSERT INTO dispensaciones(prescripcion_id,usuario_id,cantidad,fecha) "
                   "VALUES (?,3,6,'2026-09-22 10:00:00')", (pid,))
    assert stock(db, "TEST001") == (79, 15)                       # entrega parcial sale de lo reservado
    assert ps.expire_prescriptions(db, "2026-09-24 07:59:00") == []   # aún dentro de las 72 h
    assert ps.expire_prescriptions(db, "2026-09-24 08:01:00") == [pid]
    assert stock(db, "TEST001") == (94, 0)                        # 15 unidades vuelven a disponible
    p = db.execute("SELECT estado, requiere_reevaluacion FROM prescripciones WHERE id=?", (pid,)).fetchone()
    assert (p["estado"], p["requiere_reevaluacion"]) == ("CADUCADA", 1)
    ev = db.execute("SELECT descripcion FROM historia_clinica_eventos WHERE tipo='FORMULA_CADUCADA'").fetchone()
    assert ev["descripcion"].startswith("Fórmula caducada - Medicamentos no reclamados en el periodo permitido")


def test_expiry_job_is_idempotent(db):
    prescribe(db)
    ps.expire_prescriptions(db, "2026-09-30 00:00:00")
    ps.expire_prescriptions(db, "2026-09-30 00:00:00")
    assert stock(db, "TEST001") == (100, 0)
    n = db.execute("SELECT COUNT(*) FROM inventario_movimientos WHERE tipo='LIBERACION_RESERVA'").fetchone()[0]
    assert n == 1


def test_expired_formula_blocks_dispensing_and_new_prescription_until_reevaluation(db):
    pid = prescribe(db)
    ps.expire_prescriptions(db, "2026-09-30 00:00:00")
    with pytest.raises(sqlite3.IntegrityError, match="no está vigente"):
        with db:
            db.execute("INSERT INTO dispensaciones(prescripcion_id,usuario_id,cantidad) VALUES (?,3,1)", (pid,))
    with pytest.raises(sqlite3.IntegrityError, match="requiere cita de reevaluación"):
        prescribe(db, when="2026-09-30 09:00:00")
    with db:
        db.execute("INSERT INTO citas(id,id_paciente,medico_id,especialidad,fecha_hora,motivo,prescripcion_origen_id) "
                   "VALUES (1,110,2,'MEDICINA GENERAL','2026-10-01 09:00:00','REEVALUACION_FORMULA',?)", (pid,))
        db.execute("UPDATE citas SET estado='CUMPLIDA' WHERE id=1")
    assert prescribe(db, when="2026-10-01 09:30:00")                # desbloqueado tras la reevaluación


def test_continuity_critical_drug_is_not_blocked_but_alerts(db):
    prescribe(db, code="A10AB01X", units=4)
    ps.expire_prescriptions(db, "2026-09-30 00:00:00")
    assert prescribe(db, code="A10AB01X", units=4, when="2026-09-30 09:00:00")
    assert db.execute("SELECT COUNT(*) FROM historia_clinica_eventos WHERE tipo='ALERTA'").fetchone()[0] == 1


def test_cannot_overdispense_or_dispense_after_window(db):
    pid = prescribe(db, units=5)
    with pytest.raises(sqlite3.IntegrityError, match="supera"):
        with db:
            db.execute("INSERT INTO dispensaciones(prescripcion_id,usuario_id,cantidad,fecha) "
                       "VALUES (?,3,6,'2026-09-21 10:00:00')", (pid,))  # fecha fija: no depende del reloj real
    with pytest.raises(sqlite3.IntegrityError, match="ventana"):
        with db:
            db.execute("INSERT INTO dispensaciones(prescripcion_id,usuario_id,cantidad,fecha) "
                       "VALUES (?,3,1,'2026-09-25 00:00:00')", (pid,))


def test_insufficient_stock_rejects_reservation(db):
    with pytest.raises(sqlite3.IntegrityError, match="insuficiente"):
        prescribe(db, units=500)


def test_ledger_and_audit_are_immutable(db):
    with pytest.raises(sqlite3.IntegrityError):
        with db:
            db.execute("UPDATE inventario_movimientos SET delta_disponible = 999")
    ps.authorize(db, 2, "hc.ver_completa", 110, now="2026-09-21 10:00:00")
    with pytest.raises(sqlite3.IntegrityError):
        with db:
            db.execute("DELETE FROM auditoria_accesos")


@pytest.mark.parametrize("user,perm,paciente,when,justif,allowed", [
    (2, "hc.ver_completa", 110, "2026-09-21 10:00:00", None, True),        # médica en turno
    (2, "hc.ver_completa", 110, "2026-09-21 23:00:00", None, False),       # fuera de turno
    (2, "hc.ver_completa", 110, "2026-09-21 23:00:00", "Paro en urgencias", True),  # romper el vidrio
    (5, "hc.ver_completa", 110, "2026-09-21 10:00:00", None, False),       # cuenta suspendida
    (1, "hc.ver_completa", 110, "2026-09-21 10:00:00", None, False),       # admin no lee HC
    (1, "auditoria.ver", None, "2026-09-21 10:00:00", None, True),
    (3, "prescripcion.crear", 110, "2026-09-21 10:00:00", None, False),    # enfermería no formula
    (3, "dispensacion.registrar", 110, "2026-09-21 10:00:00", None, True),
    (4, "portal.propio", 110, "2026-09-21 10:00:00", None, True),          # paciente, lo suyo
    (4, "portal.propio", 405, "2026-09-21 10:00:00", None, False),         # paciente, ajeno
    (4, "farmacia.alertas.ver", None, "2026-09-21 10:00:00", None, False), # paciente sin alertas
    (4, "agente.consultar", None, "2026-09-21 10:00:00", None, False),     # paciente sin NL2SQL
])
def test_rbac_matrix(db, user, perm, paciente, when, justif, allowed):
    assert ps.authorize(db, user, perm, paciente, justif, when).allowed is allowed


def test_emergency_access_is_audited(db):
    ps.authorize(db, 2, "hc.ver_completa", 110, "Paro en urgencias", "2026-09-21 23:00:00")
    row = db.execute("SELECT acceso_emergencia, justificacion FROM auditoria_accesos").fetchone()
    assert (row["acceso_emergencia"], row["justificacion"]) == (1, "Paro en urgencias")


def test_patient_user_requires_link(db):
    with pytest.raises(sqlite3.IntegrityError, match="vinculado"):
        with db:
            db.execute("INSERT INTO usuarios(usuario,hash_password,nombre_mostrado,rol_id) VALUES ('p','x','P',4)")


def test_semaphore_and_purchase_order(db):
    rows = ps.stock_semaphore(db)
    assert rows and {r["semaforo"] for r in rows} <= {"ROJO", "AMARILLO"}
    csv_bytes = ps.purchase_order_csv(db)
    assert csv_bytes.decode("utf-8-sig").splitlines()[0].startswith("codigo;nombre")


def test_cancelled_prescription_releases_reservation_without_block(db):
    pid = prescribe(db)
    with db:
        db.execute("UPDATE prescripciones SET estado='ANULADA' WHERE id=?", (pid,))
    assert stock(db, "TEST001") == (100, 0)
    assert prescribe(db, when="2026-09-21 09:00:00")          # sin bloqueo de reevaluación
    assert ps.expire_prescriptions(db, "2026-10-30 00:00:00")  # la nueva sí caduca normalmente


def test_ui_service_flow_request_and_complete_reevaluation(db):
    pid = prescribe(db)
    ps.expire_prescriptions(db, "2026-09-30 00:00:00")
    cita = ps.request_reevaluation(db, 110, pid, "2026-10-01 08:00:00", now="2026-09-30 09:00:00")
    with pytest.raises(sqlite3.IntegrityError, match="Ya existe"):
        ps.request_reevaluation(db, 110, pid, "2026-10-02 08:00:00", now="2026-09-30 09:05:00")
    with pytest.raises(sqlite3.IntegrityError, match="propia"):
        ps.request_reevaluation(db, 405, pid, "2026-10-02 08:00:00")          # no es su fórmula
    ev = db.execute("SELECT fecha FROM historia_clinica_eventos WHERE descripcion LIKE 'Paciente solicita%'").fetchone()
    assert ev["fecha"] == "2026-09-30 09:00:00"                               # fechada al pedirla, no a la cita
    assert ps.product_status(db, "TEST001", 110)["bloqueado_reevaluacion"]
    ps.complete_appointment(db, cita, 2, "2026-10-01 08:20:00")
    assert not ps.product_status(db, "TEST001", 110)["bloqueado_reevaluacion"]
    assert ps.patient_appointments(db, 110)[0]["producto_origen"] == "Amoxicilina 500 mg"


def test_prescribe_helper_and_queue(db):
    pid = ps.prescribe(db, medico_id=2, id_paciente=110, codigo="TEST001", dosis="500 mg VO", frecuencia_horas=8,
                       duracion_dias=7, dosis_prescritas=21, ambito="AMBULATORIA", horas_ventana=72, now=T0)
    assert any(r["id"] == pid for r in ps.dispensing_queue(db))
    ps.dispense(db, pid, 3, 21, "2026-09-21 12:00:00")
    assert not any(r["id"] == pid for r in ps.dispensing_queue(db))
    with pytest.raises(sqlite3.IntegrityError, match="historia clínica"):
        ps.prescribe(db, medico_id=2, id_paciente=999999, codigo="TEST001", dosis="x", frecuencia_horas=8,
                     duracion_dias=1, dosis_prescritas=1, ambito="AMBULATORIA", horas_ventana=72, now=T0)
