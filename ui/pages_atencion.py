"""
ui/pages_atencion.py — Citas y turnos de atención.

  * Admisiones y facturación (citas.gestionar): agendar para cualquier paciente, agenda del día con llegada
    (check-in), cancelar, y la fila de turnos por servicio (dar turno, llamar al siguiente, pantalla de turnos).
  * Médico (citas.agenda): su agenda, "llamar al siguiente" en consulta, atendida / no asistió, abrir la ficha.
  * Paciente (portal): agendar y cancelar sus citas, registrar su llegada el día de la cita y ver su turno con
    cuántas personas tiene antes.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

import clinical_records as cr
import config
import requests_service as rq
import scheduling as sch
from ui import context as ctx
from ui.theme import BORDER, MUTED, TEXT, banner, chip, esc, section_title

DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
STATE = {"PROGRAMADA": ("Programada", "info"), "CUMPLIDA": ("Atendida", "ok"), "NO_ASISTIO": ("No asistió", "danger"),
         "CANCELADA": ("Cancelada", "neutral")}
TICKET_STATE = {"EN_ESPERA": "En espera", "LLAMADO": "Llamado", "EN_ATENCION": "En atención", "ATENDIDO": "Atendido",
                "NO_SE_PRESENTO": "No se presentó"}
CSS = f"""
<style>
  .tk {{background:#FFF; border:1px solid {BORDER}; border-radius:14px; padding:0.9rem 1.1rem; margin-bottom:0.6rem;
      display:flex; align-items:center; gap:1.1rem;}}
  .tk-code {{font-size:2.3rem; font-weight:800; color:#2E3378; letter-spacing:.04em; font-variant-numeric:tabular-nums;}}
  .tk.called {{border:2px solid #507643; background:#F0F7EC;}}
  .tk small {{color:{MUTED};}}
  .board {{display:grid; grid-template-columns:repeat(auto-fill, minmax(190px, 1fr)); gap:0.6rem;}}
  .board .cell {{background:#FFF; border:1px solid {BORDER}; border-radius:12px; padding:0.6rem 0.8rem;}}
  .board .cell.called {{background:#2E3378; color:#FFF; border-color:#2E3378;}}
  .board .cell b {{font-size:1.6rem; display:block;}}
  .board .cell span {{font-size:0.8rem;}}
</style>
"""


def _ts(v: str | None) -> str:
    return f"{v[8:10]}/{v[5:7]} {v[11:16]}" if v else "—"


def _day_label(d: date) -> str:
    return f"{DAYS[d.weekday()].capitalize()} {d:%d/%m}"


def _today() -> date:
    return date.fromisoformat(ctx.clock()[:10])


# ===========================================================================
# Agendar (compartido: admisiones y paciente)
# ===========================================================================
def booking_form(id_paciente: int | None, key: str, allow_patient_pick: bool) -> None:
    clin = ctx.get_clin()
    if allow_patient_pick:
        q = st.text_input("Paciente", placeholder="Nombre, documento o id", key=f"{key}_pq")
        found = cr.search_patients(clin, q, limit=25)
        if not found:
            st.caption("No hay pacientes con ese criterio. Regístralo en Clínico y farmacia → Pacientes.")
            return
        names = {p["id_paciente"]: f"{cr.display_name(p)} (id {p['id_paciente']})" for p in found}
        id_paciente = st.selectbox("Selecciona el paciente", list(names), format_func=names.get, key=f"{key}_pid")
    specs = sch.specialties(clin)
    c1, c2 = st.columns(2)
    spec = c1.selectbox("Especialidad", specs, format_func=lambda s: s.title(), key=f"{key}_spec")
    docs = {d["id"]: d["nombre_mostrado"] for d in sch.doctors(clin, spec)}
    doc = c2.selectbox("Profesional", [None, *docs], format_func=lambda d: "Cualquiera disponible" if d is None else docs[d],
                       key=f"{key}_doc")
    today = _today()
    options = [today + timedelta(days=i) for i in range(0, 14)]
    day = st.pills("Día", options, format_func=_day_label, default=options[0], key=f"{key}_day")
    if day is None:
        return
    slots = sch.free_slots(clin, especialidad=spec, medico_id=doc, day=day.isoformat(), now=ctx.clock())
    if not slots:
        st.info("No hay cupos ese día. Prueba otro día u otro profesional.")
        return
    labels = {f"{s['fecha_hora']}|{s['medico_id']}": f"{s['fecha_hora'][11:16]} · {s['medico']}" for s in slots}
    pick = st.pills("Hora", list(labels), format_func=labels.get, key=f"{key}_slot")
    c3, c4 = st.columns([1, 2])
    motivo = c3.selectbox("Tipo de cita", list(sch.MOTIVOS), format_func=sch.MOTIVOS.get, key=f"{key}_mot")
    nota = c4.text_input("Motivo de la consulta (opcional)", key=f"{key}_nota", placeholder="Ej.: control de tensión")
    if st.button("Agendar cita", type="primary", icon=":material/event_available:", disabled=pick is None,
                 key=f"{key}_go"):
        when, medico = pick.split("|")
        try:
            sch.book(clin, id_paciente=int(id_paciente), medico_id=int(medico), fecha_hora=when, motivo=motivo,
                     creada_por=ctx.user_id(), now=ctx.clock(), nota=nota)
            st.toast(f"Cita agendada: {_ts(when)}", icon=":material/event_available:")
            for k in (f"{key}_slot",):
                st.session_state.pop(k, None)
            st.rerun()
        except sqlite3.IntegrityError as exc:
            st.error(str(exc))


# ===========================================================================
# Página "Citas y turnos" (personal)
# ===========================================================================
def page_atencion() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    tabs, views = [], []
    if ctx.can("citas.agenda"):
        tabs.append("Mi agenda"); views.append(_doctor_agenda)
    if ctx.can("citas.gestionar"):
        n = len(rq.pending_requests(ctx.get_clin()))
        tabs += [f"Solicitudes de pacientes ({n})" if n else "Solicitudes de pacientes", "Agendar cita",
                 "Citas del día"]
        views += [_requests_tab, _book_tab, _day_tab]
    if ctx.can("citas.gestionar"):  # la fila general y la pantalla de sala son de facturación/admisiones
        tabs += ["Fila de turnos", "Pantalla de turnos"]; views += [_queue_tab, _screen]
    for tab, view in zip(st.tabs(tabs), views):
        with tab:
            view()


def _slot_ok(ts: str, pref: str) -> bool:
    hour = int(ts[11:13])
    return pref == "CUALQUIERA" or (pref == "MANANA" and hour < 12) or (pref == "TARDE" and hour >= 12)


def _requests_tab() -> None:
    """Facturación: solicitudes que mandan los pacientes (portal o asistente). Agendar y avisar por WhatsApp."""
    clin = ctx.get_clin()
    done = st.session_state.pop("sol_done", None)
    if done:
        banner(f"<b>Cita agendada</b> para {esc(done['paciente'])}: {esc(done['cuando'])}. Avísale por WhatsApp.", "ok")
        if done["link"]:
            st.link_button("Enviar confirmación por WhatsApp", done["link"], icon=":material/chat:", type="primary")
        else:
            st.caption("El paciente no tiene un celular válido registrado; la cita ya le aparece en su portal.")
    rows = rq.pending_requests(clin)
    if not rows:
        st.info("No hay solicitudes pendientes. Llegan cuando un paciente pide cita desde el portal o el asistente.")
        return
    st.caption("Lee qué le pasa al paciente, agéndale con el profesional adecuado y escríbele por WhatsApp. "
               "Si no hace falta cita (o debe ir por otra vía), respóndele sin agendar: lo verá en su portal.")
    specs = sch.specialties(clin)
    for r in rows:
        waited = (datetime.strptime(ctx.clock(), sch.FMT) - datetime.strptime(r["creada_en"], sch.FMT))
        hours = int(waited.total_seconds() // 3600)
        with st.container(border=True):
            flags = chip("Posible urgencia", "danger") if rq.is_emergency(r["sintomas"]) else ""
            st.markdown(f"**{esc(r['paciente'])}** · {esc(r['tipo_documento'] or '')} {esc(r['numero_documento'] or '')} "
                        f"· {chip(rq.TYPES[r['tipo']], 'info')} {chip(rq.PREFERENCES[r['preferencia']], 'neutral')} "
                        f"{chip('por el asistente' if r['canal'] == 'ASISTENTE' else 'por el portal', 'neutral')} "
                        f"{flags}<br><span class='muted'>Hace {hours} h · Contacto: {esc(r['contacto'] or 'sin teléfono')}"
                        f"{' · contactado ' + _ts(r['contactado_en']) if r['contactado_en'] else ''}</span><br>"
                        f"“{esc(r['sintomas'])}”", unsafe_allow_html=True)
            link = rq.whatsapp_link(r["contacto"], rq.whatsapp_message(r))
            a, b, c = st.columns(3)
            if link:
                a.link_button("Escribir por WhatsApp", link, icon=":material/chat:", width="stretch")
                if b.button("Marcar contactado", key=f"solc_{r['id']}", width="stretch", disabled=bool(r["contactado_en"])):
                    rq.mark_contacted(clin, r["id"], ctx.clock())
                    st.rerun()
            else:
                a.caption("Sin celular válido para WhatsApp")
            with c.popover("Responder sin agendar", width="stretch"):
                answer = st.text_area("Respuesta para el paciente", key=f"sola_{r['id']}",
                                      placeholder="Ej.: acérquese a consulta externa el lunes a las 7:00 con su documento")
                if st.button("Enviar respuesta", key=f"solr_{r['id']}"):
                    try:
                        rq.close_request(clin, r["id"], respuesta=answer, by=ctx.user_id(), now=ctx.clock())
                        st.rerun()
                    except sqlite3.IntegrityError as exc:
                        st.error(str(exc))
            msgs = rq.messages(clin, r["id"])
            waiting = bool(msgs) and msgs[-1]["lado"] == "PACIENTE"   # el paciente espera respuesta
            label = f"Mensajes con el paciente ({len(msgs)})" if msgs else "Escribirle al paciente"
            with st.expander(label + (" · espera tu respuesta" if waiting else ""), icon=":material/forum:",
                             expanded=waiting):
                request_thread(clin, r, "FACTURACION", "fac")
            with st.expander("Agendar la cita (asignar profesional)", icon=":material/event_available:"):
                default = rq.suggested_specialty(r, specs)
                k1, k2 = st.columns(2)
                spec = k1.selectbox("Especialidad", specs, index=specs.index(default), format_func=lambda x: x.title(),
                                    key=f"sols_{r['id']}")
                motivo = k2.selectbox("Tipo de cita", list(sch.MOTIVOS), format_func=sch.MOTIVOS.get,
                                      index=1 if r["tipo"] in ("CONTROL", "RESULTADOS") else 0, key=f"solm_{r['id']}")
                options = [_today() + timedelta(days=i) for i in range(14)]
                day = st.pills("Día", options, format_func=_day_label, default=options[0], key=f"sold_{r['id']}")
                if day is None:
                    continue
                slots = [x for x in sch.free_slots(clin, especialidad=spec, day=day.isoformat(), now=ctx.clock())
                         if _slot_ok(x["fecha_hora"], r["preferencia"])]
                if not slots:
                    st.caption(f"Sin cupos ese día {rq.PREFERENCES[r['preferencia']].lower()}. Prueba otro día.")
                    continue
                labels = {f"{x['fecha_hora']}|{x['medico_id']}": f"{x['fecha_hora'][11:16]} · {x['medico']}"
                          for x in slots}
                pick = st.pills("Hora", list(labels), format_func=labels.get, key=f"solh_{r['id']}")
                if st.button("Agendar y preparar WhatsApp", type="primary", disabled=pick is None,
                             key=f"solg_{r['id']}", icon=":material/event_available:"):
                    when, medico = pick.split("|")
                    try:
                        cita_id = rq.schedule_request(clin, r["id"], medico_id=int(medico), fecha_hora=when,
                                                      motivo=motivo, by=ctx.user_id(), now=ctx.clock())
                        cita = next(x for x in sch.appointments(clin, day_from=when[:10], day_to=when[:10],
                                                                id_paciente=r["id_paciente"]) if x["id"] == cita_id)
                        st.session_state.sol_done = {"paciente": r["paciente"], "cuando": f"{_ts(when)} con {cita['medico']}",
                                                     "link": rq.whatsapp_link(r["contacto"], rq.whatsapp_message(r, cita))}
                        st.rerun()
                    except sqlite3.IntegrityError as exc:
                        st.error(str(exc))


def _book_tab() -> None:
    booking_form(None, "adm", allow_patient_pick=True)


def _cancel_popover(container, c, key: str, only_patient: int | None = None) -> None:
    with container.popover("Cancelar", icon=":material/event_busy:", width="stretch"):
        with st.form(f"cancel_{key}_{c['id']}"):
            reason = st.text_input("Motivo")
            if st.form_submit_button("Cancelar cita"):
                try:
                    sch.cancel(ctx.get_clin(), c["id"], ctx.user_id(), reason, ctx.clock(), only_patient=only_patient)
                    st.rerun()
                except sqlite3.IntegrityError as exc:
                    st.error(str(exc))


def _day_tab() -> None:
    clin = ctx.get_clin()
    day = st.date_input("Día", value=_today(), format="DD/MM/YYYY", key="day_tab_date")
    rows = sch.appointments(clin, day_from=day.isoformat(), day_to=day.isoformat())
    if not rows:
        st.info("No hay citas ese día.")
        return
    st.markdown(" ".join(chip(f"{sum(r['estado'] == k for r in rows)} {v[0].lower()}", v[1])
                         for k, v in STATE.items() if any(r["estado"] == k for r in rows)), unsafe_allow_html=True)
    for c in rows:
        with st.container(border=True):
            a, b, d = st.columns([3, 1.2, 1.2], vertical_alignment="center")
            label, tone = STATE[c["estado"]]
            ticket = f" · turno <b>{c['turno']}</b> ({TICKET_STATE.get(c['turno_estado'], '')})" if c["turno"] else ""
            a.markdown(f"**{c['fecha_hora'][11:16]}** · {esc(c['paciente'])} · {esc(c['medico'])} "
                       f"{chip(label, tone)}<br><span style='color:{MUTED};font-size:0.82rem'>"
                       f"{esc(sch.MOTIVOS.get(c['motivo'], c['motivo'].replace('_', ' ').lower()))}"
                       f"{' · ' + esc(c['nota']) if c['nota'] else ''}{ticket}</span>", unsafe_allow_html=True)
            if c["estado"] == "PROGRAMADA":
                if not c["turno"] and c["fecha_hora"][:10] == ctx.clock()[:10]:
                    prio = b.checkbox("Prioridad", key=f"prio_{c['id']}", help="Adulto mayor, gestante, discapacidad")
                    if b.button("Llegó: dar turno", key=f"ci_{c['id']}", width="stretch", type="primary"):
                        try:
                            code = sch.check_in(clin, c["id"], ctx.user_id(), ctx.clock(), prioridad=prio)
                            st.toast(f"Turno {code}", icon=":material/confirmation_number:")
                            st.rerun()
                        except sqlite3.IntegrityError as exc:
                            st.error(str(exc))
                _cancel_popover(d, c, "adm")


def _queue_tab() -> None:
    clin = ctx.get_clin()
    c1, c2 = st.columns(2)
    service = c1.segmented_control("Servicio", list(sch.SERVICES), default="CONSULTA",
                                   format_func=lambda s: sch.SERVICE_LABEL[s], key="q_service") or "CONSULTA"
    module = c2.text_input("Módulo o consultorio", value="Ventanilla 1" if service != "CONSULTA" else "Consultorio 1",
                           key=f"q_mod_{service}")
    with st.expander("Dar turno sin cita", icon=":material/confirmation_number:"):
        q = st.text_input("Paciente", placeholder="Nombre, documento o id", key="q_pq")
        found = cr.search_patients(clin, q, limit=20)
        if found:
            names = {p["id_paciente"]: f"{cr.display_name(p)} (id {p['id_paciente']})" for p in found}
            pid = st.selectbox("Selecciona", list(names), format_func=names.get, key="q_pid")
            prio = st.checkbox("Atención prioritaria (adulto mayor, gestante, discapacidad)", key="q_prio")
            if st.button("Dar turno", type="primary", key="q_issue"):
                try:
                    code = sch.issue_ticket(clin, servicio=service, id_paciente=pid, by=ctx.user_id(), now=ctx.clock(),
                                            prioridad=prio)
                    st.success(f"Turno **{code}** para {names[pid]}")
                except sqlite3.IntegrityError as exc:
                    st.error(str(exc))
    medico = ctx.user_id() if (service == "CONSULTA" and ctx.can("citas.agenda")) else None
    if st.button("Llamar al siguiente", icon=":material/campaign:", type="primary", key="q_next"):
        t = sch.call_next(clin, servicio=service, modulo=module, by=ctx.user_id(), now=ctx.clock(), medico_id=medico)
        if t:
            st.toast(f"Llamando {t['codigo']} a {module}", icon=":material/campaign:")
        else:
            st.toast("No hay nadie en espera", icon=":material/info:")
    _queue_list(service)


def _queue_list(service: str) -> None:
    clin = ctx.get_clin()
    rows = [r for r in sch.board(clin, ctx.clock()[:10], service) if r["estado"] in ("EN_ESPERA", "LLAMADO", "EN_ATENCION")]
    if not rows:
        st.caption("Fila vacía.")
        return
    for t in rows:
        a, b, c = st.columns([3, 1, 1], vertical_alignment="center")
        prio = " " + chip("Prioritario", "warn") if t["prioridad"] else ""
        extra = f" · cita {t['cita_hora'][11:16]} con {esc(t['medico'])}" if t["cita_hora"] else ""
        a.markdown(f"**{t['codigo']}** · {esc(t['paciente'])}{prio} {chip(TICKET_STATE[t['estado']], 'info')}"
                   f"<br><span style='color:{MUTED};font-size:0.8rem'>llegó {t['creado_en'][11:16]}{extra}"
                   f"{' · ' + esc(t['modulo']) if t['modulo'] else ''}</span>", unsafe_allow_html=True)
        if t["estado"] != "EN_ESPERA":
            if b.button("Atendido", key=f"t_done_{t['id']}", width="stretch"):
                sch.set_ticket_state(clin, t["id"], "ATENDIDO", ctx.user_id(), ctx.clock())
                st.rerun()
            if c.button("No se presentó", key=f"t_ns_{t['id']}", width="stretch"):
                sch.set_ticket_state(clin, t["id"], "NO_SE_PRESENTO", ctx.user_id(), ctx.clock())
                st.rerun()


def _screen() -> None:
    """Lo que se proyecta en la sala de espera: sin nombres, solo códigos (privacidad)."""
    rows = sch.board(ctx.get_clin(), ctx.clock()[:10])
    called = [r for r in rows if r["estado"] == "LLAMADO"]
    waiting = [r for r in rows if r["estado"] == "EN_ESPERA"]
    st.caption("Para proyectar en la sala de espera: muestra códigos, no nombres.")
    cells = [f'<div class="cell called"><b>{r["codigo"]}</b><span>{esc(r["modulo"] or "")}</span></div>' for r in called]
    cells += [f'<div class="cell"><b>{r["codigo"]}</b><span>{sch.SERVICE_LABEL[r["servicio"]]} · en espera</span></div>'
              for r in waiting[:12]]
    st.markdown(f'<div class="board">{"".join(cells)}</div>' if cells else "Sin turnos activos.",
                unsafe_allow_html=True)


def _doctor_agenda() -> None:
    clin = ctx.get_clin()
    today = _today()
    rng = st.segmented_control("Ver", ["Hoy", "Próximos 7 días"], default="Hoy", key="ag_rng") or "Hoy"
    end = today if rng == "Hoy" else today + timedelta(days=7)
    rows = sch.appointments(clin, day_from=today.isoformat(), day_to=end.isoformat(), medico_id=ctx.user_id(),
                            states=("PROGRAMADA", "CUMPLIDA", "NO_ASISTIO"))
    c1, c2 = st.columns([1.3, 3], vertical_alignment="center")
    module = c2.text_input("Consultorio", value="Consultorio 1", key="ag_mod", label_visibility="collapsed")
    if c1.button("Llamar al siguiente", icon=":material/campaign:", type="primary", width="stretch"):
        t = sch.call_next(clin, servicio="CONSULTA", modulo=module, by=ctx.user_id(), now=ctx.clock(),
                          medico_id=ctx.user_id())
        st.toast(f"Llamando {t['codigo']}" if t else "Nadie en espera para ti", icon=":material/campaign:")
    mine = [t for t in sch.board(clin, ctx.clock()[:10], "CONSULTA")
            if t["estado"] in ("EN_ESPERA", "LLAMADO", "EN_ATENCION") and t["medico"] == ctx.current_user()["nombre_mostrado"]]
    if mine:
        st.markdown("**Tus pacientes en la sala de espera:** " + " ".join(
            chip(f"{t['codigo']} · {t['paciente']}" + (" · llamado" if t["estado"] == "LLAMADO" else ""),
                 "ok" if t["estado"] == "LLAMADO" else "info") for t in mine), unsafe_allow_html=True)
    if not rows:
        st.info("No tienes citas en este rango.")
        return
    for c in rows:
        with st.container(border=True):
            a, b, d, e = st.columns([3, 1, 1, 1.1], vertical_alignment="center")
            label, tone = STATE[c["estado"]]
            arrived = f" · llegó, turno <b>{c['turno']}</b>" if c["turno"] else ""
            a.markdown(f"**{_ts(c['fecha_hora'])}** · {esc(c['paciente'])} {chip(label, tone)}<br>"
                       f"<span style='color:{MUTED};font-size:0.82rem'>{esc(sch.MOTIVOS.get(c['motivo'], c['motivo']))}"
                       f"{' · ' + esc(c['nota']) if c['nota'] else ''}{arrived}</span>", unsafe_allow_html=True)
            if e.button("Ficha", key=f"ag_open_{c['id']}", icon=":material/folder_open:", width="stretch"):
                st.session_state.clin_patient = c["id_paciente"]
                st.switch_page(ctx.PAGES["clinico"])
            if c["estado"] == "PROGRAMADA" and c["fecha_hora"][:10] == ctx.clock()[:10]:
                if b.button("Atendida", key=f"ag_ok_{c['id']}", width="stretch"):
                    sch.set_outcome(clin, c["id"], ctx.user_id(), True, ctx.clock())
                    st.rerun()
                if d.button("No asistió", key=f"ag_ns_{c['id']}", width="stretch"):
                    sch.set_outcome(clin, c["id"], ctx.user_id(), False, ctx.clock())
                    st.rerun()


# ===========================================================================
# Portal del paciente
# ===========================================================================
def patient_ticket_banner(id_paciente: int) -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    for t in sch.patient_tickets(ctx.get_clin(), id_paciente, ctx.clock()):
        service = sch.SERVICE_LABEL[t["servicio"]]
        if t["estado"] == "LLAMADO":
            msg = f"<b>¡Te están llamando!</b> Acércate a <b>{esc(t['modulo'] or service)}</b>."
            cls = "tk called"
        elif t["estado"] == "EN_ATENCION":
            msg, cls = "Estás siendo atendido.", "tk"
        else:
            ahead = t["antes"]
            msg = ("Eres el siguiente." if ahead == 0 else f"Hay <b>{ahead}</b> persona{'s' if ahead != 1 else ''} antes "
                   "de ti.") + (" Tienes atención prioritaria." if t["prioridad"] else "")
            cls = "tk"
        st.markdown(f'<div class="{cls}"><div class="tk-code">{t["codigo"]}</div><div>{esc(service)}<br>{msg}<br>'
                    f'<small>Tomado a las {t["creado_en"][11:16]}</small></div></div>', unsafe_allow_html=True)


ROW_COLORS = {"CUMPLIDA": "background-color:#DCFCE7;color:#14532D", "NO_ASISTIO": "background-color:#FEE2E2;color:#7F1D1D",
              "CANCELADA": "color:#6B7280"}      # PROGRAMADA: sin color (activa, aún no ha ido)


def colored_history(rows) -> None:
    """Historial de citas con colores: verde atendida, rojo no asistió, gris cancelada, sin color pendiente."""
    df = pd.DataFrame([{"Fecha": _ts(c["fecha_hora"]), "Especialidad": c["especialidad"].title(),
                        "Profesional": c["medico"], "Estado": STATE[c["estado"]][0], "_e": c["estado"]} for c in rows])
    styles = df["_e"].map(lambda e: ROW_COLORS.get(e, ""))
    styled = df.drop(columns="_e").style.apply(lambda r: [styles[r.name]] * len(r), axis=1)
    st.markdown(" ".join([chip("Atendida", "ok"), chip("Programada", "neutral"), chip("No asistió", "danger"),
                          chip("Cancelada", "neutral")]), unsafe_allow_html=True)
    st.dataframe(styled, hide_index=True, width="stretch", height=min(38 * (len(df) + 1), 380))


def request_thread(clin, request, me: str, key: str, id_paciente: int | None = None) -> None:
    """Conversación de una solicitud (paciente ↔ facturación) y caja para escribir."""
    rows = rq.messages(clin, request["id"])
    rq.mark_read(clin, request["id"], me)
    bubbles = []
    for m in rows:
        mine = m["lado"] == me
        who = "Tú" if mine else ("Facturación" + (f" · {m['autor']}" if m["autor"] else "") if m["lado"] == "FACTURACION"
                                 else "Paciente")
        align, bg = ("flex-end", "#E0E7FF") if mine else ("flex-start", "#F1F5F9")
        bubbles.append(f'<div style="display:flex;justify-content:{align};margin:0.25rem 0">'
                       f'<div style="background:{bg};border-radius:12px;padding:0.4rem 0.7rem;max-width:80%">'
                       f'<div style="font-size:0.72rem;color:{MUTED}">{esc(who)} · {_ts(m["fecha"])}</div>'
                       f'<div style="font-size:0.9rem">{esc(m["texto"])}</div></div></div>')
    if bubbles:
        st.markdown("".join(bubbles), unsafe_allow_html=True)
    if request["estado"] == "CANCELADA":
        return
    target = "facturación" if me == "PACIENTE" else "el paciente"
    with st.form(f"msg_{key}_{request['id']}", clear_on_submit=True, border=False):
        c1, c2 = st.columns([5, 1.3], vertical_alignment="bottom")
        text = c1.text_input(f"Mensaje para {target}", placeholder="Escribe aquí…" if me == "PACIENTE" else
                             "Ej.: le asignamos medicina general mañana a las 8:00, ¿le sirve?")
        if c2.form_submit_button("Enviar", icon=":material/send:", width="stretch"):
            try:
                rq.send_message(clin, request["id"], lado=me, autor_id=ctx.user_id(), texto=text, now=ctx.clock(),
                                id_paciente=id_paciente)
                st.rerun()
            except sqlite3.IntegrityError as exc:
                st.error(str(exc))


def _request_form(clin, id_paciente: int) -> None:
    """El paciente cuenta qué le pasa; facturación le agenda y le escribe (no elige médico ni hora)."""
    patient = cr.get_patient(clin, id_paciente)
    with st.form("sol_cita", clear_on_submit=True):
        tipo = st.selectbox("¿Qué necesitas?", list(rq.TYPES), format_func=rq.TYPES.get)
        sintomas = st.text_area("Mensaje para facturación: cuéntanos qué te pasa",
                                placeholder="Ej.: tengo tos y fiebre desde hace 3 días", max_chars=600)
        c1, c2 = st.columns(2)
        pref = c1.selectbox("¿Cuándo puedes ir?", list(rq.PREFERENCES), format_func=rq.PREFERENCES.get, index=2)
        phone = c2.text_input("Tu WhatsApp o teléfono", value=(patient["telefono"] if patient else "") or "")
        if st.form_submit_button("Enviar a facturación", type="primary", icon=":material/send:"):
            try:
                rq.create_request(clin, id_paciente=id_paciente, tipo=tipo, sintomas=sintomas, preferencia=pref,
                                  telefono=phone, canal="PORTAL", now=ctx.clock())
                st.session_state.sol_emergency = rq.is_emergency(sintomas)
                st.toast("Solicitud enviada a facturación", icon=":material/send:")
                st.rerun()
            except sqlite3.IntegrityError as exc:
                st.error(str(exc))


def patient_appointments_tab(id_paciente: int) -> None:
    clin = ctx.get_clin()
    today = _today()
    upcoming = sch.appointments(clin, day_from=today.isoformat(), day_to=(today + timedelta(days=120)).isoformat(),
                                id_paciente=id_paciente, states=("PROGRAMADA",))
    section_title("Mis próximas citas")
    if not upcoming:
        st.caption("No tienes citas programadas.")
    for c in upcoming:
        with st.container(border=True):
            a, b, d = st.columns([3, 1.3, 1.1], vertical_alignment="center")
            a.markdown(f"**{_ts(c['fecha_hora'])}** · {esc(c['especialidad'].title())} con {esc(c['medico'])}"
                       + (f"<br><span style='color:{MUTED};font-size:0.82rem'>{esc(c['nota'])}</span>" if c["nota"] else ""),
                       unsafe_allow_html=True)
            if c["fecha_hora"][:10] == ctx.clock()[:10] and not c["turno"]:
                if b.button("Ya llegué", key=f"pci_{c['id']}", type="primary", width="stretch",
                            help="Registra tu llegada y recibe tu turno"):
                    try:
                        code = sch.check_in(clin, c["id"], ctx.user_id(), ctx.clock(), only_patient=id_paciente)
                        st.toast(f"Tu turno es {code}", icon=":material/confirmation_number:")
                        st.rerun()
                    except sqlite3.IntegrityError as exc:
                        st.error(str(exc))
            elif c["turno"]:
                b.markdown(chip(f"Turno {c['turno']}", "ok"), unsafe_allow_html=True)
            _cancel_popover(d, c, "pac", only_patient=id_paciente)

    section_title("Pedir una cita")
    if st.session_state.pop("sol_emergency", False):
        banner(f"<b>{esc(rq.EMERGENCY_TEXT)}</b>", "danger")
    requests = rq.patient_requests(clin, id_paciente)
    pending = next((r for r in requests if r["estado"] == "PENDIENTE"), None)
    if pending:
        with st.container(border=True):
            a, b = st.columns([4, 1.2], vertical_alignment="center")
            a.markdown(f"**Solicitud enviada el {_ts(pending['creada_en'])}** · {esc(rq.TYPES[pending['tipo']])} · "
                       f"{esc(rq.PREFERENCES[pending['preferencia']].lower())}<br>"
                       f"<span style='color:{MUTED};font-size:0.85rem'>{esc(pending['sintomas'])}</span><br>"
                       + chip("Facturación la revisa, te asigna el profesional y te responde aquí o por WhatsApp",
                              "info"), unsafe_allow_html=True)
            if b.button("Retirar", key=f"sol_cancel_{pending['id']}", width="stretch"):
                rq.cancel_request(clin, pending["id"], id_paciente, ctx.clock())
                st.rerun()
            st.markdown("**Mensajes con facturación**")
            request_thread(clin, pending, "PACIENTE", "pac", id_paciente)
    else:
        st.caption("Escríbele a facturación qué te pasa: ellos lo valoran, te asignan el profesional adecuado y te "
                   "responden aquí o por WhatsApp. También puedes escribírselo al asistente.")
        _request_form(clin, id_paciente)
        recent = next((r for r in requests if r["estado"] in ("AGENDADA", "CERRADA")), None)
        if recent is not None:
            with st.expander(f"Conversación de tu última solicitud ({rq.STATE[recent['estado']].lower()})",
                             icon=":material/forum:"):
                request_thread(clin, recent, "PACIENTE", "pacold", id_paciente)
    if config.HOSPITAL_WHATSAPP:
        st.link_button("Escribir a facturación por WhatsApp", icon=":material/chat:",
                       url=rq.whatsapp_link(config.HOSPITAL_WHATSAPP, "Hola, quiero pedir una cita en el HSLV.")
                       or f"https://wa.me/{config.HOSPITAL_WHATSAPP}")
    answered = [r for r in requests if r["estado"] == "CERRADA"][:3]
    for r in answered:
        banner(f"<b>Respuesta de facturación</b> ({_ts(r['atendida_en'])}): {esc(r['respuesta'])}", "info")

    history = sch.appointments(clin, day_from="2000-01-01", day_to=(today + timedelta(days=120)).isoformat(),
                               id_paciente=id_paciente)
    if history:
        section_title("Historial de citas")
        colored_history(history[::-1])
