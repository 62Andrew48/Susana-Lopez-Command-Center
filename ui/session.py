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
                "FACTURACION": "#B45309", "QUIROFANOS": "#0E7490"}
DEMO_ACCOUNTS = [("admin", "Gerencia"), ("dra.ruiz", "Dra. Ruiz · Médica"), ("enf.gomez", "Enf. Gómez"),
                 ("facturacion.alejandro", "Facturación · Alejandro"),
                 ("quirofanos.bravo", "Quirófanos · Bravo"), ("paciente.laura", "Paciente Laura"), ("paciente.110", "Paciente 110")]


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


def google_provider() -> str | None | bool:
    """Proveedor OIDC configurado en .streamlit/secrets.toml ([auth] + [auth.google]). False si no hay."""
    try:
        cfg = st.secrets.get("auth", {})
    except Exception:  # sin secrets.toml
        return False
    if not cfg or "redirect_uri" not in cfg or "cookie_secret" not in cfg:
        return False
    if "google" in cfg:
        return "google"
    return None if "client_id" in cfg else False


def _google_logged_in() -> bool:
    try:
        return bool(st.user.is_logged_in)
    except Exception:
        return False


def _google_callback() -> None:
    """Vuelta de Google: el correo verificado debe corresponder a una cuenta activa del hospital."""
    if google_provider() is False or not _google_logged_in():
        return
    email = str(st.user.get("email") or "")
    if st.session_state.get("google_checked") == email:
        return  # ya se intentó con este correo: no repetir el intento (ni la auditoría) en cada recarga
    st.session_state.google_checked = email
    result = auth.login_google(ctx.get_clin(), email, ctx.clock(), bool(st.user.get("email_verified", True)))
    if result.user_id is None:
        st.session_state.login_error = result.message
        return
    for key in ("login_error", "history", "ia_open", "authz", "emergency", "notif_read"):
        st.session_state.pop(key, None)
    st.session_state.user_id = result.user_id
    st.session_state.via_google = True
    st.rerun()


def end_session() -> None:
    """Cierra la sesión de la app y, si se entró con Google, también la de Google en esta app."""
    google = st.session_state.get("via_google") or _google_logged_in()
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    if google:
        st.logout()
    st.rerun()


def _google_button() -> None:
    provider = google_provider()
    if provider is False:
        return
    if _google_logged_in() and st.session_state.get("login_error"):
        if st.button("Usar otra cuenta de Google", icon=":material/switch_account:", width="stretch"):
            st.session_state.pop("google_checked", None)
            st.session_state.pop("login_error", None)
            st.logout()
        return
    if st.button("Ingresar con Google", icon=":material/account_circle:", width="stretch", key="google_login"):
        st.login(provider) if provider else st.login()
    st.markdown(f'<div style="text-align:center;color:{MUTED};font-size:0.8rem;margin:0.2rem 0 0.6rem">'
                'o con tu usuario del hospital</div>', unsafe_allow_html=True)


def _signup() -> None:
    """Paciente: crea su cuenta con su documento y el correo que dio en admisiones. El código llega a ese correo."""
    with st.expander("Soy paciente: crear mi cuenta"):
        step = st.session_state.get("su_step", 1)
        if step == 1:
            st.caption("Usa el número de documento y el correo que registraste en admisiones o facturación. "
                       "Si no diste correo, pide que lo registren en tu próxima visita.")
            with st.form("su_request"):
                doc = st.text_input("Número de documento")
                mail = st.text_input("Correo")
                if st.form_submit_button("Enviarme el código", width="stretch") and doc.strip() and mail.strip():
                    try:
                        pid, code = auth.request_signup(ctx.get_clin(), doc, mail, ctx.clock())
                    except Exception as exc:
                        st.error(str(exc))
                        return
                    sent = bool(pid) and mailer.send_signup_code(mail.strip().lower(), code, auth.CODE_MINUTES)
                    st.session_state.su_data = (doc.strip(), mail.strip())
                    st.session_state.su_demo_code = None if (sent or not pid) else code
                    st.session_state.su_step = 2
                    st.rerun()
            return
        st.info(auth.SIGNUP_GENERIC + " Si no te llega, puede que aún no estés registrado: pide tu registro en "
                "“No estoy registrado en el hospital”.")
        demo = st.session_state.get("su_demo_code")
        if demo:
            st.warning(f"Modo demostración (sin correo configurado): tu código es **{demo}**")
        doc, mail = st.session_state.get("su_data", ("", ""))
        with st.form("su_finish"):
            code = st.text_input("Código", max_chars=6)
            new = st.text_input("Crea tu contraseña", type="password", help="Mínimo 8 caracteres, con letras y números")
            again = st.text_input("Repite la contraseña", type="password")
            if st.form_submit_button("Crear mi cuenta", type="primary", width="stretch"):
                if new != again:
                    st.error("Las contraseñas no coinciden.")
                else:
                    try:
                        auth.complete_signup(ctx.get_clin(), doc, mail, code, new, ctx.clock())
                    except Exception as exc:
                        st.error(str(exc))
                    else:
                        for k in ("su_step", "su_data", "su_demo_code"):
                            st.session_state.pop(k, None)
                        _do_login(mail.strip().lower(), new)
        if st.button("Volver", key="su_back"):
            for k in ("su_step", "su_data", "su_demo_code"):
                st.session_state.pop(k, None)
            st.rerun()


def _registration() -> None:
    """Persona que no está en el hospital: pide su registro; gerencia la cita para ir en persona."""
    import clinical_records as cr
    import requests_service as rq
    with st.expander("No estoy registrado en el hospital"):
        mode = st.segmented_control("Opción", ["Pedir registro", "Consultar mi solicitud"], default="Pedir registro",
                                    key="reg_mode", label_visibility="collapsed")
        if mode == "Consultar mi solicitud":
            with st.form("reg_status"):
                doc = st.text_input("Número de documento", key="reg_s_doc")
                mail = st.text_input("Correo", key="reg_s_mail")
                if st.form_submit_button("Consultar", width="stretch"):
                    row = rq.registration_status(ctx.get_clin(), doc, mail)
                    if row is None:
                        st.warning("No encontramos una solicitud con ese documento y ese correo.")
                    elif row["estado"] == "PENDIENTE":
                        st.info("Tu solicitud está en revisión. Te responderemos con el día y la hora para ir al "
                                "hospital.")
                    elif row["estado"] == "CITADA":
                        when = row["cita_fecha_hora"]
                        st.success(f"Te esperamos el **{when[8:10]}/{when[5:7]} a las {when[11:16]}** en "
                                   f"**{row['cita_lugar']}** con tu documento de identidad original. Allí te "
                                   "registran y te crean la cuenta." + (f" {row['respuesta']}" if row["respuesta"] else ""))
                    else:
                        st.error(f"Tu solicitud no fue aprobada: {row['respuesta']}")
            return
        st.caption("Déjanos tus datos. Gerencia te indicará qué día y a qué hora ir al hospital para registrarte y "
                   "crear tu cuenta.")
        with st.form("reg_request", clear_on_submit=False):
            c1, c2 = st.columns(2)
            nombres = c1.text_input("Nombres")
            apellidos = c2.text_input("Apellidos")
            c3, c4 = st.columns([1, 1.4])
            tipo = c3.selectbox("Documento", list(cr.DOC_TYPES), format_func=lambda k: k)
            doc = c4.text_input("Número")
            mail = st.text_input("Correo")
            phone = st.text_input("Celular (WhatsApp)")
            if st.form_submit_button("Enviar solicitud", type="primary", width="stretch"):
                try:
                    rq.create_registration(ctx.get_clin(), nombres=nombres, apellidos=apellidos, tipo_documento=tipo,
                                           numero_documento=doc, correo=mail, telefono=phone, now=ctx.clock())
                    st.success("Recibimos tu solicitud. Consulta la respuesta aquí mismo (“Consultar mi solicitud”) "
                               "o espera nuestro mensaje.")
                except Exception as exc:
                    st.error(str(exc))


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
    _google_callback()
    _google_button()
    with st.form("login", border=True):
        user = st.text_input("Usuario", placeholder="p. ej. dra.ruiz")
        pwd = st.text_input("Contraseña", type="password")
        if st.form_submit_button("Ingresar", type="primary", width="stretch"):
            _do_login(user, pwd)
    if st.session_state.get("login_error"):
        st.error(st.session_state.login_error)
    _recovery()
    _signup()
    _registration()
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
            end_session()


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
        end_session()
