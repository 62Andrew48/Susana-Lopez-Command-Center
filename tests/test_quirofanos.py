"""Quirófanos: área por CUPS, capacidad probada, lista de espera y optimizador."""
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402
import database as db  # noqa: E402
import demo_seed as ds  # noqa: E402
import pharmacy_service as ps  # noqa: E402
import surgery_planner as sp  # noqa: E402

NOW = "2026-09-21 10:00:00"
GEN, ORTO = "QUIROFANOS - CIRUGIA GENERAL", "QUIROFANOS - TRAUMATOLOGIA Y ORTOPEDIA"


@pytest.fixture()
def clin(tmp_path):
    c = ps.init_clinical_db(config.DB_PATH, tmp_path / "clinico.db")
    ds.seed(c, config.DB_PATH)
    yield c
    c.close()


@pytest.mark.parametrize("codes, service, area", [
    ("793501,861104", "Hospitalización", ORTO), ("470100", "Hospitalización", GEN),
    ("740100", "Hospitalización", "QUIROFANOS - CIRUGIA GINECOBSTETRICA"), ("552603", None, "QUIROFANOS - UROLOGIA"),
    ("470100", "Pediatría", "QUIROFANOS PEDIATRIA UMI - CIRUGIA PEDIATRICA"), (None, None, GEN)])
def test_area_from_cups_chapter(codes, service, area):
    assert sp.area_from_cups(codes, service) == area


def test_capacity_profile_is_grounded_in_history():
    conn = db.get_connection(read_only=True)
    prof = sp.capacity_profile(conn, date(2026, 9, 21))
    gen = prof[prof["area"] == GEN]
    assert len(gen) == 7 and (gen["capacidad"] >= gen["habitual"]).all()
    assert (prof["libres"] >= 0).all()
    assert sp.compliance(conn)["cumplimiento_pct"] == 97.0


def test_waitlist_synced_from_his_and_idempotent(clin):
    n = len(sp.waitlist(clin))
    assert n == 44                      # 40 del HIS sin ejecutar + 1 urgencia y 3 solicitudes de la demo
    assert sp.sync_waitlist(clin, config.DB_PATH) == 0


def _profile(free_gen=1, free_orto=1):
    rows = [{"area": GEN, "dow": d, "habitual": 4, "capacidad": 4 + free_gen, "libres": free_gen} for d in range(7)]
    rows += [{"area": ORTO, "dow": d, "habitual": 4, "capacidad": 4 + free_orto, "libres": free_orto} for d in range(7)]
    return pd.DataFrame(rows)


def test_optimizer_priorities_and_capacity():
    reqs = [{"id": 1, "area_quirofano": GEN, "prioridad": "ELECTIVA", "fecha_solicitud": "2026-05-01"},
            {"id": 2, "area_quirofano": GEN, "prioridad": "ELECTIVA", "fecha_solicitud": "2026-06-01"},
            {"id": 3, "area_quirofano": GEN, "prioridad": "PRIORITARIA", "fecha_solicitud": "2026-09-20"},
            {"id": 4, "area_quirofano": ORTO, "prioridad": "URGENTE", "fecha_solicitud": "2026-09-21"},
            {"id": 5, "area_quirofano": ORTO, "prioridad": "URGENTE", "fecha_solicitud": "2026-09-21"}]
    out = {p["id"]: p for p in sp.plan(reqs, _profile(), date(2026, 9, 22), 14)}
    assert out[3]["fecha"] == "2026-09-22"                          # la prioritaria antes que las electivas
    assert out[1]["fecha"] == "2026-09-23" and out[2]["fecha"] == "2026-09-24"   # electivas por antigüedad
    assert out[4]["fecha"] == "2026-09-22" and out[4]["alerta"] is None
    assert out[5]["fecha"] == "2026-09-22" and "Sobrecupo" in out[5]["alerta"]  # la 2.a urgencia no cabe


def test_optimizer_respects_already_scheduled():
    reqs = [{"id": 1, "area_quirofano": GEN, "prioridad": "ELECTIVA", "fecha_solicitud": "2026-05-01"}]
    out = sp.plan(reqs, _profile(), date(2026, 9, 22), 14, already={(GEN, "2026-09-22"): 1})
    assert out[0]["fecha"] == "2026-09-23"


COORD, DRA, DR_PAREDES = 17, 2, 5


def _prof():
    return sp.capacity_profile(db.get_connection(read_only=True), date(2026, 9, 21))


def test_confirm_only_coordinator_and_close(clin):
    waiting = [dict(r) for r in sp.waitlist(clin)]
    proposal = sp.plan(waiting, _prof(), date(2026, 9, 22), 14)
    with pytest.raises(sqlite3.IntegrityError, match="coordinación"):
        sp.confirm(clin, proposal, DRA, NOW, coordinator=False)
    n = sp.confirm(clin, [p for p in proposal if p["fecha"]], COORD, NOW)
    assert n >= 30 and len(sp.waitlist(clin, ("PROGRAMADA",))) == n
    urgent = next(p for p in proposal if p["prioridad"] == "URGENTE")
    with pytest.raises(sqlite3.IntegrityError, match="fecha futura"):
        sp.mark_done(clin, urgent["id"], COORD, NOW, coordinator=True)            # es para mañana
    sp.mark_done(clin, urgent["id"], COORD, "2026-09-22 15:00:00", coordinator=True)
    with pytest.raises(sqlite3.IntegrityError, match="realizada"):
        sp.cancel(clin, urgent["id"], COORD, NOW, category="Otro", detail="ya no se necesita", coordinator=True)
    first = sp.waitlist(clin, ("PROGRAMADA",))[0]["id"]
    with pytest.raises(sqlite3.IntegrityError, match="Solo coordinación"):
        sp.reprogram(clin, first, DRA, NOW, "Cirujano incapacitado", coordinator=False)
    sp.reprogram(clin, first, COORD, NOW, "Cirujano incapacitado", coordinator=True)
    assert any(r["id"] == first for r in sp.waitlist(clin))
    with pytest.raises(sqlite3.IntegrityError):
        with clin:
            clin.execute("DELETE FROM cirugias_solicitudes")


def test_schedule_rules_overbooking_needs_justification(clin):
    prof = _prof()
    rid = sp.request_surgery(clin, id_paciente=110, area=GEN, prioridad="ELECTIVA", procedimiento="Herniorrafia inguinal",
                             user_id=DRA, now=NOW)
    with pytest.raises(sqlite3.IntegrityError, match="Solo coordinación"):
        sp.schedule(clin, rid, "2026-09-23", DRA, NOW, prof, coordinator=False, hour="16:30")
    with pytest.raises(sqlite3.IntegrityError, match="anterior"):
        sp.schedule(clin, rid, "2026-09-20", COORD, NOW, prof, coordinator=True, hour="16:30")
    free, _ = sp.free_on(clin, prof, GEN, "2026-09-23")
    fillers = [sp.request_surgery(clin, id_paciente=110 + 0, area=GEN, prioridad="ELECTIVA",
                                  procedimiento=f"Relleno {i}", user_id=DRA, now=NOW) for i in range(free)]
    for i, f in enumerate(fillers):   # llena los cupos del día con pacientes distintos (id del extracto)
        clin.execute("UPDATE cirugias_solicitudes SET id_paciente = ? WHERE id = ?", (900000 + i, f))
        sp.schedule(clin, f, "2026-09-23", COORD, NOW, prof, coordinator=True, hour=f"{7 + i:02d}:00")
    with pytest.raises(sqlite3.IntegrityError, match="justificación"):
        sp.schedule(clin, rid, "2026-09-23", COORD, NOW, prof, coordinator=True, hour="16:30")
    sp.schedule(clin, rid, "2026-09-23", COORD, NOW, prof, coordinator=True,
                justification="Se habilita turno quirúrgico adicional en la tarde", hour="16:30")
    row = clin.execute("SELECT sobrecupo_justificacion FROM cirugias_solicitudes WHERE id = ?", (rid,)).fetchone()
    assert "turno quirúrgico" in row[0]


def test_same_patient_same_day_and_urgent_window(clin):
    prof = _prof()
    a = sp.request_surgery(clin, id_paciente=110, area=ORTO, prioridad="URGENTE", procedimiento="Lavado quirúrgico",
                           user_id=DRA, now=NOW)
    b = sp.request_surgery(clin, id_paciente=110, area=GEN, prioridad="ELECTIVA", procedimiento="Colecistectomía",
                           user_id=DRA, now=NOW)
    with pytest.raises(sqlite3.IntegrityError, match="urgente"):
        sp.schedule(clin, a, "2026-09-25", COORD, NOW, prof, coordinator=True, hour="16:30")
    sp.schedule(clin, a, "2026-09-22", COORD, NOW, prof, coordinator=True, justification="", hour="16:30")
    with pytest.raises(sqlite3.IntegrityError, match="otra cirugía programada ese día"):
        sp.schedule(clin, b, "2026-09-22", COORD, NOW, prof, coordinator=True, hour="16:30")


def test_cancel_rules_by_role(clin):
    prof = _prof()
    mine = sp.request_surgery(clin, id_paciente=110, area=GEN, prioridad="ELECTIVA", procedimiento="Herniorrafia",
                              user_id=DRA, now=NOW)
    his = sp.waitlist(clin)[-1]["id"] if sp.waitlist(clin)[-1]["origen"] == "HIS" else \
        next(r["id"] for r in sp.waitlist(clin) if r["origen"] == "HIS")
    with pytest.raises(sqlite3.IntegrityError, match="propias"):
        sp.cancel(clin, his, DRA, NOW, category="Paciente desiste", detail="", coordinator=False)
    with pytest.raises(sqlite3.IntegrityError, match="propias"):
        sp.cancel(clin, mine, DR_PAREDES, NOW, category="Paciente desiste", detail="", coordinator=False)
    with pytest.raises(sqlite3.IntegrityError, match="15"):
        sp.cancel(clin, mine, DRA, NOW, category="Otro", detail="corto", coordinator=False)
    sp.cancel(clin, mine, DRA, NOW, category="Paciente desiste", detail="", coordinator=False)
    # programada para mañana: solo coordinación y con detalle
    other = sp.request_surgery(clin, id_paciente=110, area=GEN, prioridad="ELECTIVA", procedimiento="Biopsia",
                               user_id=DRA, now=NOW)
    sp.schedule(clin, other, "2026-09-22", COORD, NOW, prof, coordinator=True,
                justification="Cupo acordado con el servicio de cirugía", hour="16:30")
    with pytest.raises(sqlite3.IntegrityError, match="propias"):
        sp.cancel(clin, other, DRA, NOW, category="Paciente desiste", detail="", coordinator=False)
    with pytest.raises(sqlite3.IntegrityError, match="15"):
        sp.cancel(clin, other, COORD, NOW, category="Sin cama postoperatoria", detail="", coordinator=True)
    sp.cancel(clin, other, COORD, NOW, category="Sin cama postoperatoria", detail="UCI llena, sin cama de recuperación",
              coordinator=True)
    assert tuple(clin.execute("SELECT estado, motivo_categoria FROM cirugias_solicitudes WHERE id = ?",
                        (other,)).fetchone()) == ("CANCELADA", "Sin cama postoperatoria")


def test_coordinator_role_permissions(clin):
    perms = ps.user_permissions(clin, COORD)
    assert {"quirofanos.ver", "quirofanos.coordinar", "quirofanos.solicitar"} <= perms
    assert "hc.ver_completa" not in perms
    assert "quirofanos.coordinar" not in ps.user_permissions(clin, DRA)
    assert "quirofanos.coordinar" not in ps.user_permissions(clin, 1)
