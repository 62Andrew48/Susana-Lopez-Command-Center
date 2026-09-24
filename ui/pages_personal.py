"""
ui/pages_personal.py — Administración del personal: usuarios (crear, suspender, restablecer clave) y turnos de
trabajo de las personas a cargo (asignar por días, ver la semana, quitar turnos que aún no empiezan).
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pandas as pd
import streamlit as st

import clinical_records as cr
import staff
from ui import context as ctx
from ui.theme import MUTED, chip, esc

ROLE_LABEL = {"ADMIN": "Administrador", "DOCTOR": "Médico", "ENFERMERIA": "Enfermería", "PACIENTE": "Paciente",
              "FACTURACION": "Facturación y admisiones"}
STATE_TONE = {"ACTIVO": "ok", "SUSPENDIDO": "warn", "INACTIVO": "neutral", "PENDIENTE_ACTIVACION": "info"}


def page_personal() -> None:
    tabs, views = [], []
    if ctx.can("personal.turnos"):
        tabs += ["Cobertura ahora", "Turnos del personal"]; views += [_coverage_tab, _shifts_tab]
    if ctx.can("usuarios.administrar"):
        tabs.append("Usuarios"); views.append(_users_tab)
    for tab, view in zip(st.tabs(tabs), views):
        with tab:
            view()


# ---------------------------------------------------------------------------
# Cobertura: pacientes por servicio frente a quién está de turno
# ---------------------------------------------------------------------------
def _coverage_tab() -> None:
    rows = staff.coverage(ctx.get_clin(), ctx.live_beds(), ctx.clock())
    hint = staff.reassignment_hint(rows)
    if hint:
        st.warning(hint, icon=":material/swap_horiz:")
    df = pd.DataFrame([{"Servicio": r["servicio"], "Pacientes": r["pacientes"],
                        "Médicos de turno": ", ".join(r["medicos"]) or "—",
                        "Enfermería de turno": ", ".join(r["enfermeria"]) or "Nadie",
                        "Pacientes por persona de enfermería": r["por_enfermera"]} for r in rows])
    st.dataframe(df, hide_index=True, width="stretch", column_config={
        "Pacientes por persona de enfermería": st.column_config.ProgressColumn(
            format="%.1f", min_value=0, max_value=float(max([r["por_enfermera"] or 0 for r in rows] + [1])))})
    st.caption(f"Hora del reloj clínico: {ctx.clock()[11:16]}. Pacientes = camas ocupadas del servicio (incluidas las de "
               "expansión). La app compara servicios entre sí y no asume un estándar de dotación. La plantilla de la "
               "demo es parcial (personal sintético); con la plantilla real, esta tabla muestra la carga real.")


# ---------------------------------------------------------------------------
# Turnos
# ---------------------------------------------------------------------------
def _today() -> date:
    return date.fromisoformat(ctx.clock()[:10])


def _shifts_tab() -> None:
    clin = ctx.get_clin()
    people = staff.staff_members(clin)
    names = {p["id"]: f"{p['nombre_mostrado']} · {ROLE_LABEL[p['rol']]}" for p in people}
    msg = st.session_state.pop("shift_msg", None)
    if msg:
        st.success(msg)
    with st.expander("Asignar turnos", icon=":material/calendar_add_on:", expanded=True):
        with st.form("assign_shift"):
            c1, c2 = st.columns(2)
            who = c1.selectbox("Persona", list(names), format_func=names.get)
            service = c2.selectbox("Servicio", staff.SERVICES)
            template = st.selectbox("Turno", list(staff.SHIFT_TEMPLATES))
            c3, c4 = st.columns(2)
            start = c3.date_input("Desde", value=_today() + timedelta(days=1), min_value=_today(), format="DD/MM/YYYY")
            end = c4.date_input("Hasta", value=_today() + timedelta(days=5), min_value=_today(), format="DD/MM/YYYY")
            weekdays = st.multiselect("Días de la semana", ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"],
                                      default=["lun", "mar", "mié", "jue", "vie"])
            skip = st.checkbox("Saltar los días que ya tiene turno", value=True,
                               help="Varias personas pueden cubrir la misma área; solo se evita que una misma persona "
                                    "quede en dos turnos a la vez")
            if st.form_submit_button("Asignar", type="primary", icon=":material/check:"):
                idx = {d: i for i, d in enumerate(["lun", "mar", "mié", "jue", "vie", "sáb", "dom"])}
                wanted = {idx[w] for w in weekdays}
                days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)
                        if (start + timedelta(days=i)).weekday() in wanted]
                if not days:
                    st.error("Elige un rango con al menos un día de la semana marcado.")
                else:
                    try:
                        res = staff.assign_shifts(clin, admin_id=ctx.user_id(), user_id=who, servicio=service,
                                                  template=template, days=days, now=ctx.clock(), skip_conflicts=skip)
                        n, skipped = res if skip else (res, [])
                        msg = f"{n} turno(s) asignados a {names[who]}"
                        if skipped:
                            msg += " · ya tenía turno: " + ", ".join(f"{d[8:10]}/{d[5:7]}" for d in skipped)
                        st.session_state.shift_msg = msg
                        st.rerun()
                    except sqlite3.IntegrityError as exc:
                        st.error(str(exc))
    c1, c2 = st.columns([1, 2])
    week0 = c1.date_input("Semana desde", value=_today(), format="DD/MM/YYYY", key="sh_week")
    who_f = c2.selectbox("Filtrar persona", [None, *names], format_func=lambda x: "Todo el personal" if x is None
                         else names[x], key="sh_who")
    days = [week0 + timedelta(days=i) for i in range(7)]
    rows = staff.shifts(clin, days[0].isoformat(), days[-1].isoformat(), who_f)
    if not rows:
        st.info("Sin turnos en esa semana.")
        return
    grid = {}
    for r in rows:
        label = f"{r['inicio'][11:16]}-{r['fin'][11:16]} {r['servicio']}" + (" (guardia)" if r["tipo"] == "GUARDIA" else "")
        grid.setdefault(r["nombre_mostrado"], {}).setdefault(r["inicio"][:10], []).append(label)
    abbr = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
    table = pd.DataFrame([{"Persona": person, **{f"{abbr[d.weekday()]} {d:%d/%m}": " · ".join(by_day.get(d.isoformat(), [])) or "—"
                                                  for d in days}} for person, by_day in grid.items()])
    st.dataframe(table, hide_index=True, width="stretch")
    st.caption("El turno es lo que habilita el acceso clínico: fuera de turno se requiere “romper el vidrio”.")
    future = [r for r in rows if r["inicio"] > ctx.clock()]
    if future:
        with st.expander("Quitar un turno (solo los que no han empezado)", icon=":material/event_busy:"):
            opts = {r["id"]: f"{r['nombre_mostrado']} · {r['inicio'][:16]} → {r['fin'][11:16]} · {r['servicio']}"
                    for r in future}
            sid = st.selectbox("Turno", list(opts), format_func=opts.get, key="sh_del")
            if st.button("Quitar turno", key="sh_del_btn"):
                try:
                    staff.delete_shift(clin, ctx.user_id(), sid, ctx.clock())
                    st.rerun()
                except sqlite3.IntegrityError as exc:
                    st.error(str(exc))


# ---------------------------------------------------------------------------
# Usuarios
# ---------------------------------------------------------------------------
def _users_tab() -> None:
    clin = ctx.get_clin()
    shown = st.session_state.pop("new_user_pwd", None)
    if shown:
        st.success(f"Cuenta **{shown[0]}** lista. Contraseña temporal: **`{shown[1]}`** (se muestra una sola vez; "
                   "deberá cambiarla al entrar).")
    with st.expander("Crear usuario", icon=":material/person_add:"):
        rol = st.selectbox("Rol", list(ROLE_LABEL), format_func=ROLE_LABEL.get, key="nu_rol")
        with st.form("new_user"):
            c1, c2 = st.columns(2)
            usuario = c1.text_input("Usuario", placeholder="nombre.apellido")
            nombre = c2.text_input("Nombre para mostrar", placeholder="Dr. Pérez")
            correo = st.text_input("Correo (para recuperar la contraseña)")
            registro = especialidad = None
            pid = None
            if rol in ("DOCTOR", "ENFERMERIA"):
                c3, c4 = st.columns(2)
                registro = c3.text_input("Registro profesional (ReTHUS)")
                if rol == "DOCTOR":
                    especialidad = c4.text_input("Especialidad", placeholder="MEDICINA GENERAL")
            if rol == "PACIENTE":
                q = st.text_input("Documento o nombre del paciente registrado")
                found = cr.search_patients(clin, q, limit=10) if q else []
                opts = {p["id_paciente"]: cr.display_name(p) for p in found}
                pid = st.selectbox("Paciente", list(opts), format_func=opts.get) if opts else None
            if st.form_submit_button("Crear cuenta", type="primary"):
                try:
                    pwd = staff.create_user(clin, admin_id=ctx.user_id(), usuario=usuario, nombre=nombre, rol=rol,
                                            correo=correo, registro=registro, especialidad=especialidad,
                                            id_paciente=pid, now=ctx.clock())
                    st.session_state.new_user_pwd = (usuario.strip().lower(), pwd)
                    st.rerun()
                except sqlite3.IntegrityError as exc:
                    msg = str(exc)
                    st.error("Médicos y enfermería necesitan registro profesional (ReTHUS)" if "ReTHUS" in msg else msg)
    rows = staff.users(clin)
    df = pd.DataFrame([{"id": u["id"], "Usuario": u["usuario"], "Nombre": u["nombre_mostrado"],
                        "Rol": ROLE_LABEL.get(u["rol"], u["rol"]), "Estado": u["estado_cuenta"].capitalize(),
                        "Correo": u["correo"] or "—", "Último acceso": (u["ultimo_acceso"] or "—")[:16],
                        "Cambio de clave pendiente": "Sí" if u["debe_cambiar_clave"] else ""} for u in rows])
    event = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                         key="users_table", column_config={"id": None})
    sel = event.selection.rows if event and event.selection else []
    if not sel:
        st.caption("Selecciona una cuenta para suspenderla, reactivarla o restablecer su contraseña.")
        return
    u = rows[sel[0]]
    st.markdown(f"**{esc(u['nombre_mostrado'])}** · {esc(ROLE_LABEL.get(u['rol'], u['rol']))} "
                f"{chip(u['estado_cuenta'].capitalize(), STATE_TONE.get(u['estado_cuenta'], 'neutral'))}",
                unsafe_allow_html=True)
    c1, c2, c3 = st.columns([2, 1, 1], vertical_alignment="bottom")
    reason = c1.text_input("Motivo", key=f"u_reason_{u['id']}", placeholder="Vacaciones, retiro, incapacidad…")
    target = "ACTIVO" if u["estado_cuenta"] != "ACTIVO" else "SUSPENDIDO"
    if c2.button("Reactivar" if target == "ACTIVO" else "Suspender", key=f"u_state_{u['id']}", width="stretch"):
        try:
            staff.set_status(clin, ctx.user_id(), u["id"], target, reason, ctx.clock())
            st.rerun()
        except sqlite3.IntegrityError as exc:
            st.error(str(exc))
    if c3.button("Restablecer clave", key=f"u_reset_{u['id']}", width="stretch"):
        pwd = staff.reset_password(clin, ctx.user_id(), u["id"], ctx.clock())
        st.session_state.new_user_pwd = (u["usuario"], pwd)
        st.rerun()
    st.markdown(f"<span style='color:{MUTED};font-size:0.8rem'>Las cuentas no se borran: se suspenden o inactivan para "
                "conservar la trazabilidad de la bitácora.</span>", unsafe_allow_html=True)
