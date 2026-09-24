"""Pruebas de la operación del día: ubicación de camas, mapa y cola de urgencias.
Ejecutar: python -m pytest -q   (requiere haber corrido database.py)"""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database as db  # noqa: E402

REF = date(2026, 9, 21)


@pytest.fixture(scope="module")
def conn():
    c = db.get_connection(read_only=True)
    yield c
    c.close()


# --- Ubicación desde el código de cama ------------------------------------------
@pytest.mark.parametrize("code, floor, room, bed", [
    ("H-203C", 2, "203", "C"), ("H-117B", 1, "117", "B"), ("G-108B", 1, "108", "B"),
    ("H-415C", 4, "415", "C"), ("H-121", 1, "121", "única"),
])
def test_bed_location_reads_floor_room_and_bed(code, floor, room, bed):
    loc = db.bed_location(code, "HOSPITALIZACION 2")
    assert (loc["piso"], loc["habitacion"], loc["cama"]) == (floor, room, bed)
    assert loc["ubicacion"].startswith(f"Piso {floor} · Hab. {room}")


@pytest.mark.parametrize("code", ["UCIA3", "VOA86", "CUITA4", "OA27", "", None])
def test_bed_location_without_room_pattern_uses_unit(code):
    loc = db.bed_location(code, "UNIDAD DE CUIDADOS INTENSIVOS ADULTOS")
    assert loc["piso"] is None and loc["habitacion"] is None
    assert "Unidad de Cuidados Intensivos Adultos" in loc["ubicacion"]


def test_unit_label_is_readable():
    assert db.unit_label("UNIDAD DE CUIDAD BASICO NEONATAL") == "Unidad de Cuidado Básico Neonatal"
    assert db.unit_label("HOSPITALIZACION 2") == "Hospitalización 2"


def test_occupancy_uses_physical_beds_everywhere(conn):
    """Tarjeta de Indicadores, agente y alertas usan la misma capacidad física que Hoy y el mapa."""
    occ = db.kpi_global_occupancy(conn, REF)
    beds = db.bed_map(conn, REF)
    phys = beds[(beds["es_virtual"] == 0) & (beds["servicio"] != "Urgencias")]
    assert (occ["capacidad"], occ["ocupadas"]) == (len(phys), int(phys["ocupada"].sum()))
    h3 = db.kpi_bed_occupancy(conn, REF, by="subgrupo_cama").query("subgrupo_cama == 'HOSPITALIZACION 3'")
    assert h3["capacidad"].iat[0] == 43, "las 95 camas virtuales de Hospitalización 3 no son capacidad"


def test_population_keeps_units_apart():
    assert db.bed_population("UNIDAD DE CUIDAD BASICO NEONATAL") == "Neonatal"
    assert db.bed_population("UNIDAD DE CUIDADO INTENSIVO PEDIATRICO") == "Pediátrica"
    assert db.bed_population("GINECO OBSTETRICIA") == "Materna"
    assert db.bed_population("UNIDAD DE CUIDADOS INTENSIVOS ADULTOS") == "Crítica adultos"
    assert db.bed_population("RECUPERACION ADULTOS") == "Recuperación quirúrgica"
    assert db.bed_population("HOSPITALIZACION 1") == "Adultos"


# --- Mapa de camas ----------------------------------------------------------------
def test_bed_map_matches_daily_occupancy(conn):
    """El mapa y el tablero no pueden contradecirse: misma regla de ocupación por unidad."""
    beds = db.bed_map(conn, REF)
    assert len(beds) == conn.execute("SELECT COUNT(*) FROM camas").fetchone()[0]
    by_unit = beds[beds["es_virtual"] == 0].groupby("subgrupo_cama")["ocupada"].sum()  # ocupación = camas físicas
    for sub, occupied in conn.execute(
            "SELECT subgrupo_cama, camas_ocupadas FROM ocupacion_diaria WHERE fecha = ?", (REF.isoformat(),)):
        assert by_unit.get(sub, 0) == occupied, sub


def test_free_beds_are_physical_inpatient_and_free(conn):
    free = db.free_beds(conn, REF, limit=500)
    assert not free.empty
    assert (free["ocupada"] == 0).all() and (free["es_virtual"] == 0).all()
    assert (free["servicio"] != "Urgencias").all()
    located = free["piso"].notna().tolist()
    assert located == sorted(located, reverse=True), "las camas con piso y habitación van primero"


# --- Cola de urgencias -------------------------------------------------------------
def test_triage_queue_only_waiting_patients_and_no_identifiers(conn):
    q = db.triage_queue(conn, "2026-09-21 10:00:00")
    assert not q.empty
    assert (q["espera_min"] >= 0).all()
    assert {"id_paciente", "oid_ingreso", "nombre_paciente"}.isdisjoint(q.columns)
    assert q["codigo"].str.fullmatch(r"URG-[0-9A-F]{4}").all()
    levels = q["nivel_triage"].dropna().tolist()
    assert levels == sorted(levels), "ordenada por nivel de triage"


def test_triage_queue_flags_level_two_over_target(conn):
    q = db.triage_queue(conn, "2026-09-21 09:00:00")
    t2 = q[q["nivel_triage"] == 2]
    assert not t2.empty
    assert (t2["fuera_de_meta"] == (t2["espera_min"] > t2["meta_min"]).astype(int)).all()


def test_triage_queue_empty_after_extract_ends(conn):
    assert db.triage_queue(conn, "2026-09-21 23:00:00", lookback_hours=6).empty


# --- Enrutamiento del chat: ubicar camas vs. preguntas al agente -------------------
@pytest.mark.parametrize("question, is_bed_lookup", [
    ("¿Dónde hay camas libres para adultos?", True),
    ("hay cama disponible en pediatria", True),
    ("ubícame una cama para un recién nacido", True),
    ("camas libres piso 2", True),
    ("¿Cuántas camas de UCI están ocupadas hoy?", False),
    ("Ocupación de camas por servicio", False),
    ("¿Qué servicio tiene más pacientes ingresados este mes?", False),
])
def test_chat_routes_bed_lookups_to_bed_map(question, is_bed_lookup):
    from ui.chat_bubble import _BED_Q, _norm
    q = _norm(question)
    assert bool(_BED_Q.search(q) and "ocupad" not in q) == is_bed_lookup


# --- Notificaciones por rol -----------------------------------------------------------
@pytest.fixture()
def clin(tmp_path):
    import config
    import demo_seed as ds
    import pharmacy_service as ps
    c = ps.init_clinical_db(config.DB_PATH, tmp_path / "clinico.db")
    ds.seed(c, config.DB_PATH)
    yield c
    c.close()


def _user(clin, uid):
    return dict(clin.execute("SELECT u.*, r.codigo AS rol FROM usuarios u JOIN roles r ON r.id = u.rol_id "
                             "WHERE u.id = ?", (uid,)).fetchone())


def _notes(clin, conn, uid, now, alerts=()):
    from ui.notifications import collect
    u = _user(clin, uid)
    return collect(u["rol"], u, clin, conn, now, list(alerts))


def test_patient_sees_only_own_prescription_and_no_hospital_alerts(clin, conn):
    from agent import Alert
    alert = Alert("crítica", "Ocupación", "Hospitalizacion 2 al 100 %", "x", "y")
    notes = _notes(clin, conn, 4, "2026-09-21 10:00:00", [alert])
    assert [n.title for n in notes] == ["Reclama tu medicamento"]
    assert all(n.slug == "portal" for n in notes)


def test_expiry_flow_notifies_nurse_then_patient_and_doctor(clin, conn):
    import pharmacy_service as ps
    later = "2026-10-22 10:00:00"   # 30 días de apartado + 1 día
    nurse = _notes(clin, conn, 3, later)
    assert any("vencida" in n.title for n in nurse), "enfermería ve las fórmulas vencidas sin procesar"
    ps.expire_prescriptions(clin, later)
    patient = _notes(clin, conn, 4, later)
    assert any(n.title == "Tu fórmula venció" for n in patient)
    doctor = _notes(clin, conn, 2, later)
    crit = [n for n in doctor if n.id.startswith("caducada:") and n.severity == "crítica"]
    assert crit and "continuidad crítica" in crit[0].detail


def test_roles_receive_only_their_categories(clin, conn):
    from agent import Alert
    alerts = [Alert("crítica", "Farmacia", "64 ítems", "d", "a"), Alert("alta", "Demanda", "Pico", "d", "a"),
              Alert("crítica", "Ocupación", "Hosp 2", "d", "a")]
    doctor = {n.title for n in _notes(clin, conn, 2, "2026-09-21 10:00:00", alerts)}
    nurse = {n.title for n in _notes(clin, conn, 3, "2026-09-21 10:00:00", alerts)}
    admin = {n.title for n in _notes(clin, conn, 1, "2026-09-21 10:00:00", alerts)}
    pharmacy = next(t for t in admin if "se agotan" in t)       # farmacia sale del inventario vivo
    assert "Hosp 2" in doctor and pharmacy not in doctor and "Pico" not in doctor
    assert {"Hosp 2", pharmacy} <= nurse and "Pico" not in nurse
    assert {"Hosp 2", pharmacy, "Pico"} <= admin and "64 ítems" not in admin


def test_receiving_orders_updates_pharmacy_alert(clin, conn):
    import pharmacy_service as ps
    from ui.notifications import _pharmacy_live
    before = clin.execute("SELECT COUNT(*) FROM v_semaforo_stock WHERE semaforo = 'ROJO'").fetchone()[0]
    assert before > 0 and str(before) in _pharmacy_live(clin).title
    for r in ps.stock_semaphore(clin, ("ROJO",)):
        ps.receive_stock(clin, r["codigo"], max(int(r["orden_sugerida_15d"]), 1), 1, "prueba", "2026-09-21 11:00:00")
    assert _pharmacy_live(clin) is None, "tras recibir la orden ya no quedan urgentes"


def test_inventory_adjustment_goes_through_ledger(clin):
    import sqlite3

    import pharmacy_service as ps
    code = ps.inventory(clin)[0]["codigo"]
    before = clin.execute("SELECT disponible FROM v_stock WHERE codigo = ?", (code,)).fetchone()[0]
    delta = ps.adjust_stock(clin, code, before + 7, 1, "Conteo semanal", "2026-09-21 12:00:00")
    assert delta == 7
    assert clin.execute("SELECT disponible FROM v_stock WHERE codigo = ?", (code,)).fetchone()[0] == before + 7
    with pytest.raises(sqlite3.IntegrityError):
        ps.adjust_stock(clin, code, 3, 1, "", "2026-09-21 12:00:00")   # sin motivo no hay ajuste
    with pytest.raises(sqlite3.IntegrityError):
        clin.execute("UPDATE inventario_movimientos SET delta_disponible = 0")  # el libro mayor es inmutable


# --- Glosario en lenguaje sencillo ------------------------------------------------------
@pytest.mark.parametrize("question, expected", [
    ("¿Qué es triage?", "Triage"), ("que significa CIE-10", "CIE-10"), ("¿Qué es una EPS?", "EPS"),
    ("¿qué son las camas virtuales?", "Cama de expansión"), ("¿Qué es triage 2?", "Triage II"),
])
def test_glossary_answers_definitions(question, expected):
    from ui.glossary import answer
    out = answer(question)
    assert out and out.startswith(f"**{expected}:**")


@pytest.mark.parametrize("question", ["¿Cuántas camas de UCI están ocupadas hoy?", "¿Qué es lo más urgente hoy?",
                                      "¿Qué servicio tiene más pacientes?"])
def test_glossary_ignores_data_questions(question):
    from ui.glossary import answer
    assert answer(question) is None


# --- Alcance del asistente por rol ----------------------------------------------------------
@pytest.fixture(scope="module")
def rules_agent():
    from agent import HospitalAgent
    return HospitalAgent(mode="rules")


def _scope(perms):
    from ui.assistant_scope import scope_for
    return scope_for(set(perms))


def test_scope_follows_permissions():
    assert _scope({"agente.consultar", "camas.ver"}).code == "completo"
    assert _scope({"camas.ver", "farmacia.alertas.ver", "hc.ver_notas"}).code == "operativo"
    assert _scope({"portal.propio"}).code == "paciente"
    assert _scope(set()).code == "ninguno"


@pytest.mark.parametrize("question, allowed", [
    ("¿Qué medicamentos tienen menos de 5 días de inventario?", True),
    ("¿Cuál es el tiempo de espera en urgencias esta semana?", True),
    ("¿Cuántas camas de UCI están ocupadas hoy?", True),
    ("¿Qué servicio tiene más pacientes ingresados este mes?", False),
    ("¿Cuáles son los diagnósticos más frecuentes?", False),
    ("¿Cuántos pacientes por régimen hay?", False),
])
def test_nurse_scope_only_operational_questions(rules_agent, question, allowed):
    from ui.assistant_scope import answer
    resp = answer(question, _scope({"camas.ver"}), agent=rules_agent)
    assert (resp.engine != "fuera de alcance") == allowed
    assert resp.sql is None, "enfermería no ve el SQL"


@pytest.mark.parametrize("question", ["SELECT * FROM pacientes", "borra la tabla de ingresos"])
def test_nurse_scope_blocks_sql_and_writes(rules_agent, question):
    from ui.assistant_scope import answer
    assert answer(question, _scope({"camas.ver"}), agent=rules_agent).engine == "seguridad"


def test_patient_scope_never_touches_hospital_data(clin):
    from ui.assistant_scope import answer

    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError("el paciente no puede llegar al agente de la base del hospital")

    sc = _scope({"portal.propio"})
    kw = dict(agent=Forbidden(), bed_answer=lambda q: pytest.fail("ni al mapa de camas"), clin=clin,
              id_paciente=110, now="2026-09-21 10:00:00")
    mine = answer("¿Qué medicamentos tengo por reclamar?", sc, **kw)
    assert mine.engine == "mis datos" and "Acetaminofen" in mine.answer
    assert "05/10" in answer("¿Cuándo es mi próxima cita?", sc, **kw).answer
    assert answer("¿Cuántas camas de UCI están ocupadas hoy?", sc, **kw).engine == "fuera de alcance"
    assert answer("SELECT * FROM pacientes", sc, **kw).engine == "fuera de alcance"
    assert answer("¿Qué es una EPS?", sc, **kw).engine == "glosario"


def test_physical_trend_matches_bed_map(conn):
    """La serie de Indicadores y la foto de Hoy/Camas usan la misma regla."""
    trend = db.physical_occupancy_trend(conn, date(2026, 9, 14), REF)
    beds = db.bed_map(conn, REF)
    phys = beds[(beds["es_virtual"] == 0) & (beds["servicio"] != "Urgencias")]
    last = trend[trend["fecha"] == REF.isoformat()]
    assert last["camas_ocupadas"].sum() == phys["ocupada"].sum()
    assert last["capacidad"].sum() == len(phys)
    assert trend["fecha"].nunique() == 8
