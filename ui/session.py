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
import mailer
from ui import context as ctx
from ui.theme import BORDER, BRAND_GREEN, MUTED, NAVY, TEXT, esc

ASSETS = Path(__file__).resolve().parents[1] / "assets"
LOGO, LOGO_ICON = ASSETS / "logo_hslv.png", ASSETS / "logo_hslv_icono.png"
AVATAR_COLOR = {"ADMIN": NAVY, "DOCTOR": BRAND_GREEN, "ENFERMERIA": "#0F766E", "PACIENTE": "#7C3AED",
                "FACTURACION": "#B45309"}
DEMO_ACCOUNTS = [("admin", "Gerencia"), ("dra.ruiz", "Dra. Ruiz · Médica"), ("enf.gomez", "Enf. Gómez"),
                 ("facturacion.alejandro", "Facturación · Alejandro"), ("paciente.laura", "Paciente Laura"), ("paciente.110", "Paciente 110")]


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
    _recovery()
    with st.expander("Acceso rápido para la demostración"):
        st.caption("Cuentas de prueba del escenario sintético (contraseña: demo). Cada ingreso queda en la bitácora. "
                   "Otras: dr.paredes (pediatría), enf.castro (turno de noche).")
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


def _recovery() -> None:
    """¿Olvidaste tu contraseña? Código de 6 dígitos por correo (o en pantalla si no hay SMTP)."""
    with st.expander("¿Olvidaste tu contraseña?"):
        step = st.session_state.get("rec_step", 1)
        if step == 1:
            with st.form("rec_request"):
                ident = st.text_input("Usuario o correo")
                if st.form_submit_button("Enviarme un código", width="stretch") and ident.strip():
                    try:
                        uid, code, mail = auth.request_recovery(ctx.get_clin(), ident, ctx.clock())
                    except Exception as exc:  # límite de códigos por hora
                        st.error(str(exc))
                        return
                    sent = bool(uid) and mailer.send_recovery_code(mail, code, auth.CODE_MINUTES)
                    st.session_state.rec_ident = ident.strip()
                    st.session_state.rec_demo_code = None if (sent or not uid) else code
                    st.session_state.rec_step = 2
                    st.rerun()
            return
        st.info("Si la cuenta existe y está activa, te enviamos un código de 6 dígitos que vence en "
                f"{auth.CODE_MINUTES} minutos.")
        demo = st.session_state.get("rec_demo_code")
        if demo:
            st.warning(f"Modo demostración (sin correo configurado): tu código es **{demo}**")
        with st.form("rec_reset"):
            code = st.text_input("Código", max_chars=6)
            new = st.text_input("Nueva contraseña", type="password", help="Mínimo 8 caracteres, con letras y números")
            again = st.text_input("Repite la contraseña", type="password")
            if st.form_submit_button("Cambiar contraseña", type="primary", width="stretch"):
                if new != again:
                    st.error("Las contraseñas no coinciden.")
                else:
                    try:
                        auth.reset_with_code(ctx.get_clin(), st.session_state.get("rec_ident", ""), code, new,
                                             ctx.clock())
                        for k in ("rec_step", "rec_ident", "rec_demo_code"):
                            st.session_state.pop(k, None)
                        st.success("Listo. Ya puedes ingresar con tu nueva contraseña.")
                    except Exception as exc:
                        st.error(str(exc))
        if st.button("Volver", key="rec_back"):
            for k in ("rec_step", "rec_ident", "rec_demo_code"):
                st.session_state.pop(k, None)
            st.rerun()


def password_form(key: str, forced: bool = False) -> None:
    with st.form(f"pwd_{key}"):
        if forced:
            st.warning("Tu contraseña es temporal: cámbiala para continuar.")
        cur = st.text_input("Contraseña actual", type="password")
        new = st.text_input("Nueva contraseña", type="password", help="Mínimo 8 caracteres, con letras y números")
        again = st.text_input("Repite la nueva", type="password")
        if st.form_submit_button("Guardar contraseña", type="primary", width="stretch"):
            if new != again:
                st.error("Las contraseñas no coinciden.")
                return
            try:
                auth.change_password(ctx.get_clin(), ctx.user_id(), cur, new, ctx.clock())
                st.success("Contraseña actualizada.")
                if forced:
                    st.rerun()
            except Exception as exc:
                st.error(str(exc))


def forced_password_change() -> None:
    """Primera entrada con contraseña temporal: no se muestra nada más hasta cambiarla."""
    st.markdown(f'<div class="login-brand" style="text-align:center">{logo_html(70)}</div>', unsafe_allow_html=True)
    st.markdown("### Cambia tu contraseña")
    password_form("forced", forced=True)
    if st.button("Cerrar sesión", key="forced_logout"):
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        st.rerun()
