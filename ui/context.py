"""
ui/context.py — Estado compartido de la sesión: conexiones, usuario activo, permisos, reloj clínico
y autorización con auditoría. Las páginas solo consumen estas funciones.
"""
from __future__ import annotations

import logging
from datetime import date

import streamlit as st

import config
import database as db
import demo_seed as ds
import pharmacy_service as ps
from agent import HospitalAgent

PAGES: dict = {}   # registro de st.Page (lo llena app.py) para enlaces entre páginas


# ---------------------------------------------------------------------------
# Base analítica: construcción visible (sin pantalla en blanco)
# ---------------------------------------------------------------------------
class _StatusLogHandler(logging.Handler):
    def __init__(self, status):
        super().__init__(level=logging.INFO)
        self.status = status

    def emit(self, record: logging.LogRecord) -> None:
        self.status.write(f"{record.getMessage()}")


def analytics_ready() -> bool:
    if not config.DB_PATH.exists():
        return False
    try:
        conn = db.get_connection(read_only=True)
        try:
            conn.execute("SELECT valor FROM metadatos WHERE clave = 'fecha_referencia'").fetchone()
            return True
        finally:
            conn.close()
    except Exception:
        return False


def build_analytics_with_status(label: str, rebuild: bool) -> None:
    missing = [f for f in db.SOURCE_FILES.values() if not (config.DATA_DIR / f).exists()]
    if missing:
        st.error("Faltan archivos del HIS en la carpeta Datos: " + ", ".join(missing))
        st.stop()
    etl_logger = logging.getLogger("database")
    with st.status(label, expanded=True) as status:
        handler = _StatusLogHandler(status)
        etl_logger.addHandler(handler)
        try:
            db.build_database(rebuild=rebuild)
        except Exception as exc:
            status.update(label="No se pudo construir la base de datos", state="error")
            st.exception(exc)
            st.stop()
        finally:
            etl_logger.removeHandler(handler)
        status.update(label="Base de datos hospitalaria lista", state="complete", expanded=False)


# ---------------------------------------------------------------------------
# Recursos compartidos por proceso
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_agent() -> HospitalAgent:
    return HospitalAgent()


@st.cache_resource(show_spinner=False)
def get_conn():
    return db.get_connection(read_only=True)


@st.cache_resource(show_spinner=False)
def get_clin():
    conn = ps.init_clinical_db(config.DB_PATH, ps.CLINICAL_DB)
    ds.seed(conn, config.DB_PATH)
    return conn


@st.cache_data(ttl=600, show_spinner=False)
def alerts() -> list:
    """Alertas del motor de recomendaciones. Salen de la base analítica (no cambian con los clics):
    se calculan una vez cada 10 minutos en lugar de en cada interacción."""
    return get_agent().alerts()


@st.cache_data(ttl=600, show_spinner=False)
def cached(fn_name: str, *args, **kwargs):
    """Cachea cualquier KPI de database.py por nombre y argumentos."""
    return getattr(db, fn_name)(get_conn(), *args, **kwargs)


def meta() -> dict:
    return db.get_metadata(get_conn())


def ref_date() -> date:
    return date.fromisoformat(meta()["fecha_referencia"])


def data_start() -> date:
    return date.fromisoformat(meta()["fecha_min_datos"])


def reset_analytics_resources() -> None:
    get_agent().close()
    get_conn().close()
    for resource in (get_agent, get_conn):
        resource.clear()
    st.cache_data.clear()   # incluye alerts()


def reset_clinical_demo() -> None:
    get_clin().close()
    get_clin.clear()
    ds.reset(ps.CLINICAL_DB, config.DB_PATH).close()
    for key in ("authz", "emergency", "last_simulation"):
        st.session_state.pop(key, None)


# ---------------------------------------------------------------------------
# Usuario activo, permisos y reloj
# ---------------------------------------------------------------------------
def user_id() -> int | None:
    """Usuario autenticado (lo fija ui/session.py al iniciar sesión). None si nadie ha iniciado sesión."""
    return st.session_state.get("user_id")


def logged_in() -> bool:
    uid = user_id()
    if uid is None:
        return False
    row = get_clin().execute("SELECT estado_cuenta FROM usuarios WHERE id = ?", (uid,)).fetchone()
    return row is not None and row["estado_cuenta"] == "ACTIVO"


def current_user() -> dict:
    row = get_clin().execute("SELECT u.*, r.codigo AS rol, r.nombre AS rol_nombre FROM usuarios u "
                             "JOIN roles r ON r.id = u.rol_id WHERE u.id = ?", (user_id(),)).fetchone()
    return dict(row)


def permissions() -> set[str]:
    return ps.user_permissions(get_clin(), user_id())


def can(permission: str) -> bool:
    return permission in permissions()


def clock() -> str:
    return ds.get_clock(get_clin())


def shift():
    return ps.current_shift(get_clin(), user_id(), clock())


def emergency_for(id_paciente: int) -> str | None:
    return st.session_state.setdefault("emergency", {}).get((user_id(), id_paciente))


def set_emergency(id_paciente: int, justification: str) -> None:
    st.session_state.setdefault("emergency", {})[(user_id(), id_paciente)] = justification


def authorize_once(permission: str, id_paciente: int | None = None, justification: str | None = None):
    """Autoriza y audita UNA vez por combinación (usuario, permiso, paciente, justificación, reloj):
    cada interacción de Streamlit re-ejecuta la página y no debe inflar la bitácora."""
    key = (user_id(), permission, id_paciente, justification, clock())
    cache = st.session_state.setdefault("authz", {})
    if key not in cache:
        cache[key] = ps.authorize(get_clin(), user_id(), permission, id_paciente, justification, clock())
    return cache[key]


@st.cache_data(ttl=600, show_spinner=False)
def _census(day_iso: str):
    from datetime import date
    return db.bed_map(get_conn(), date.fromisoformat(day_iso))


def live_beds(day_iso: str | None = None):
    """Censo de camas del día de corte (en caché) + lo que el personal ocupó o liberó en la app."""
    import beds_service as bs
    return bs.apply(_census(day_iso or ref_date().isoformat()), get_clin(), clock())


def set_theme(theme: str) -> None:
    """Guarda la preferencia claro/oscuro del usuario (se recuerda en su próximo inicio de sesión)."""
    clin = get_clin()
    with clin:
        clin.execute("UPDATE usuarios SET preferencia_tema = ? WHERE id = ?", (theme, user_id()))
