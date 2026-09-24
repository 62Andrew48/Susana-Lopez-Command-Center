"""Inicio de sesión: credenciales, cuentas inactivas, bloqueo por intentos y bitácora."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auth  # noqa: E402
import config  # noqa: E402
import demo_seed as ds  # noqa: E402
import pharmacy_service as ps  # noqa: E402

NOW = "2026-09-21 10:00:00"


@pytest.fixture()
def clin(tmp_path):
    c = ps.init_clinical_db(config.DB_PATH, tmp_path / "clinico.db")
    ds.seed(c, config.DB_PATH)
    yield c
    c.close()


def _last(clin):
    return clin.execute("SELECT accion FROM auditoria_accesos ORDER BY id DESC LIMIT 1").fetchone()[0]


def test_valid_login_and_logout_are_audited(clin):
    assert auth.login(clin, "Dra.Ruiz", "demo", NOW).user_id == 2
    assert _last(clin) == "LOGIN_OK"
    auth.logout(clin, 2, NOW)
    assert _last(clin) == "LOGOUT"


def test_wrong_password_and_unknown_user_give_the_same_message(clin):
    bad_pwd = auth.login(clin, "admin", "x", NOW)
    unknown = auth.login(clin, "nadie", "demo", NOW)
    assert bad_pwd.user_id is None and unknown.user_id is None
    assert bad_pwd.message == unknown.message == auth.GENERIC_ERROR
    assert _last(clin) == "LOGIN_FALLIDO"


def test_account_locks_after_five_failures(clin):
    for _ in range(auth.MAX_ATTEMPTS):
        auth.login(clin, "enf.gomez", "mala", NOW)
    locked = auth.login(clin, "enf.gomez", "demo", "2026-09-21 10:05:00")
    assert locked.user_id is None and "bloqueada" in locked.message
    assert auth.login(clin, "enf.gomez", "demo", "2026-09-21 10:16:00").user_id == 3  # pasados 15 min


def test_suspended_account_cannot_enter(clin):
    with clin:
        clin.execute("UPDATE usuarios SET estado_cuenta = 'SUSPENDIDO' WHERE id = 2")
    assert auth.login(clin, "dra.ruiz", "demo", NOW).user_id is None
