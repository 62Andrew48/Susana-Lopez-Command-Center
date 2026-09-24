"""
ui/pages_quirofanos.py — Quirófanos: capacidad por área y día, lista de espera y programación optimizada.
Coordinación de quirófanos (quirofanos.coordinar) agenda, reprograma, cancela y cierra; los médicos
(quirofanos.solicitar) piden cirugías y cancelan las suyas en espera; gerencia (quirofanos.ver) consulta.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import clinical_records as cr
import surgery_planner as sp
from agent import fmt_num
from ui import context as ctx
from ui.theme import AMBER, BLUE, EMERALD, GREY, RED, banner, chip, esc, kpi_card, show, style_fig

STATE = {"EN_ESPERA": "En espera", "PROGRAMADA": "Programada", "REALIZADA": "Realizada", "CANCELADA": "Cancelada"}


@st.cache_data(ttl=600, show_spinner=False)
def _profile(ref_iso: str) -> pd.DataFrame:
    return sp.capacity_profile(ctx.get_conn(), date.fromisoformat(ref_iso))


@st.cache_data(ttl=600, show_spinner=False)
def _compliance() -> dict:
    return sp.compliance(ctx.get_conn())


def _area(a: str) -> str:
    return sp.AREAS.get(a, a)


def _today() -> date:
    return date.fromisoformat(ctx.clock()[:10])


def page_quirofanos() -> None:
    if not ctx.can("quirofanos.ver"):
        st.error("Tu rol no tiene acceso a la programación quirúrgica.")
        st.stop()
    clin = ctx.get_clin()
    profile = _profile(ctx.ref_date().isoformat())
    comp = _compliance()
    waiting = [dict(r) for r in sp.waitlist(clin)]
    scheduled = [dict(r) for r in sp.waitlist(clin, ("PROGRAMADA",))]
    start = _today() + timedelta(days=1)
    horizon = 14
    free_total = int(sum(profile.set_index(["area", "dow"])["libres"].get((a, (start + timedelta(days=i)).weekday()), 0)
                         for a in profile["area"].unique() for i in range(horizon)))
    urgent = sum(r["prioridad"] == "URGENTE" for r in waiting)
    avg_wait = (sum((start - date.fromisoformat(w["fecha_solicitud"][:10])).days for w in waiting) / len(waiting)
                if waiting else 0)

    st.markdown("### Quirófanos")
    cols = st.columns(4)
    cards = [
        ("Cumplimiento de la programación", f"{fmt_num(comp['cumplimiento_pct'], 1)} %",
         f"{comp['realizadas']} realizadas · {comp['sin_ejecutar']} sin evidencia", EMERALD),
        ("Lista de espera", str(len(waiting)), f"{urgent} urgente(s)", RED if urgent else AMBER),
        ("Espera promedio", f"{fmt_num(avg_wait, 0)} días" if waiting else "—", "desde la solicitud", BLUE),
        (f"Cupos libres · {horizon} días", str(free_total), "capacidad probada − carga habitual", BLUE),
    ]
    for col, (label, value, sub, tone) in zip(cols, cards):
        col.markdown(kpi_card(label, value, sub, tone), unsafe_allow_html=True)

    tab_plan, tab_wait, tab_sched, tab_cap = st.tabs(["Programación sugerida", "Lista de espera", "Programadas",
                                                      "Capacidad por día"])
    with tab_plan:
        _plan_tab(clin, waiting, profile, start, horizon)
    with tab_wait:
        _waitlist_tab(clin, waiting, profile)
    with tab_sched:
        _scheduled_tab(clin, scheduled)
    with tab_cap:
        _capacity_tab(profile)


def _plan_tab(clin, waiting, profile, start, horizon) -> None:
    st.caption("Urgentes en 24 h, prioritarias en 7 días y electivas por antigüedad, al primer día con cupo libre en "
               "su área. La capacidad sale de lo que cada área ya demostró operar (sin inventar salas ni horarios).")
    if not waiting:
        st.info("No hay solicitudes en espera.")
        return
    used = sp.scheduled_counts(clin, start.isoformat(), (start + timedelta(days=horizon)).isoformat())
    proposal = sp.plan(waiting, profile, start, horizon, used)
    fit = [p for p in proposal if p["fecha"]]
    alerts = [p for p in proposal if p["alerta"]]
    st.markdown(" ".join([chip(f"{len(fit)} de {len(proposal)} con fecha propuesta", "ok"),
                          chip(f"{len(alerts)} con alerta", "warn" if alerts else "ok")]), unsafe_allow_html=True)
    for p in [p for p in proposal if p["alerta"] and p["prioridad"] == "URGENTE"]:
        banner(f"<b>Urgente:</b> {esc(p['paciente'])} · {esc(_area(p['area']))}: {esc(p['alerta'])}.", "danger")
    # Carga por día: habitual + propuesta vs capacidad (todas las áreas)
    days = [start + timedelta(days=i) for i in range(horizon)]
    prof = profile.groupby("dow")[["habitual", "capacidad"]].sum()
    extra = pd.Series([sum(1 for p in fit if p["fecha"] == d.isoformat()) for d in days])
    already = pd.Series([sum(n for (a, dd), n in used.items() if dd == d.isoformat()) for d in days])
    labels = [f"{sp.DOW[d.weekday()][:3]} {d:%d/%m}" for d in days]
    fig = go.Figure()
    fig.add_bar(x=labels, y=[prof.loc[d.weekday(), "habitual"] for d in days], name="Carga habitual", marker_color=GREY)
    fig.add_bar(x=labels, y=already, name="Ya programadas", marker_color=BLUE)
    fig.add_bar(x=labels, y=extra, name="Propuesta (lista de espera)", marker_color=EMERALD)
    fig.add_scatter(x=labels, y=[prof.loc[d.weekday(), "capacidad"] for d in days], name="Capacidad probada",
                    mode="lines+markers", line=dict(color=AMBER, dash="dot"))
    fig.update_layout(barmode="stack", legend=dict(orientation="h", y=-0.2))
    fig.update_yaxes(title="cirugías por día")
    show(style_fig(fig, 340, "Cirugías por día: carga habitual + programación vs. capacidad"))
    df = pd.DataFrame([{"Fecha": p["fecha"] or "—", "Paciente": p["paciente"], "Área": _area(p["area"]),
                        "Prioridad": sp.PRIORITY_LABEL[p["prioridad"]], "Días esperando": p["espera_dias"],
                        "Alerta": p["alerta"] or ""} for p in proposal])
    st.dataframe(df, hide_index=True, width="stretch", height=min(38 * (len(df) + 1), 420))
    coordinator = ctx.can("quirofanos.coordinar")
    if not coordinator:
        st.caption("La programación la confirma coordinación de quirófanos.")
    if coordinator and st.button(f"Confirmar la programación ({len(fit)} cirugías)", type="primary",
                                 icon=":material/event_available:", disabled=not fit):
        n = sp.confirm(clin, fit, ctx.user_id(), ctx.clock(), coordinator=True)
        st.toast(f"{n} cirugías programadas", icon=":material/check_circle:")
        st.rerun()


def _waitlist_tab(clin, waiting, profile) -> None:
    if not ctx.can("quirofanos.solicitar"):
        _waitlist_table(clin, waiting, profile)
        return
    with st.expander("Nueva solicitud de cirugía", icon=":material/add_circle:"):
        q = st.text_input("Paciente", placeholder="Nombre, documento o id", key="qx_pq")
        found = cr.search_patients(clin, q, limit=20)
        if not found:
            st.caption("Registra primero al paciente en Clínico y farmacia → Pacientes.")
        else:
            with st.form("qx_new"):
                names = {p["id_paciente"]: f"{cr.display_name(p)} (id {p['id_paciente']})" for p in found}
                pid = st.selectbox("Selecciona", list(names), format_func=names.get)
                c1, c2 = st.columns(2)
                area = c1.selectbox("Área quirúrgica", list(sp.AREAS), format_func=_area)
                prio = c2.selectbox("Prioridad", list(sp.PRIORITY_LABEL), format_func=sp.PRIORITY_LABEL.get, index=2)
                proc = st.text_input("Procedimiento", placeholder="Ej.: colecistectomía laparoscópica")
                cups = st.text_input("Códigos CUPS (opcional)")
                if st.form_submit_button("Agregar a la lista de espera", type="primary"):
                    try:
                        sp.request_surgery(clin, id_paciente=pid, area=area, prioridad=prio, procedimiento=proc,
                                           user_id=ctx.user_id(), now=ctx.clock(), codigos=cups)
                        st.toast("Solicitud registrada", icon=":material/check_circle:")
                        st.rerun()
                    except sqlite3.IntegrityError as exc:
                        st.error(str(exc))
    _waitlist_table(clin, waiting, profile)


def _waitlist_table(clin, waiting, profile) -> None:
    if not waiting:
        st.info("Lista de espera vacía.")
        return
    df = pd.DataFrame([{"id": w["id"], "Paciente": w["paciente"], "Área": _area(w["area_quirofano"]),
                        "Prioridad": sp.PRIORITY_LABEL[w["prioridad"]], "Solicitada": w["fecha_solicitud"][:10],
                        "Procedimientos": w["procedimientos"], "CUPS": w["codigos_cups"] or "—",
                        "Origen": "HIS (sin ejecutar)" if w["origen"] == "HIS" else "App", "Nota": w["nota"] or ""}
                       for w in waiting])
    event = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                         key=f"qx_wait_{st.session_state.get('qx_wait_v', 0)}", column_config={"id": None})
    sel = event.selection.rows if event and event.selection else []
    if not sel:
        st.caption("Selecciona una solicitud para agendarla o quitarla.")
        return
    w = waiting[sel[0]]
    coordinator = ctx.can("quirofanos.coordinar")
    left, right = st.columns(2, gap="medium")
    with left.container(border=True):
        st.markdown(f"**Agendar** · {esc(w['paciente'])} · {esc(_area(w['area_quirofano']))}", unsafe_allow_html=True)
        if not coordinator:
            st.caption("Solo coordinación de quirófanos agenda cirugías.")
        else:
            day = st.date_input("Fecha de la cirugía", value=_today() + timedelta(days=1), min_value=_today(),
                                max_value=_today() + timedelta(days=sp.MAX_DAYS_AHEAD), format="DD/MM/YYYY",
                                key=f"qx_day_{w['id']}")
            free, used = sp.free_on(clin, profile, w["area_quirofano"], day.isoformat())
            over = used >= free
            urgent_late = w["prioridad"] == "URGENTE" and (day - _today()).days > 1
            st.markdown(chip(f"{used} programadas de {free} cupos ese día", "danger" if over else "ok"),
                        unsafe_allow_html=True)
            just = ""
            if over or urgent_late:
                just = st.text_area("Justificación (sobrecupo o urgente fuera de 24 h)", key=f"qx_just_{w['id']}",
                                    placeholder="Ej.: se habilita turno quirúrgico adicional de 14:00 a 19:00")
            if st.button("Agendar cirugía", type="primary", key=f"qx_sched_{w['id']}", icon=":material/event:"):
                try:
                    sp.schedule(clin, w["id"], day.isoformat(), ctx.user_id(), ctx.clock(), profile,
                                coordinator=True, justification=just)
                    st.toast(f"Cirugía agendada para el {day:%d/%m/%Y}", icon=":material/check_circle:")
                    _clear_selection()
                    st.rerun()
                except sqlite3.IntegrityError as exc:
                    st.error(str(exc))
    with right.container(border=True):
        st.markdown("**Quitar de la lista**")
        _cancel_form(clin, w, "w")


def _clear_selection() -> None:
    """La tabla cambió: se quita la selección para que no quede marcada otra solicitud por su posición."""
    st.session_state.qx_wait_v = st.session_state.get("qx_wait_v", 0) + 1


def _cancel_form(clin, w, key: str) -> None:
    cat = st.selectbox("Causa", sp.CANCEL_REASONS, key=f"qx_cat_{key}_{w['id']}")
    detail = st.text_input("Detalle", key=f"qx_det_{key}_{w['id']}",
                           help="Obligatorio (15+ caracteres) si la cirugía es para hoy/mañana o la causa es “Otro”")
    if st.button("Cancelar cirugía", key=f"qx_cancel_{key}_{w['id']}", icon=":material/event_busy:"):
        try:
            sp.cancel(clin, w["id"], ctx.user_id(), ctx.clock(), category=cat, detail=detail,
                      coordinator=ctx.can("quirofanos.coordinar"))
            st.toast("Cirugía cancelada (queda registrada)", icon=":material/event_busy:")
            _clear_selection()
            st.rerun()
        except sqlite3.IntegrityError as exc:
            st.error(str(exc))
    st.caption("No se borra: queda cancelada con su causa, quién y cuándo. Un médico solo quita sus solicitudes en "
               "espera; lo programado lo quita coordinación.")


def _scheduled_tab(clin, scheduled) -> None:
    if not scheduled:
        st.info("Aún no hay cirugías programadas. Confírmalas en “Programación sugerida” o agéndalas desde la lista.")
        return
    coordinator = ctx.can("quirofanos.coordinar")
    for day, group in pd.DataFrame(scheduled).sort_values("fecha_programada").groupby("fecha_programada", sort=True):
        d = date.fromisoformat(day)
        st.markdown(f"#### {sp.DOW[d.weekday()].capitalize()} {d:%d/%m/%Y} · {len(group)} cirugía(s)")
        for s_ in group.to_dict("records"):
            with st.container(border=True):
                a, b, c = st.columns([3, 1, 1.6], vertical_alignment="center")
                over = f"<br><span class='muted'>Sobrecupo: {esc(s_['sobrecupo_justificacion'])}</span>" \
                    if s_.get("sobrecupo_justificacion") else ""
                a.markdown(f"{esc(s_['paciente'])} · {esc(_area(s_['area_quirofano']))} "
                           f"{chip(sp.PRIORITY_LABEL[s_['prioridad']], 'danger' if s_['prioridad'] == 'URGENTE' else 'neutral')}"
                           f"<br><span class='muted'>{esc(s_['nota'] or '')}</span>{over}", unsafe_allow_html=True)
                if coordinator and b.button("Realizada", key=f"qx_done_{s_['id']}", width="stretch",
                                            disabled=day > ctx.clock()[:10], help="Disponible el día de la cirugía"):
                    sp.mark_done(clin, s_["id"], ctx.user_id(), ctx.clock(), coordinator=True)
                    st.rerun()
                with c.popover("Reprogramar / cancelar", width="stretch"):
                    if coordinator:
                        reason = st.text_input("Motivo para reprogramar", key=f"qx_r_{s_['id']}")
                        if st.button("Devolver a lista de espera", key=f"qx_back_{s_['id']}"):
                            try:
                                sp.reprogram(clin, s_["id"], ctx.user_id(), ctx.clock(), reason, coordinator=True)
                                st.rerun()
                            except sqlite3.IntegrityError as exc:
                                st.error(str(exc))
                        st.divider()
                    _cancel_form(clin, s_, "s")


def _capacity_tab(profile) -> None:
    for line in sp.idle_insight(profile):
        st.markdown(f"- {line}")
    main = profile[profile["capacidad"] >= 1]
    if main.empty:
        return
    table = main.assign(Área=main["area"].map(_area), Día=main["dow"].map(lambda d: sp.DOW[d]),
                        celda=[f"{h:.1f} de {c:.0f} ({l} libres)" for h, c, l in
                               zip(main["habitual"], main["capacidad"], main["libres"])])
    pv = table.pivot_table(index="Área", columns="Día", values="celda", aggfunc="first").reindex(columns=sp.DOW)
    st.dataframe(pv.fillna("—"), width="stretch")
    st.caption("Cada celda: cirugías habituales ese día de la semana de capacidad probada (percentil 90 de las últimas "
               "16 semanas) y los cupos que quedan libres. El HIS no trae salas ni horas; con esos datos el mismo "
               "optimizador puede asignar por sala y franja horaria.")
