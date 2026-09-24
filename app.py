"""
app.py — Punto de entrada de la interfaz (Streamlit ≥ 1.46).

    streamlit run app.py

Estructura:
  * Cabecera contextual persistente: usuario (selector de demo), campana de notificaciones por rol, turno,
    acceso de emergencia y reloj.
  * Menú lateral agrupado (Operación · Clínico · Gestión) filtrado por permisos (RBAC de clinico.db).
    La página de inicio es "Hoy". st.navigation ejecuta SOLO la página activa.
  * Subsecciones dentro de cada página con st.tabs / st.expander.

Módulos:  ui/theme.py (estilo y componentes) · ui/context.py (sesión, RBAC, reloj)
          ui/pages_hoy.py (inicio por rol) · ui/pages_camas.py (mapa de camas por piso)
          ui/chat_bubble.py (asistente IA flotante)
          ui/pages_analytics.py (Tablero, Asistente, Alertas, Datos) · ui/pages_clinical.py (Clínico, Portal)
"""
from __future__ import annotations

from datetime import datetime

import streamlit as st

import config
import demo_seed as ds
from ui import chat_bubble as cb
from ui import context as ctx
from ui import notifications as nt
from ui import pages_analytics as pa
from ui import pages_camas as pm
from ui import pages_clinical as pc
from ui import pages_hoy as ph
from ui.theme import chip, inject_css

st.set_page_config(page_title="HSLV · Centro de mando operativo", page_icon="🏥", layout="wide")
inject_css()

# --- Inicialización (con progreso visible si la base analítica no existe) ---
if not ctx.analytics_ready():
    ctx.build_analytics_with_status("Inicializando base de datos hospitalaria...", rebuild=config.DB_PATH.exists())
clin = ctx.get_clin()


def _clock_label() -> str:
    return f"{datetime.strptime(ctx.clock(), '%Y-%m-%d %H:%M:%S'):%d/%m/%Y %H:%M}"


# ---------------------------------------------------------------------------
# Cabecera contextual persistente
# ---------------------------------------------------------------------------
brand, who, bell_col, clock_col = st.columns([2.2, 1.6, 0.5, 0.9], vertical_alignment="center")
brand.markdown('<div class="brand"><h1>Hospital Susana López de Valencia</h1>'
               '<p>Centro de mando</p></div>', unsafe_allow_html=True)
selected = who.selectbox("Usuario (demo)", list(ctx.DEMO_SELECTOR), format_func=ctx.DEMO_SELECTOR.get,
                         index=list(ctx.DEMO_SELECTOR).index(ctx.user_id()),
                         help="Selector simulado para la demostración. En producción: login con JWT.")
if selected != ctx.user_id():
    st.session_state.user_id = selected
    st.rerun()

with clock_col.popover(f"🕒 {datetime.strptime(ctx.clock(), '%Y-%m-%d %H:%M:%S'):%H:%M}", width="stretch",
                       help="Reloj de la demostración: cambia la hora para simular turnos"):
    st.caption(f"Reloj clínico: **{_clock_label()}**")
    if st.button("☀️ Turno de día (10:00)", width="stretch"):
        ds.set_clock_hour(clin, 10)
        st.rerun()
    if st.button("🌙 Turno de noche (22:00)", width="stretch",
                 help="Nadie está en turno: permite demostrar el acceso de emergencia"):
        ds.set_clock_hour(clin, 22)
        st.rerun()
    st.divider()
    if st.button("↺ Reiniciar escenario clínico", width="stretch",
                 help="Borra clinico.db y vuelve a sembrar la demo. No toca la base analítica."):
        ctx.reset_clinical_demo()
        st.rerun()

user = ctx.current_user()
chips = [chip(user["rol_nombre"], "info", "👤")]
if user["rol"] in ("DOCTOR", "ENFERMERIA"):
    turno = ctx.shift()
    chips.append(chip(f"En turno · {turno['servicio']} hasta {turno['fin'][11:16]}", "ok", "●") if turno
                 else chip("Fuera de turno · requiere “romper el vidrio”", "warn", "●"))
    active = [k for k in st.session_state.get("emergency", {}) if k[0] == user["id"]]
    if active:
        chips.append(chip(f"Acceso de emergencia activo ({len(active)})", "danger", "🚨"))
st.markdown(f'<div class="ctx-chips">{"".join(chips)}</div>', unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Navegación por permisos
# ---------------------------------------------------------------------------
P = ctx.permissions()
CATALOG = [  # grupo del menú, slug, título, icono, página, permisos que la habilitan (basta uno)
    ("Operación", "hoy", "Hoy", "🏠", ph.page_hoy, {"tablero.gerencial.ver", "camas.ver", "portal.propio"}),
    ("Operación", "camas", "Mapa de camas", "🛏️", pm.page_camas, {"camas.ver"}),
    ("Operación", "alertas", "Alertas y acciones", "🚨", pa.page_alertas, {"farmacia.alertas.ver"}),
    ("Clínico", "clinico", "Clínico y farmacia", "🩺", pc.page_clinico,
     {"hc.ver_notas", "hc.ver_completa", "prescripcion.crear", "dispensacion.registrar"}),
    ("Clínico", "portal", "Mis fórmulas y citas", "🧑", pc.page_portal, {"portal.propio"}),
    ("Gestión", "tablero", "Indicadores", "📊", pa.page_tablero, {"tablero.gerencial.ver", "camas.ver"}),
    ("Gestión", "asistente", "Asistente IA", "🤖", pa.page_asistente, {"agente.consultar"}),
    ("Gestión", "datos", "Datos y auditoría", "🗂️", pa.page_datos, {"auditoria.ver"}),
]
sections: dict[str, list] = {}
ctx.PAGES.clear()
for group, slug, title, icon, fn, needs in CATALOG:
    if P & needs:
        page = st.Page(fn, title=title, icon=icon, url_path=slug, default=slug == "hoy")
        ctx.PAGES[slug] = page
        sections.setdefault(group, []).append(page)

if not sections:
    st.error("Tu cuenta no tiene secciones habilitadas. Contacta al administrador.")
    st.stop()

# Menú lateral agrupado (Operación · Clínico · Gestión). El paciente ve un menú plano, sin grupos.
flat = [page for group in sections.values() for page in group]
menu = flat if ctx.current_user()["rol"] == "PACIENTE" else sections
page = st.navigation(menu, position="sidebar")
nt.render_bell(bell_col)  # la campana se dibuja cuando ya existen los enlaces a las páginas del rol
page.run()
cb.render(page.url_path)  # asistente IA flotante (abajo a la derecha), según permisos
