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
    assert "Unidad De Cuidados Intensivos Adultos" in loc["ubicacion"]


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
    by_unit = beds.groupby("subgrupo_cama")["ocupada"].sum()
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
