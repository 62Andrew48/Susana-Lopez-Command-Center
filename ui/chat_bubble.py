"""
ui/chat_bubble.py — Asistente IA flotante (burbuja abajo a la derecha, disponible en todas las páginas).

  * Se abre y se cierra sin salir de la página. Corre dentro de un st.fragment: conversar solo recalcula
    el panel, no el tablero de fondo.
  * La conversación es la MISMA del Asistente IA (st.session_state.history[usuario]): lo que se pregunta
    en la burbuja aparece en la página y viceversa, y se conserva al navegar entre secciones.
  * La ven todos los roles, con el alcance que dan sus permisos (ui/assistant_scope.py): Admin y Médico
    consultan la base analítica completa; Enfermería, solo camas, farmacia y urgencias; el paciente, solo
    sus fórmulas y citas. El SQL solo se muestra a quien tiene agente.consultar.
  * Antes del agente NL2SQL resuelve una intención propia: "¿dónde hay una cama libre?", con la ubicación
    física (piso, habitación y cama) que sale del mapa de camas.
"""
from __future__ import annotations

import re
import time
import unicodedata

import streamlit as st
import streamlit.components.v1 as components

import auth
import database as db
from agent import AgentResponse
import file_assistant
import voice
from ui import context as ctx
from ui.session import LOGO_ICON
from ui import assistant_scope as scope_mod
from ui.theme import esc, severity_pill

PANEL_H = 430            # alto del hilo de mensajes (px)
SHORT = ["Camas UCI hoy", "Medicamentos < 5 días", "Espera urgencias", "Servicio con más ingresos",
         "¿Dónde hay camas libres para adultos?"]

CSS = """
<style>
  .st-key-ia_fab {position:fixed; right:24px; bottom:24px; z-index:1000; width:auto !important;}
  .stApp .st-key-ia_fab button {border-radius:999px !important; padding:0.65rem 1.15rem !important; min-height:44px;
      border:1px solid transparent !important; background:var(--chat-me) !important; color:#FFF !important;
      font-weight:700 !important; box-shadow:0 8px 24px rgba(15,23,42,.30) !important;}
  .stApp .st-key-ia_fab button:hover {filter:brightness(1.15); border-color:transparent !important;
      background:var(--chat-me) !important;}
  .stApp .st-key-ia_fab button p, .stApp .st-key-ia_fab button span {color:#FFF !important; font-size:0.95rem !important;}
  .st-key-ia_panel {position:fixed; right:24px; bottom:88px; z-index:1000; width:410px; max-width:calc(100vw - 32px);
      max-height:calc(100vh - 110px); overflow:auto;
      background:var(--surface); border:1px solid var(--border); border-radius:16px; box-shadow:var(--shadow-lg);
      padding:0.8rem 0.9rem 0.6rem;}
  .ia-head {display:flex; align-items:center; gap:0.6rem; min-height:2.5rem;}
  .ia-head b {color:var(--text); font-size:0.95rem;}
  .ia-head small {display:block; color:var(--muted); font-size:0.72rem;}
  .st-key-ia_header {border-bottom:1px solid var(--border); padding-bottom:0.35rem;}
  /* Engranaje: reservado para la configuración del asistente. Texto accesible oculto a la vista. */
  .st-key-ia_settings button {min-height:36px; width:36px; padding:0 !important; border-radius:10px !important;}
  .st-key-ia_settings button p {position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0);
      white-space:nowrap;}
  .st-key-ia_settings {display:flex; justify-content:flex-end;}
  .st-key-ia_header [data-testid="stHorizontalBlock"] {flex-wrap:nowrap !important;}
  .st-key-ia_header [data-testid="stColumn"]:first-child {flex:1 1 auto !important; min-width:0; width:auto !important;}
  .st-key-ia_header [data-testid="stColumn"]:last-child {flex:0 0 auto !important; width:auto !important; min-width:0;}
  .ia-me, .ia-bot {font-size:0.84rem; line-height:1.45; border-radius:12px; padding:0.5rem 0.7rem; margin:0.35rem 0;
      max-width:92%;}
  .ia-me {background:var(--chat-me); color:var(--chat-me-fg); margin-left:auto; border-bottom-right-radius:4px;}
  .ia-bot {background:var(--surface-2); color:var(--text);}
  .ia-meta {font-size:0.68rem; color:var(--muted); margin:-0.15rem 0 0.25rem;}
  .ia-beds {display:flex; flex-wrap:wrap; gap:0.3rem; margin-top:0.35rem;}
  .ia-beds span {background:var(--ok-bg); border:1px solid var(--ok-bd); color:var(--ok-fg); border-radius:7px;
      padding:0.15rem 0.45rem; font-size:0.75rem; font-weight:600;}
  .st-key-ia_panel [data-testid="stChatMessage"] {padding:0.3rem 0.4rem; background:var(--surface-3);}
  .st-key-ia_panel [data-testid="stChatMessage"] p {font-size:0.84rem;}
  .st-key-ia_panel .stButton button {font-size:0.78rem; padding:0.2rem 0.5rem; min-height:0;}
  .st-key-ia_panel .st-key-ia_settings button {min-height:36px;}
  @media (max-width: 640px) {
    .st-key-ia_panel {right:8px; left:8px; bottom:78px; width:auto; max-height:calc(100vh - 96px);}
    .st-key-ia_fab {right:14px; bottom:14px;}
  }
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
    beds = ctx.live_beds()
    beds = beds[(beds["ocupada"] == 0) & (beds["es_virtual"] == 0) & (beds["servicio"] != "Urgencias")]
    if pop:
        beds = beds[beds["poblacion"] == pop]
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


def _scope():
    return scope_mod.scope_for(ctx.permissions())


def ask(question: str) -> AgentResponse:
    """Punto único de entrada del chat (burbuja y página). El alcance depende de los permisos del usuario."""
    user = ctx.current_user()
    t0 = time.time()
    resp = scope_mod.answer(question, _scope(), agent=ctx.get_agent(), bed_answer=_bed_answer, clin=ctx.get_clin(),
                            id_paciente=user.get("id_paciente"), now=ctx.clock(),
                            forecasts=ctx.can("tablero.gerencial.ver"))
    resp.elapsed_ms = resp.elapsed_ms or int((time.time() - t0) * 1000)
    if resp.engine in ("fuera de alcance", "seguridad"):  # queda en la bitácora de auditoría
        permission = "agente.consultar" if resp.engine == "fuera de alcance" else "consulta.solo_lectura"
        auth.log_denied(ctx.get_clin(), user["id"], permission, f"Asistente: {question}", ctx.clock())
    return resp


# ---------------------------------------------------------------------------
# Render compacto de una respuesta
# ---------------------------------------------------------------------------
ENGINE_SHORT = {"llm": "IA", "reglas": "Respuesta verificada", "reglas (respaldo)": "Respuesta verificada",
                "seguridad": "Bloqueado", "sql directo": "Consulta validada", "mapa de camas": "Mapa de camas",
                "glosario": "Glosario", "mis datos": "Solo tus datos", "fuera de alcance": "Acceso limitado",
                "modelo predictivo": "Modelo predictivo", "archivo": "Análisis del archivo"}


def prompt_text(value) -> str | None:
    """Texto del chat: lo escrito o, si se grabó audio con el micrófono, su transcripción."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    text = (getattr(value, "text", "") or "").strip()
    audio = getattr(value, "audio", None)
    if audio is not None:
        heard, engine = voice.transcribe(audio.getvalue())
        if heard:
            st.toast(f"Te escuché: “{heard}”", icon=":material/mic:")
            return f"{text} {heard}".strip()
        st.warning(engine)
    return text or None


def can_upload() -> bool:
    """Subir archivos al asistente: personal del hospital (el paciente solo consulta sus propios datos)."""
    return _scope().code not in ("paciente", "ninguno")


def chat_box(placeholder: str, key: str | None = None):
    """st.chat_input con micrófono y, para el personal, adjuntar archivos (CSV, Excel, PDF, TXT, imagen)."""
    extra = {"accept_file": True, "file_type": file_assistant.FILE_TYPES} if can_upload() else {}
    return st.chat_input(placeholder, key=key, accept_audio=True, **extra)


def attached_file(value):
    files = getattr(value, "files", None) or []
    return files[0] if files else None


def submit(value, pending: str | None = None) -> tuple[str, AgentResponse] | None:
    """Procesa lo que llegó del chat: texto, voz o archivo. Devuelve (lo que se muestra como pregunta, respuesta)."""
    file = attached_file(value)
    prompt = prompt_text(value) or (None if file else pending)
    if file is not None:
        t0 = time.time()
        resp = file_assistant.answer(prompt or "", file.name, file.getvalue(), getattr(file, "type", None))
        resp.elapsed_ms = int((time.time() - t0) * 1000)
        return f"[Archivo: {file.name}] {prompt or 'Resume este archivo.'}", resp
    if not prompt:
        return None
    return prompt, ask(prompt)


def speak(resp: AgentResponse, key: str) -> None:
    """Botón para escuchar la respuesta (voz del navegador)."""
    components.html(voice.speak_widget(resp.answer, key), height=42)


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
    if resp.sql and _scope().can_see_sql:
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
            scope = _scope()
            # Cabecera: título y engranaje. El panel se cierra solo con el botón flotante de abajo ("Cerrar"),
            # así no hay dos controles que hagan lo mismo. El engranaje queda reservado para la futura
            # configuración del asistente: visible, accesible y deshabilitado para no simular una función.
            with st.container(key="ia_header"):
                head, gear = st.columns([5, 1], vertical_alignment="center")
                head.markdown('<div class="ia-head"><div><b>Asistente HSLV</b>'
                              f'<small>{esc(scope.subtitle)}</small></div></div>', unsafe_allow_html=True)
                with gear:
                    st.button("Configuración del asistente", icon=":material/settings:", key="ia_settings",
                              disabled=True, help="Configuración del asistente (disponible próximamente)")
            history = _history()
            thread = st.container(height=PANEL_H, border=False)
            with thread:
                if not history:
                    st.markdown('<div class="ia-bot">Hola, soy el asistente '
                                'del HSLV. Puedes preguntarme, por ejemplo:</div>', unsafe_allow_html=True)
                    for i, q in enumerate(scope.suggestions):
                        label = SHORT[i] if scope.code == "completo" and i < len(SHORT) else q
                        if st.button(label, key=f"ia_sug_{i}", help=q, width="stretch"):
                            st.session_state.ia_pending = q
                for i, (question, resp) in enumerate(history):
                    st.markdown(f'<div class="ia-me">{esc(question)}</div>', unsafe_allow_html=True)
                    with st.chat_message("assistant", avatar=str(LOGO_ICON)):
                        _render(resp, i)
                        if i == len(history) - 1:
                            speak(resp, f"b{i}")
            value = chat_box("Escribe, graba o adjunta un archivo…" if can_upload() else "Escribe o graba tu pregunta…",
                             key="ia_input")
            pending = st.session_state.pop("ia_pending", None)
            if value is not None or pending:
                with thread:
                    with st.spinner("Buscando la respuesta…"):
                        result = submit(value, pending)
                if result:
                    history.append(result)
                    st.rerun(scope="fragment")
            if history:
                c1, c2 = st.columns(2)
                if "asistente" in ctx.PAGES:
                    c1.page_link(ctx.PAGES["asistente"], label="Pantalla completa", icon=":material/open_in_full:")
                if c2.button("Limpiar conversación", key="ia_clear", width="stretch"):
                    history.clear()
                    st.rerun(scope="fragment")
    with st.container(key="ia_fab"):
        if st.button("Cerrar" if is_open else "Pregúntale a la IA", key="ia_toggle",
                     icon=":material/close:" if is_open else ":material/auto_awesome:",
                     help="Cerrar el asistente" if is_open else "Abrir el asistente del hospital"):
            st.session_state.ia_open = not is_open
            st.rerun(scope="fragment")


def render(current_slug: str | None) -> None:
    """Dibuja la burbuja para todo usuario con algún alcance, salvo en la página del Asistente (evita duplicarlo)."""
    if _scope().code == "ninguno" or current_slug == "asistente":
        return
    st.markdown(CSS, unsafe_allow_html=True)
    _bubble()
