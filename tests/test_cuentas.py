"""Ingreso con Google y cuenta del paciente creada con documento + código enviado a su correo."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auth  # noqa: E402
import clinical_records as cr  # noqa: E402
import config  # noqa: E402
import demo_seed as ds  # noqa: E402
import pharmacy_service as ps  # noqa: E402

NOW = "2026-09-21 10:00:00"
MARIA_DOC, MARIA_MAIL = "1061800222", "maria.ortiz@correo.demo"


@pytest.fixture()
def clin(tmp_path):
    c = ps.init_clinical_db(config.DB_PATH, tmp_path / "clinico.db")
    ds.seed(c, config.DB_PATH)
    yield c
    c.close()


def _audit(clin, action):
    return clin.execute("SELECT recurso FROM auditoria_accesos WHERE accion = ? ORDER BY id DESC",
                        (action,)).fetchall()


# --- Google -----------------------------------------------------------------------------
def test_google_entra_solo_con_correo_registrado(clin):
    r = auth.login_google(clin, "Dra.Ruiz@hslv.demo", NOW)
    assert r.user_id == 2
    assert _audit(clin, "LOGIN_OK")[0][0] == "Inicio de sesión con Google"
    r = auth.login_google(clin, "desconocido@gmail.com", NOW)
    assert r.user_id is None and "no está vinculada" in r.message
    assert auth.login_google(clin, "dra.ruiz@hslv.demo", NOW, verified=False).user_id is None


def test_google_respeta_cuentas_suspendidas_y_correos_repetidos(clin):
    clin.execute("UPDATE usuarios SET estado_cuenta = 'SUSPENDIDO' WHERE id = 3")
    clin.commit()
    assert auth.login_google(clin, "enf.gomez@hslv.demo", NOW).user_id is None
    clin.execute("UPDATE usuarios SET correo = 'admin@hslv.demo' WHERE id = 2")
    clin.commit()
    r = auth.login_google(clin, "admin@hslv.demo", NOW)
    assert r.user_id is None and "varias cuentas" in r.message


# --- Registro del paciente ------------------------------------------------------------------
def test_paciente_crea_su_cuenta_con_codigo(clin):
    pid, code = auth.request_signup(clin, MARIA_DOC, "  Maria.Ortiz@correo.demo ", NOW)
    assert pid and len(code) == 6
    with pytest.raises(sqlite3.IntegrityError, match="código no es válido"):
        auth.complete_signup(clin, MARIA_DOC, MARIA_MAIL, "000000" if code != "000000" else "111111", "clave1234", NOW)
    with pytest.raises(sqlite3.IntegrityError, match="8 caracteres"):
        auth.complete_signup(clin, MARIA_DOC, MARIA_MAIL, code, "corta1", NOW)
    uid = auth.complete_signup(clin, MARIA_DOC, MARIA_MAIL, code, "clave1234", NOW)
    user = clin.execute("SELECT * FROM usuarios WHERE id = ?", (uid,)).fetchone()
    assert user["usuario"] == MARIA_MAIL and user["id_paciente"] == pid and user["estado_cuenta"] == "ACTIVO"
    assert user["nombre_mostrado"] == "María Ortiz"
    assert auth.login(clin, MARIA_MAIL, "clave1234", NOW).user_id == uid
    assert auth.login_google(clin, MARIA_MAIL, NOW).user_id == uid        # y también puede entrar con Google
    with pytest.raises(sqlite3.IntegrityError, match="código no es válido"):  # el código no se reutiliza
        auth.complete_signup(clin, MARIA_DOC, MARIA_MAIL, code, "clave1234", NOW)
    with pytest.raises(sqlite3.IntegrityError, match="Ya existe una cuenta"):
        auth.request_signup(clin, MARIA_DOC, MARIA_MAIL, NOW)


def test_no_se_abre_cuenta_ajena_ni_sin_correo_de_admisiones(clin):
    assert auth.request_signup(clin, MARIA_DOC, "otro@gmail.com", NOW) == (None, None)   # documento ajeno
    assert auth.request_signup(clin, "999", MARIA_MAIL, NOW) == (None, None)
    juan = clin.execute("SELECT correo FROM pacientes_clinicos WHERE numero_documento = '1061900333'").fetchone()
    assert juan[0] is None
    assert auth.request_signup(clin, "1061900333", "cualquiera@gmail.com", NOW) == (None, None)


def test_codigo_vence_y_limite_por_hora(clin):
    pid, code = auth.request_signup(clin, MARIA_DOC, MARIA_MAIL, NOW)
    with pytest.raises(sqlite3.IntegrityError, match="venció"):
        auth.complete_signup(clin, MARIA_DOC, MARIA_MAIL, code, "clave1234", "2026-09-21 10:20:00")
    auth.request_signup(clin, MARIA_DOC, MARIA_MAIL, "2026-09-21 10:21:00")
    auth.request_signup(clin, MARIA_DOC, MARIA_MAIL, "2026-09-21 10:22:00")
    with pytest.raises(sqlite3.IntegrityError, match="varios códigos"):
        auth.request_signup(clin, MARIA_DOC, MARIA_MAIL, "2026-09-21 10:23:00")


def test_admisiones_registra_y_valida_el_correo(clin):
    pid = clin.execute("SELECT id_paciente FROM pacientes_clinicos WHERE numero_documento = '1061900333'").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="correo no es válido"):
        cr.update_patient(clin, pid, 2, {"correo": "no-es-correo"}, NOW)
    assert cr.update_patient(clin, pid, 2, {"correo": " Diana.Velasco@Correo.demo "}, NOW) == ["correo"]
    assert auth.request_signup(clin, "1061900333", "diana.velasco@correo.demo", NOW)[0] == pid
