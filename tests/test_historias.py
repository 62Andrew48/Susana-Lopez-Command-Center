"""Pacientes, historia clínica (CRUD con versiones y anulación), adjuntos, búsqueda y apartado de 30 días."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import clinical_records as cr  # noqa: E402
import config  # noqa: E402
import demo_seed as ds  # noqa: E402
import pharmacy_service as ps  # noqa: E402

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
NOW = "2026-09-21 10:00:00"
DRA, ADMIN, OTRO = 2, 1, 9


@pytest.fixture()
def clin(tmp_path):
    c = ps.init_clinical_db(config.DB_PATH, tmp_path / "clinico.db")
    ds.seed(c, config.DB_PATH)
    with c:
        c.execute("INSERT INTO usuarios(id, usuario, hash_password, nombre_mostrado, rol_id, estado_cuenta, "
                  "registro_profesional) VALUES (9, 'dr.otro', 'x', 'Dr. Otro', 2, 'ACTIVO', 'RM-9')")
    yield c
    c.close()


def _new_patient(clin, doc="1061000111"):
    return cr.register_patient(clin, ADMIN, {"tipo_documento": "CC", "numero_documento": doc, "nombres": "Ana María",
                                             "apellidos": "Muñoz Ruiz", "fecha_nacimiento": "1990-05-10",
                                             "sexo": "Femenino", "telefono": "3001234567"}, NOW)


# --- Pacientes ---------------------------------------------------------------------
def test_register_patient_opens_history_and_rejects_duplicates(clin):
    pid = _new_patient(clin)
    assert pid >= cr.NEW_PATIENT_START
    row = cr.get_patient(clin, pid)
    assert row["edad"] == 36 and row["origen"] == "REGISTRO"
    assert clin.execute("SELECT 1 FROM historias_clinicas WHERE id_paciente = ?", (pid,)).fetchone()
    with pytest.raises(sqlite3.IntegrityError, match="Ya existe"):
        _new_patient(clin)
    with pytest.raises(sqlite3.IntegrityError, match="documento"):
        cr.register_patient(clin, ADMIN, {"nombres": "X", "apellidos": "Y"}, NOW)


def test_seeded_patients_come_from_his_without_names(clin):
    row = cr.get_patient(clin, 110)
    assert row["origen"] == "HIS" and row["nombres"] == "Paciente 110" and row["numero_documento"] is None


def test_import_his_patient_and_update_logs_event(clin):
    his = sqlite3.connect(config.DB_PATH).execute("SELECT MIN(id_paciente) FROM pacientes").fetchone()[0]
    cr.import_his_patient(clin, config.DB_PATH, his, ADMIN, NOW)
    assert cr.get_patient(clin, his)["origen"] == "HIS"
    changed = cr.update_patient(clin, his, ADMIN, {"telefono": "3110000000"}, NOW)
    assert changed == ["telefono"]
    events = [e["descripcion"] for e in ps.patient_timeline(clin, his)]
    assert any("telefono" in e for e in events)
    with pytest.raises(sqlite3.IntegrityError, match="no está en el extracto"):
        cr.import_his_patient(clin, config.DB_PATH, 999_999_999, ADMIN, NOW)


def test_patients_are_never_deleted(clin):
    pid = _new_patient(clin)
    with pytest.raises(sqlite3.IntegrityError, match="no se eliminan"):
        with clin:
            clin.execute("DELETE FROM pacientes_clinicos WHERE id_paciente = ?", (pid,))
    cr.set_patient_status(clin, pid, ADMIN, False, NOW)
    assert not [r for r in cr.search_patients(clin, "Muñoz") if r["id_paciente"] == pid]
    assert [r for r in cr.search_patients(clin, "Muñoz", include_inactive=True) if r["id_paciente"] == pid]


def test_search_patients_by_name_document_and_id(clin):
    pid = _new_patient(clin)
    for q in ("ana muñoz", "1061000", str(pid)):
        assert any(r["id_paciente"] == pid for r in cr.search_patients(clin, q)), q


# --- Registros: crear, corregir (con versión), anular (no borrar) -------------------------
def _record(clin, pid, autor=DRA):
    return cr.create_record(clin, id_paciente=pid, autor_id=autor, tipo="CONSULTA", titulo="Dolor torácico",
                            contenido="Dolor opresivo de 2 horas de evolución, sin irradiación.",
                            diagnostico_cie10="R072", diagnostico_nombre="Dolor precordial", plan="ECG y troponinas",
                            now=NOW)


def test_create_record_adds_timeline_event(clin):
    pid = _new_patient(clin)
    rid = _record(clin, pid)
    assert cr.records(clin, pid)[0]["id"] == rid
    assert any(e["tipo"] == "REGISTRO_HC" for e in ps.patient_timeline(clin, pid))


def test_update_keeps_previous_version_and_requires_reason(clin):
    pid = _new_patient(clin)
    rid = _record(clin, pid)
    with pytest.raises(sqlite3.IntegrityError, match="motivo"):
        cr.update_record(clin, rid, DRA, {"plan": "ECG, troponinas y radiografía"}, "corto", NOW)
    v = cr.update_record(clin, rid, DRA, {"plan": "ECG, troponinas y radiografía"}, "Faltó la radiografía de tórax",
                         "2026-09-21 11:00:00")
    assert v == 2
    rec = cr.records(clin, pid)[0]
    assert rec["plan"] == "ECG, troponinas y radiografía" and rec["version"] == 2
    old = cr.record_versions(clin, rid)
    assert len(old) == 1 and old[0]["plan"] == "ECG y troponinas" and old[0]["reemplazada_por"] == DRA


def test_only_author_can_edit_or_annul(clin):
    pid = _new_patient(clin)
    rid = _record(clin, pid)
    with pytest.raises(sqlite3.IntegrityError, match="Solo el profesional"):
        cr.update_record(clin, rid, OTRO, {"plan": "otro plan distinto"}, "Corrección de otro médico", NOW)
    with pytest.raises(sqlite3.IntegrityError, match="Solo el profesional"):
        cr.annul_record(clin, rid, OTRO, "No corresponde a este paciente", NOW)


def test_annul_instead_of_delete(clin):
    pid = _new_patient(clin)
    rid = _record(clin, pid)
    with pytest.raises(sqlite3.IntegrityError, match="no se eliminan"):
        with clin:
            clin.execute("DELETE FROM hc_registros WHERE id = ?", (rid,))
    cr.annul_record(clin, rid, DRA, "Registro hecho en el paciente equivocado", NOW)
    assert cr.records(clin, pid, include_annulled=False) == []
    rec = cr.records(clin, pid)[0]
    assert rec["estado"] == "ANULADO" and "equivocado" in rec["motivo_anulacion"]
    with pytest.raises(sqlite3.IntegrityError, match="anulado"):
        cr.update_record(clin, rid, DRA, {"plan": "nuevo plan"}, "Intento de cambiar un anulado", NOW)
    with pytest.raises(sqlite3.IntegrityError):  # tampoco por SQL directo
        with clin:
            clin.execute("UPDATE hc_registros SET contenido = 'x' WHERE id = ?", (rid,))


def test_versions_are_append_only(clin):
    pid = _new_patient(clin)
    rid = _record(clin, pid)
    cr.update_record(clin, rid, DRA, {"titulo": "Dolor torácico atípico"}, "Precisión del diagnóstico", NOW)
    with pytest.raises(sqlite3.IntegrityError):
        with clin:
            clin.execute("DELETE FROM hc_registros_versiones WHERE registro_id = ?", (rid,))


# --- Adjuntos ---------------------------------------------------------------------------
def test_attachments_validate_real_type_and_size(clin):
    pid = _new_patient(clin)
    rid = _record(clin, pid)
    aid = cr.add_attachment(clin, id_paciente=pid, user_id=DRA, filename="ecg.pdf", data=PDF, registro_id=rid, now=NOW)
    cr.add_attachment(clin, id_paciente=pid, user_id=DRA, filename="rx.png", data=PNG, descripcion="Rx tórax", now=NOW)
    name, mime, data = cr.attachment_content(clin, aid, pid)
    assert (name, mime, data) == ("ecg.pdf", "application/pdf", PDF)
    with pytest.raises(sqlite3.IntegrityError, match="PDF, PNG o JPG"):
        cr.add_attachment(clin, id_paciente=pid, user_id=DRA, filename="virus.pdf", data=b"MZ\x90\x00", now=NOW)
    with pytest.raises(sqlite3.IntegrityError, match="5 MB"):
        cr.add_attachment(clin, id_paciente=pid, user_id=DRA, filename="big.pdf",
                          data=b"%PDF" + b"0" * cr.MAX_FILE_BYTES, now=NOW)
    with pytest.raises(sqlite3.IntegrityError):  # otro paciente no puede leerlo por id
        cr.attachment_content(clin, aid, 110)


def test_attachment_annul_not_delete(clin):
    pid = _new_patient(clin)
    aid = cr.add_attachment(clin, id_paciente=pid, user_id=DRA, filename="lab.pdf", data=PDF, now=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        with clin:
            clin.execute("DELETE FROM hc_adjuntos WHERE id = ?", (aid,))
    cr.annul_attachment(clin, aid, DRA, "Documento de otro paciente", NOW)
    assert cr.attachments(clin, pid, include_annulled=False) == []


# --- Búsqueda ---------------------------------------------------------------------------
def test_search_records_by_text_type_dates_and_author(clin):
    pid = _new_patient(clin)
    _record(clin, pid)
    assert [r["id_paciente"] for r in cr.search_records(clin, text="precordial")] == [pid]
    assert cr.search_records(clin, text="Muñoz", tipo="CONSULTA")
    assert not cr.search_records(clin, text="precordial", tipo="EPICRISIS")
    assert cr.search_records(clin, desde="2026-09-21", hasta="2026-09-21", autor_id=DRA)
    assert not cr.search_records(clin, desde="2026-10-01")


def test_admin_can_search_but_not_read_clinical_content(clin):
    perms = ps.user_permissions(clin, ADMIN)
    assert {"hc.buscar", "pacientes.registrar"} <= perms
    assert "hc.ver_completa" not in perms and "hc.registrar" not in perms
    assert not ps.authorize(clin, ADMIN, "hc.ver_completa", 110, now=NOW).allowed


def test_doctor_off_duty_needs_break_glass_to_write(clin):
    assert ps.authorize(clin, DRA, "hc.registrar", 110, now=NOW).allowed          # 10:00 en turno
    assert not ps.authorize(clin, DRA, "hc.registrar", 110, now="2026-09-21 23:00:00").allowed


# --- Apartado de 30 días ------------------------------------------------------------------
def test_prescription_reserves_units_for_30_days_then_returns(clin):
    code = clin.execute("SELECT codigo FROM v_stock WHERE disponible >= 3 AND codigo IN "
                        "(SELECT codigo FROM productos_farmacia WHERE tipo_item = 'Medicamento') LIMIT 1").fetchone()[0]
    before = dict(ps.stock_of(clin, [code])[code])
    pid = ps.prescribe(clin, medico_id=DRA, id_paciente=110, codigo=code, dosis="500 mg VO", frecuencia_horas=8,
                       duracion_dias=1, dosis_prescritas=3, ambito="AMBULATORIA", now=NOW)
    held = dict(ps.stock_of(clin, [code])[code])
    assert held["disponible"] == before["disponible"] - 3 and held["reservado"] == before["reservado"] + 3
    row = clin.execute("SELECT fecha_limite_reclamo FROM prescripciones WHERE id = ?", (pid,)).fetchone()
    assert row[0] == "2026-10-21 10:00:00"
    assert any(r["id"] == pid and r["apartadas"] == 3 for r in ps.reservations(clin, code))
    assert pid not in ps.expire_prescriptions(clin, "2026-10-21 09:59:00")      # día 29: sigue apartado
    assert pid in ps.expire_prescriptions(clin, "2026-10-21 10:01:00")          # pasados 30 días: vuelve
    after = dict(ps.stock_of(clin, [code])[code])
    assert after["disponible"] == before["disponible"] and after["reservado"] == before["reservado"]


def test_old_schema_database_is_backed_up_and_recreated(tmp_path):
    path = tmp_path / "clinico.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE vieja (x)")
    old.execute("PRAGMA user_version = 1")
    old.commit()
    old.close()
    conn = ps.init_clinical_db(config.DB_PATH, path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == ps.SCHEMA_VERSION
    assert (tmp_path / "clinico_respaldo_v1.db").exists()
    conn.close()
