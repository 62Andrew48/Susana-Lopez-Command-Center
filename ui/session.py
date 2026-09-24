"""
ui/session.py — Pantalla de inicio de sesión y tarjeta del usuario (con cierre de sesión) en la barra lateral.
La verificación vive en auth.py; aquí solo se presenta.
"""
from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

import streamlit as st

import auth
from ui import context as ctx
from ui.theme import BORDER, BRAND_GREEN, MUTED, NAVY, TEXT, esc

ASSETS = Path(__file__).resolve().parents[1] / "assets"
LOGO, LOGO_ICON = ASSETS / "logo_hslv.png", ASSETS / "logo_hslv_icono.png"
AVATAR_COLOR = {"ADMIN": NAVY, "DOCTOR": BRAND_GREEN, "ENFERMERIA": "#0F766E", "PACIENTE": "#7C3AED"}
DEMO_ACCOUNTS = [("admin", "Gerencia"), ("dra.ruiz", "Dra. Ruiz · Médica"), ("enf.gomez", "Enf. Gómez"),
                 ("paciente.110", "Paciente 110")]


@lru_cache(maxsize=4)
def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode() if path.exists() else ""


def logo_html(height: int = 44) -> str:
    data = _b64(LOGO_ICON)
    return f'<img src="data:image/png;base64,{data}" style="height:{height}px" alt="HSLV">' if data else ""


def _initials(name: str) -> str:
    parts = [p for p in name.replace("·", " ").replace(".", " ").split() if p[0].isalpha()]
    return "".join(p[0] for p in parts[:2]).upper() or "U"


def _do_login(username: str, password: str) -> None:
    result = auth.login(ctx.get_clin(), username, password, ctx.clock())
    if result.user_id is None:
        st.session_state.login_error = result.message
        return
    for key in ("login_error", "history", "ia_open", "authz", "emergency", "notif_read"):
        st.session_state.pop(key, None)
    st.session_state.user_id = result.user_id
    st.rerun()


def login_page() -> None:
    st.markdown(f"""<style>
      [data-testid="stSidebar"], header[data-testid="stHeader"] {{display:none;}}
      [data-testid="stMainBlockContainer"] {{max-width:440px; padding-top:3rem;}}
      .login-brand {{text-align:center; margin-bottom:1.2rem;}}
      .login-brand h2 {{color:{NAVY}; font-size:1.35rem; margin:0.6rem 0 0.1rem;}}
      .login-brand p {{color:{MUTED}; font-size:0.9rem; margin:0;}}
    </style>""", unsafe_allow_html=True)
    st.markdown(f'<div class="login-brand">{logo_html(88)}<h2>Hospital Susana López de Valencia</h2>'
                '<p>Centro de mando · inicia sesión para continuar</p></div>', unsafe_allow_html=True)
    with st.form("login", border=True):
        user = st.text_input("Usuario", placeholder="p. ej. dra.ruiz")
        pwd = st.text_input("Contraseña", type="password")
        if st.form_submit_button("Ingresar", type="primary", width="stretch"):
            _do_login(user, pwd)
    if st.session_state.get("login_error"):
        st.error(st.session_state.login_error)
    with st.expander("Acceso rápido para la demostración"):
        st.caption("Cuentas de prueba del escenario sintético (contraseña: demo). Cada ingreso queda en la bitácora.")
        cols = st.columns(2)
        for i, (username, label) in enumerate(DEMO_ACCOUNTS):
            if cols[i % 2].button(label, key=f"demo_{username}", width="stretch"):
                _do_login(username, "demo")


def user_card() -> None:
    """Tarjeta fija abajo en la barra lateral: foto (iniciales), nombre, rol, turno y cerrar sesión."""
    user = ctx.current_user()
    color = AVATAR_COLOR.get(user["rol"], NAVY)
    status = ""
    if user["rol"] in ("DOCTOR", "ENFERMERIA"):
        turno = ctx.shift()
        status = (f'<span style="color:#059669">● En turno hasta {turno["fin"][11:16]}</span>' if turno
                  else '<span style="color:#B45309">● Fuera de turno</span>')
    elif user["rol"] == "ADMIN":
        status = '<span style="color:#059669">● Activo</span>'
    st.markdown(f"""<style>
      .st-key-user_card {{position:fixed; bottom:12px; left:12px; width:calc(var(--sidebar-width, 300px) - 40px);
          max-width:276px; background:#FFF; border:1px solid {BORDER}; border-radius:12px; padding:0.6rem 0.7rem;
          box-shadow:0 2px 8px rgba(0,0,0,.06); z-index:999;}}
      .u-row {{display:flex; align-items:center; gap:0.6rem;}}
      .u-av {{width:2.3rem; height:2.3rem; border-radius:50%; background:{color}; color:#FFF; font-weight:700;
          display:flex; align-items:center; justify-content:center; font-size:0.85rem; flex:none;}}
      .u-name {{font-weight:650; color:{TEXT}; font-size:0.88rem; line-height:1.2;}}
      .u-meta {{font-size:0.72rem; color:{MUTED}; line-height:1.3;}}
      [data-testid="stSidebarUserContent"] {{padding-bottom:7rem;}}
    </style>""", unsafe_allow_html=True)
    with st.sidebar.container(key="user_card"):
        st.markdown(f'<div class="u-row"><div class="u-av">{esc(_initials(user["nombre_mostrado"]))}</div><div>'
                    f'<div class="u-name">{esc(user["nombre_mostrado"])}</div>'
                    f'<div class="u-meta">{esc(user["rol_nombre"])}{" · " + status if status else ""}</div></div></div>',
                    unsafe_allow_html=True)
        if st.button("Cerrar sesión", key="logout", icon=":material/logout:", width="stretch"):
            auth.logout(ctx.get_clin(), user["id"], ctx.clock())
            for key in list(st.session_state.keys()):
                del st.session_state[key]
            st.rerun()
