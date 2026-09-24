"""Solicitudes de cita (paciente → facturación), de registro (persona nueva → gerencia), horas y propuesta de
calendario quirúrgico, y camas para pacientes quirúrgicos."""
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import beds_service as bs  # noqa: E402
import config  # noqa: E402
import demo_seed as ds  # noqa: E402
import pharmacy_service as ps  # noqa: E402
import requests_service as rq  # noqa: E402
import scheduling as sch  # noqa: E402
import surgery_planner as sp  # noqa: E402
from ui import assistant_scope as scope  # noqa: E402
from ui import notifications as nt  # noqa: E402

NOW = "2026-09-21 10:00:00"
ADMIN, DRA, FACT, COORD = 1, 2, 7, 17


@pytest.fixture()
def clin(tmp_path):
    c = ps.init_clinical_db(config.DB_PATH, tmp_path / "clinico.db")
    ds.seed(c, config.DB_PATH)
    yield c
    c.close()


def _pid(clin, doc):
    return clin.execute("SELECT id_paciente FROM pacientes_clinicos WHERE numero_documento = ?", (doc,)).fetchone()[0]


# --- Solicitudes de cita --------------------------------------------------------------------
def test_demo_trae_solicitudes_pendientes_para_facturacion(clin):
    rows = rq.pending_requests(clin)
    assert len(rows) == 2 and {r["canal"] for r in rows} == {"PORTAL", "ASISTENTE"}
    notif = nt.collect("FACTURACION", {"id": FACT}, clin, None, NOW, [])
    assert notif and "solicitudes de cita" in notif[0].title


def test_paciente_pide_cita_y_facturacion_agenda_y_avisa(clin):
    rosa = _pid(clin, "25270444")
    with pytest.raises(sqlite3.IntegrityError, match="Cuéntanos"):
        rq.create_request(clin, id_paciente=rosa, tipo="MEDICINA_GENERAL", sintomas="mal", preferencia="MANANA",
                          telefono="3002223344", canal="PORTAL", now=NOW)
    rid = rq.create_request(clin, id_paciente=rosa, tipo="MEDICINA_GENERAL", sintomas="Tengo los pies muy hinchados",
                            preferencia="MANANA", telefono="300 222 3344", canal="PORTAL", now=NOW)
    with pytest.raises(sqlite3.IntegrityError, match="pendiente"):  # una sola pendiente por paciente
        rq.create_request(clin, id_paciente=rosa, tipo="CONTROL", sintomas="Otra cosa distinta aquí",
                          preferencia="TARDE", telefono=None, canal="PORTAL", now=NOW)
    slot = next(s for s in sch.free_slots(clin, especialidad="MEDICINA GENERAL", day="2026-09-22", now=NOW)
                if s["fecha_hora"][11:13] < "12")
    cita = rq.schedule_request(clin, rid, medico_id=slot["medico_id"], fecha_hora=slot["fecha_hora"],
                               motivo="PRIMERA_VEZ", by=FACT, now=NOW)
    row = clin.execute("SELECT * FROM solicitudes_cita WHERE id = ?", (rid,)).fetchone()
    assert row["estado"] == "AGENDADA" and row["cita_id"] == cita and row["atendida_por"] == FACT
    with pytest.raises(sqlite3.IntegrityError, match="ya fue atendida"):
        rq.close_request(clin, rid, respuesta="Respuesta tardía sin sentido", by=FACT, now=NOW)
    pending = [r for r in rq.pending_requests(clin)]
    appt = next(a for a in sch.appointments(clin, day_from="2026-09-22", day_to="2026-09-22", id_paciente=rosa)
                if a["id"] == cita)
    fake = {"nombres": "Rosa Elena", "tipo": "MEDICINA_GENERAL", "edad": 76}
    link = rq.whatsapp_link("300 222 3344", rq.whatsapp_message(fake, appt))
    assert link.startswith("https://wa.me/573002223344?text=") and "Rosa" in rq.whatsapp_message(fake, appt)
    assert len(pending) == 2
    juan = next(r for r in pending if r["canal"] == "PORTAL")
    assert rq.whatsapp_message(juan).startswith("Hola, le escribimos del Hospital Susana López de Valencia sobre Juan José")
    assert rq.suggested_specialty(juan, sch.specialties(clin)) == "PEDIATRIA"
    notif = nt.collect("PACIENTE", {"id": 0, "id_paciente": rosa}, clin, None, NOW, [])
    assert any(n.title == "Te agendaron la cita que pediste" for n in notif)


def test_responder_sin_agendar_y_retirar(clin):
    carlos = _pid(clin, "10542001")
    rid = rq.create_request(clin, id_paciente=carlos, tipo="RESULTADOS", sintomas="Quiero que revisen mis exámenes",
                            preferencia="CUALQUIERA", telefono=None, canal="PORTAL", now=NOW)
    with pytest.raises(sqlite3.IntegrityError, match="10 caracteres"):
        rq.close_request(clin, rid, respuesta="ok", by=FACT, now=NOW)
    rq.close_request(clin, rid, respuesta="Sus resultados se revisan en su control de hoy a las 10:20", by=FACT, now=NOW)
    maria = _pid(clin, "1061800222")
    rid2 = rq.create_request(clin, id_paciente=maria, tipo="CONTROL", sintomas="Control después del parto",
                             preferencia="TARDE", telefono=None, canal="PORTAL", now=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        rq.cancel_request(clin, rid2, carlos, NOW)          # no puede retirar la de otro paciente
    rq.cancel_request(clin, rid2, maria, NOW)
    with pytest.raises(sqlite3.IntegrityError):
        with clin:
            clin.execute("DELETE FROM solicitudes_cita")


def test_urgencias_y_whatsapp():
    assert rq.is_emergency("Tengo dolor en el pecho y me cuesta respirar")
    assert rq.is_emergency("siento FALTA DE AIRE")
    assert not rq.is_emergency("tengo tos y fiebre")
    assert rq.whatsapp_link(None, "hola") is None and rq.whatsapp_link("12345", "hola") is None
    assert rq.whatsapp_link("+57 310 456 7890", "a b").startswith("https://wa.me/573104567890?text=a%20b")


def test_el_bot_crea_la_solicitud_del_paciente(clin):
    rosa = _pid(clin, "25270444")
    pat = scope.SCOPES["paciente"]
    r = scope.answer("Quiero una cita en la tarde, tengo tos y fiebre desde hace 3 días", pat, clin=clin,
                     id_paciente=rosa, now=NOW)
    assert "envié tu solicitud a facturación" in r.answer and r.engine == "mis datos"
    row = clin.execute("SELECT * FROM solicitudes_cita WHERE id_paciente = ? AND estado = 'PENDIENTE'", (rosa,)).fetchone()
    assert row["canal"] == "ASISTENTE" and row["preferencia"] == "TARDE" and row["telefono"] == "3002223344"
    again = scope.answer("necesito una cita con el medico", pat, clin=clin, id_paciente=rosa, now=NOW)
    assert "le pasé este mensaje a facturación" in again.answer
    urgent = scope.answer("me duele mucho, tengo dolor en el pecho", pat, clin=clin, id_paciente=_pid(clin, "10542001"),
                          now=NOW)
    assert "123" in urgent.answer
    nxt = scope.answer("¿Cuándo es mi próxima cita?", pat, clin=clin, id_paciente=rosa, now=NOW)
    assert "próxima cita" in nxt.answer or "No tienes citas" in nxt.answer


# --- Solicitudes de registro ----------------------------------------------------------------
def test_registro_de_persona_nueva(clin):
    assert len(rq.pending_registrations(clin)) == 1          # caso sintético de la demo
    with pytest.raises(sqlite3.IntegrityError, match="correo"):
        rq.create_registration(clin, nombres="Pedro", apellidos="Gómez", tipo_documento="CC",
                               numero_documento="1061999888", correo="no-correo", telefono=None, now=NOW)
    rid = rq.create_registration(clin, nombres="Pedro", apellidos="Gómez", tipo_documento="CC",
                                 numero_documento="1061 999 888", correo="Pedro@Correo.demo", telefono="3001112233",
                                 now=NOW)
    with pytest.raises(sqlite3.IntegrityError, match="pendiente"):
        rq.create_registration(clin, nombres="Pedro", apellidos="Gómez", tipo_documento="CC",
                               numero_documento="1061999888", correo="pedro@correo.demo", telefono=None, now=NOW)
    assert rq.registration_status(clin, "1061999888", "otro@correo.demo") is None   # doc + correo deben coincidir
    with pytest.raises(sqlite3.IntegrityError, match="futuras"):
        rq.cite_registration(clin, rid, fecha_hora="2026-09-20 08:00:00", lugar="Admisiones", nota="", by=ADMIN, now=NOW)
    rq.cite_registration(clin, rid, fecha_hora="2026-09-22 08:00:00", lugar="Admisiones", nota="Traiga su carné",
                         by=ADMIN, now=NOW)
    row = rq.registration_status(clin, "1061999888", "pedro@correo.demo")
    assert row["estado"] == "CITADA" and "22/09 a las 08:00" in rq.registration_message(row)
    other = rq.pending_registrations(clin)[0]["id"]
    rq.reject_registration(clin, other, motivo="No es paciente de esta red de servicios", by=ADMIN, now=NOW)
    notif = nt.collect("ADMIN", {"id": ADMIN}, clin, ps_conn(), NOW, [])
    assert not any("registrarse" in n.title for n in notif)


def ps_conn():
    return sqlite3.connect(f"{Path(config.DB_PATH).resolve().as_uri()}?mode=ro", uri=True)


# --- Quirófanos: horas, propuesta de gerencia, realizada el mismo día, camas --------------------
def _prof():
    return sp.capacity_profile(ps_conn(), date(2026, 9, 21))


def test_plan_asigna_horas_distintas_por_area_y_dia(clin):
    prof = _prof()
    waiting = [dict(r) for r in sp.waitlist(clin)]
    plan = sp.plan(waiting, prof, date(2026, 9, 21), 14, {}, {}, NOW)
    fit = [p for p in plan if p["fecha"]]
    assert fit and all(sp.valid_hour(p["hora"]) for p in fit)
    keys = [(p["area"], p["fecha"], p["hora"]) for p in fit]
    assert len(keys) == len(set(keys))
    assert all(p["hora"] > "10:00" for p in fit if p["fecha"] == "2026-09-21")   # hoy: después de la hora actual


def test_hora_obligatoria_y_sin_choques(clin):
    prof = _prof()
    a = sp.request_surgery(clin, id_paciente=110, area="QUIROFANOS - CIRUGIA GENERAL", prioridad="URGENTE",
                           procedimiento="Apendicectomía", user_id=DRA, now=NOW)
    with pytest.raises(sqlite3.IntegrityError, match="hora"):
        sp.schedule(clin, a, "2026-09-21", COORD, NOW, prof, coordinator=True)
    with pytest.raises(sqlite3.IntegrityError, match="ya pasó"):
        sp.schedule(clin, a, "2026-09-21", COORD, NOW, prof, coordinator=True, hour="09:30",
                    justification="Sobrecupo por urgencia abdominal")
    sp.schedule(clin, a, "2026-09-21", COORD, NOW, prof, coordinator=True, hour="14:00",
                justification="Sobrecupo por urgencia abdominal")
    b = sp.request_surgery(clin, id_paciente=634732, area="QUIROFANOS - CIRUGIA GENERAL", prioridad="ELECTIVA",
                           procedimiento="Herniorrafia", user_id=DRA, now=NOW)
    with pytest.raises(sqlite3.IntegrityError, match="a las 14:00"):
        sp.schedule(clin, b, "2026-09-21", COORD, NOW, prof, coordinator=True, hour="14:00",
                    justification="Sobrecupo por urgencia abdominal")
    notif = nt.collect("QUIROFANOS", {"id": COORD}, clin, None, NOW, [])
    assert any(n.title == "Cirugía urgente hoy a las 14:00" for n in notif)
    mine = nt.collect("DOCTOR", {"id": DRA}, clin, ps_conn(), NOW, [])
    assert any("urgente hoy a las 14:00" in n.title for n in mine)
    # se puede cerrar el mismo día, no una futura
    sp.mark_done(clin, a, COORD, NOW, coordinator=True)
    assert clin.execute("SELECT estado FROM cirugias_solicitudes WHERE id = ?", (a,)).fetchone()[0] == "REALIZADA"


def test_gerencia_propone_y_coordinacion_acepta_o_rechaza(clin):
    prof = _prof()
    waiting = [dict(r) for r in sp.waitlist(clin)]
    fit = [p for p in sp.plan(waiting, prof, date(2026, 9, 21), 14, {}, {}, NOW) if p["fecha"]]
    with pytest.raises(sqlite3.IntegrityError, match="gerencia"):
        sp.propose(clin, fit, COORD, NOW, can_propose=False)
    first = sp.propose(clin, fit, ADMIN, NOW, can_propose=True)
    second = sp.propose(clin, fit[:3], ADMIN, NOW, can_propose=True)     # reemplaza la anterior
    assert clin.execute("SELECT estado FROM cirugias_propuestas WHERE id = ?", (first,)).fetchone()[0] == "REEMPLAZADA"
    head, items = sp.pending_proposal(clin)
    assert head["id"] == second and len(items) == 3
    notif = nt.collect("QUIROFANOS", {"id": COORD}, clin, None, NOW, [])
    assert any("propuso un calendario" in n.title for n in notif)
    with pytest.raises(sqlite3.IntegrityError, match="10 caracteres"):
        sp.reject_proposal(clin, second, COORD, NOW, "no", coordinator=True)
    with pytest.raises(sqlite3.IntegrityError, match="Solo coordinación"):
        sp.accept_proposal(clin, second, ADMIN, NOW, prof, coordinator=False)
    done, skipped = sp.accept_proposal(clin, second, COORD, NOW, prof, coordinator=True)
    assert done == 3 and not skipped
    hours = clin.execute("SELECT hora_programada FROM cirugias_solicitudes WHERE id IN (?,?,?)",
                         [i["solicitud_id"] for i in items]).fetchall()
    assert all(h[0] for h in hours)
    third = sp.propose(clin, [p for p in fit[3:6]], ADMIN, NOW, can_propose=True)
    sp.reject_proposal(clin, third, COORD, NOW, "Falta anestesiólogo el jueves", coordinator=True)
    admin = nt.collect("ADMIN", {"id": ADMIN}, clin, ps_conn(), NOW, [])
    assert any("rechazó tu propuesta" in n.title for n in admin)


def test_coordinacion_asigna_cama_solo_a_sus_pacientes(clin):
    prof = _prof()
    rid = sp.request_surgery(clin, id_paciente=110, area="QUIROFANOS - TRAUMATOLOGIA Y ORTOPEDIA",
                             prioridad="PRIORITARIA", procedimiento="Osteosíntesis de radio", user_id=DRA, now=NOW)
    with pytest.raises(sqlite3.IntegrityError, match="cirugía programada"):
        bs.occupy(clin, codigo_cama="H-101A", id_paciente=110, dias_estimados=2, usuario_id=COORD, cama_ocupada=False,
                  now=NOW, surgical_only=True)
    sp.schedule(clin, rid, "2026-09-22", COORD, NOW, prof, coordinator=True, hour="15:00",
                justification="Cupo acordado con ortopedia para mañana")
    assert 110 in {r["id_paciente"] for r in bs.surgical_patients(clin, NOW)}
    bs.occupy(clin, codigo_cama="H-101A", id_paciente=110, dias_estimados=2, usuario_id=COORD, cama_ocupada=False,
              now=NOW, surgical_only=True)
    perms = ps.user_permissions(clin, COORD)
    assert "camas.quirurgicas" in perms and "camas.gestionar" not in perms
    assert {"quirofanos.proponer", "registro.atender"} <= ps.user_permissions(clin, ADMIN)


def test_conversacion_paciente_facturacion(clin):
    pending = {r["canal"]: r for r in rq.pending_requests(clin)}
    sol = pending["ASISTENTE"]                                   # paciente 110
    with pytest.raises(sqlite3.IntegrityError, match="inexistente"):   # otro paciente no escribe en ella
        rq.send_message(clin, sol["id"], lado="PACIENTE", autor_id=None, texto="hola", now=NOW, id_paciente=999)
    rq.send_message(clin, sol["id"], lado="PACIENTE", autor_id=4, texto="¿Me pueden atender el jueves?", now=NOW,
                    id_paciente=110)
    fact = nt.collect("FACTURACION", {"id": FACT}, clin, None, NOW, [])
    assert any("mensaje nuevo de pacientes" in n.title for n in fact)
    rq.mark_read(clin, sol["id"], "FACTURACION")
    rq.send_message(clin, sol["id"], lado="FACTURACION", autor_id=FACT,
                    texto="Sí, le asignamos medicina general el jueves 8:00", now=NOW)
    assert clin.execute("SELECT contactado_en FROM solicitudes_cita WHERE id = ?", (sol["id"],)).fetchone()[0] == NOW
    pac = nt.collect("PACIENTE", {"id": 4, "id_paciente": 110}, clin, None, NOW, [])
    assert any(n.title == "Facturación te escribió" for n in pac)
    assert [m["lado"] for m in rq.messages(clin, sol["id"])] == ["PACIENTE", "FACTURACION"]
    assert rq.unread(clin, "PACIENTE", 110) == 1
    rq.mark_read(clin, sol["id"], "PACIENTE")
    assert rq.unread(clin, "PACIENTE", 110) == 0
    rq.cancel_request(clin, sol["id"], 110, NOW)
    with pytest.raises(sqlite3.IntegrityError, match="retirada"):
        rq.send_message(clin, sol["id"], lado="PACIENTE", autor_id=4, texto="hola", now=NOW, id_paciente=110)


# --- El asistente del paciente no da consejo médico ------------------------------------------------
def test_bot_no_receta_y_pasa_el_mensaje_a_facturacion(clin):
    pat = scope.SCOPES["paciente"]
    rosa = _pid(clin, "25270444")
    r = scope.answer("me duele la cabeza que medicamento me recomiendas", pat, clin=clin, id_paciente=rosa, now=NOW)
    assert r.answer.startswith("No soy un profesional de la salud") and "alergias" in r.answer
    assert r.engine == "orientación"
    row = clin.execute("SELECT * FROM solicitudes_cita WHERE id_paciente = ? AND estado = 'PENDIENTE'", (rosa,)).fetchone()
    assert row["canal"] == "ASISTENTE"
    # con la solicitud ya abierta, el siguiente mensaje llega a la conversación con facturación
    r2 = scope.answer("¿puedo tomar ibuprofeno?", pat, clin=clin, id_paciente=rosa, now=NOW)
    assert "le pasé este mensaje a facturación" in r2.answer
    assert [m["texto"] for m in rq.messages(clin, row["id"])] == ["¿puedo tomar ibuprofeno?"]
    for q in ("¿es grave tener fiebre de 39?", "que significa mi resultado de glicemia", "tengo diarrea, que pastilla me sirve"):
        assert scope.answer(q, pat, clin=clin, id_paciente=rosa, now=NOW).answer.startswith("No soy un profesional")
    own = scope.answer("¿Qué medicamentos tengo por reclamar?", pat, clin=clin, id_paciente=rosa, now=NOW)
    assert "No soy un profesional" not in own.answer


def test_bot_responde_la_dosis_solo_desde_la_formula_del_paciente(clin):
    pat = scope.SCOPES["paciente"]
    laura = _pid(clin, "1061700001")
    r = scope.answer("¿cada cuánto me tomo la claritromicina?", pat, clin=clin, id_paciente=laura, now=NOW)
    assert "Según la fórmula" in r.answer and "cada 12 horas" in r.answer
    other = scope.answer("¿cada cuánto me tomo el tramadol?", pat, clin=clin, id_paciente=laura, now=NOW)
    assert other.answer.startswith("No soy un profesional")


# --- Reporte del mes en curso --------------------------------------------------------------------
def test_reporte_desde_el_dia_1_del_mes():
    import month_report as mr
    import reports
    conn = ps_conn()
    assert mr.period(date(2026, 3, 31))[2:] == (date(2026, 2, 1), date(2026, 2, 28))
    mtd = mr.month_to_date(conn, date(2026, 9, 21))
    assert mtd["inicio"] == "2026-09-01" and mtd["dias"] == 21
    assert mtd["titular"].startswith("Del 1 al 21 de septiembre (21 días) se han atendido")
    personas = next(r for r in mtd["filas"] if r["clave"] == "personas")
    real = conn.execute("SELECT COUNT(DISTINCT id_paciente) FROM ingresos WHERE fecha_ingreso BETWEEN "
                        "'2026-09-01 00:00:00' AND '2026-09-21 23:59:59'").fetchone()[0]
    assert personas["valor"] == real
    assert "puntos" in next(r for r in mtd["filas"] if r["clave"] == "ocupacion")["variacion"]
    assert reports.month_to_date_xlsx(mtd)[:2] == b"PK"


def test_el_asistente_genera_el_reporte_del_mes():
    from agent import HospitalAgent
    agent = HospitalAgent(provider="none", mode="rules")
    full = scope.SCOPES["completo"]
    r = scope.answer("genera el reporte del mes", full, agent=agent)
    assert r.engine == "reporte del mes" and "se han atendido" in r.answer and r.files
    assert r.files[0][0].endswith(".xlsx") and r.data is not None
    k = scope.answer("¿Qué servicio tiene más pacientes ingresados este mes?", full, agent=agent)
    assert k.engine != "reporte del mes"
