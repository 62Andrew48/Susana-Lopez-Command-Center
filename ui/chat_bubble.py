"""
ui/chat_bubble.py — Asistente IA flotante (burbuja abajo a la derecha, disponible en todas las páginas).

  * Se abre y se cierra sin salir de la página. Corre dentro de un st.fragment: conversar solo recalcula
    el panel, no el tablero de fondo.
  * La conversación es la MISMA del Asistente IA (st.session_state.history[usuario]): lo que se pregunta
    en la burbuja aparece en la página y viceversa, y se conserva al navegar entre secciones.
  * Solo la ven los roles con el permiso agente.consultar (Admin y Doctor). Enfermería y pacientes no.
  * Antes del agente NL2SQL resuelve una intención propia: "¿dónde hay una cama libre?", con la ubicación
    física (piso, habitación y cama) que sale del mapa de camas.
"""
from __future__ import annotations

import re
import unicodedata

import streamlit as st

import database as db
from agent import KEY_QUESTIONS, AgentResponse
from ui import context as ctx
from ui.theme import BLUE, BORDER, MUTED, TEXT, esc, severity_pill

PANEL_H = 430            # alto del hilo de mensajes (px)
SHORT = ["🛏️ Camas UCI hoy", "💊 Medicamentos < 5 días", "⏱️ Espera urgencias", "🏥 Servicio con más ingresos"]

CSS = f"""
<style>
  .st-key-ia_fab {{position:fixed; right:24px; bottom:24px; z-index:1000; width:auto !important;}}
  .st-key-ia_fab button {{border-radius:999px !important; padding:0.65rem 1.15rem !important; border:none !important;
      background:linear-gradient(135deg, {BLUE}, #059669) !important; color:#FFF !important; font-weight:700 !important;
      box-shadow:0 8px 24px rgba(31,78,121,.35) !important;}}
  .st-key-ia_fab button p {{color:#FFF !important; font-size:0.95rem !important;}}
  .st-key-ia_panel {{position:fixed; right:24px; bottom:88px; z-index:1000; width:410px; max-width:calc(100vw - 32px);
      background:#FFF; border:1px solid {BORDER}; border-radius:16px; box-shadow:0 18px 50px rgba(15,23,42,.22);
      padding:0.8rem 0.9rem 0.6rem;}}
  .ia-head {{display:flex; align-items:center; gap:0.6rem; padding-bottom:0.5rem; border-bottom:1px solid {BORDER};}}
  .ia-logo {{width:2.2rem; height:2.2rem; border-radius:50%; display:flex; align-items:center; justify-content:center;
      background:linear-gradient(135deg, {BLUE}, #059669); color:#FFF; font-weight:800; font-size:0.85rem; flex:none;}}
  .ia-head b {{color:{TEXT}; font-size:0.95rem;}}
  .ia-head small {{display:block; color:{MUTED}; font-size:0.72rem;}}
  .ia-me, .ia-bot {{font-size:0.84rem; line-height:1.45; border-radius:12px; padding:0.5rem 0.7rem; margin:0.35rem 0;
      max-width:92%;}}
  .ia-me {{background:{BLUE}; color:#FFF; margin-left:auto; border-bottom-right-radius:4px;}}
  .ia-meta {{font-size:0.68rem; color:{MUTED}; margin:-0.15rem 0 0.25rem;}}
  .ia-beds {{display:flex; flex-wrap:wrap; gap:0.3rem; margin-top:0.35rem;}}
  .ia-beds span {{background:#ECFDF5; border:1px solid #A7F3D0; color:#065F46; border-radius:7px;
      padding:0.15rem 0.45rem; font-size:0.75rem; font-weight:600;}}
  .st-key-ia_panel [data-testid="stChatMessage"] {{padding:0.3rem 0.4rem; background:#F8FAFC;}}
  .st-key-ia_panel [data-testid="stChatMessage"] p {{font-size:0.84rem;}}
  .st-key-ia_panel .stButton button {{font-size:0.78rem; padding:0.2rem 0.5rem; min-height:0;}}
  @media (max-width: 640px) {{
    .st-key-ia_panel {{right:8px; left:8px; bottom:78px; width:auto;}}
    .st-key-ia_fab {{right:14px; bottom:14px;}}
    .st-key-ia_close {{display:none;}}   /* en celular se cierra con el botón flotante */
  }}
</style>
"""


# ---------------------------------------------------------------------------
# Intención local: ubicar camas libres (el agente NL2SQL no conoce pisos ni habitaciones)
# ---------------------------------------------------------------------------
_BED_Q = re.compile(r"\b(donde|hay|ubica|busca|necesito|disponible|libre)\w*\b.*\bcamas?\b|\bcamas?\b.*\b(libres?|disponibles?)\b")
_POP_WORDS = [("neonat", "Neonatal"), ("recien nacid", "Neonatal"), ("pediatr", "Pediátrica"), ("nin", "Pediátrica"),
              ("gineco", "Materna"), ("materna", "Materna"), ("obstetr", "Materna"), ("embaraz", "Materna"),
              ("uci", "Crítica adultos"), ("intensiv", "Crítica adultos"), ("intermedi", "Crítica adultos"),
              ("adult", "Adultos"), ("hospitaliz", "Adultos")]


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _bed_answer(question: str) -> AgentResponse | None:
    q = _norm(question)
    if not _BED_Q.search(q) or "ocupad" in q:
        return None
    pop = next((p for w, p in _POP_WORDS if w in q), None)
    floor = re.search(r"piso\s*(\d)", q)
    beds = db.free_beds(ctx.get_conn(), ctx.ref_date(), population=pop, limit=500)
    if floor:
        beds = beds[beds["piso"] == int(floor.group(1))]
    who = f"para población {pop.lower()}" if pop else "de internación"
    where = f" en el piso {floor.group(1)}" if floor else ""
    if beds.empty:
        text = (f"No hay camas físicas libres {who}{where} en el censo del {ctx.ref_date():%d/%m/%Y}. "
                "Opciones: habilitar camas de expansión de la unidad o coordinar un traslado.")
    else:
        text = (f"Hay **{len(beds)} camas físicas libres** {who}{where} (censo del {ctx.ref_date():%d/%m/%Y}). "
                "Las más fáciles de ubicar:")
    resp = AgentResponse(question, text, engine="mapa de camas")
    resp.data = beds.head(6)[["ubicacion", "unidad"]].rename(columns={"ubicacion": "cama"}) if not beds.empty else None
    return resp


def ask(question: str) -> AgentResponse:
    """Punto único de entrada del chat (burbuja y página): mapa de camas o agente NL2SQL."""
    local = _bed_answer(question)
    return local if local is not None else ctx.get_agent().ask(question)


# ---------------------------------------------------------------------------
# Render compacto de una respuesta
# ---------------------------------------------------------------------------
ENGINE_SHORT = {"llm": "🧠 LLM", "reglas": "🛡️ Plan B · SQL validado", "reglas (respaldo)": "🛟 Plan B de respaldo",
                "seguridad": "⛔ Bloqueado", "sql directo": "⌨️ SQL validado", "mapa de camas": "🛏️ Mapa de camas"}


def _render(resp: AgentResponse, idx: int) -> None:
    st.markdown(resp.answer)
    if resp.engine == "mapa de camas" and resp.data is not None:
        st.markdown('<div class="ia-beds">' + "".join(f"<span>{esc(c)}</span>" for c in resp.data["cama"])
                    + "</div>", unsafe_allow_html=True)
    else:
        for rec in resp.recommendations[:1]:
            st.markdown(f'{severity_pill(rec.severity)} <b>{esc(rec.title)}</b> — {esc(rec.action)}',
                        unsafe_allow_html=True)
        if resp.data is not None and not resp.data.empty and resp.engine != "seguridad" \
                and "severity" not in resp.data.columns:
            st.dataframe(resp.data.head(8), hide_index=True, width="stretch",
                         height=min(36 * min(len(resp.data), 8) + 40, 260), key=f"ia_df_{ctx.user_id()}_{idx}")
    meta = f"{ENGINE_SHORT.get(resp.engine, resp.engine)} · {resp.elapsed_ms} ms"
    if resp.sql:
        with st.expander(meta + " · ver SQL"):
            st.code(resp.sql, language="sql")
    else:
        st.markdown(f'<div class="ia-meta">{esc(meta)}</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Burbuja
# ---------------------------------------------------------------------------
def _history() -> list:
    return st.session_state.setdefault("history", {}).setdefault(ctx.user_id(), [])


@st.fragment
def _bubble() -> None:
    is_open = st.session_state.get("ia_open", False)
    if is_open:
        with st.container(key="ia_panel"):
            head, close = st.columns([5, 1], vertical_alignment="center")
            head.markdown('<div class="ia-head"><div class="ia-logo">IA</div><div><b>Asistente HSLV</b>'
                          '<small>Consulta los datos del hospital en lenguaje natural · solo lectura</small>'
                          '</div></div>', unsafe_allow_html=True)
            if close.button("✕", key="ia_close", help="Cerrar"):
                st.session_state.ia_open = False
                st.rerun(scope="fragment")
            history = _history()
            thread = st.container(height=PANEL_H, border=False)
            with thread:
                if not history:
                    st.markdown(f'<div class="ia-bot" style="background:#F1F5F9;color:{TEXT}">Hola 👋 Soy el asistente '
                                'del HSLV. Pregúntame por camas, urgencias, farmacia o quirófanos. Por ejemplo:</div>',
                                unsafe_allow_html=True)
                    for i, (label, q) in enumerate(zip(SHORT, KEY_QUESTIONS)):
                        if st.button(label, key=f"ia_sug_{i}", help=q, width="stretch"):
                            st.session_state.ia_pending = q
                    if st.button("🗺️ ¿Dónde hay camas libres para adultos?", key="ia_sug_beds", width="stretch"):
                        st.session_state.ia_pending = "¿Dónde hay camas libres para adultos?"
                for i, (question, resp) in enumerate(history):
                    st.markdown(f'<div class="ia-me">{esc(question)}</div>', unsafe_allow_html=True)
                    with st.chat_message("assistant", avatar="🏥"):
                        _render(resp, i)
            prompt = st.chat_input("Escribe tu pregunta…", key="ia_input")
            prompt = prompt or st.session_state.pop("ia_pending", None)
            if prompt:
                with thread:
                    st.markdown(f'<div class="ia-me">{esc(prompt)}</div>', unsafe_allow_html=True)
                    with st.spinner("Consultando la base del hospital…"):
                        history.append((prompt, ask(prompt)))
                st.rerun(scope="fragment")
            if history:
                c1, c2 = st.columns(2)
                if "asistente" in ctx.PAGES:
                    c1.page_link(ctx.PAGES["asistente"], label="Pantalla completa", icon="↗️")
                if c2.button("Limpiar conversación", key="ia_clear", width="stretch"):
                    history.clear()
                    st.rerun(scope="fragment")
    with st.container(key="ia_fab"):
        if st.button("✕  Cerrar" if is_open else "✨  Pregúntale a la IA", key="ia_toggle"):
            st.session_state.ia_open = not is_open
            st.rerun(scope="fragment")


def render(current_slug: str | None) -> None:
    """Dibuja la burbuja si el rol puede consultar al agente y no está ya en la página del Asistente."""
    if not ctx.can("agente.consultar") or current_slug == "asistente":
        return
    st.markdown(CSS, unsafe_allow_html=True)
    _bubble()
