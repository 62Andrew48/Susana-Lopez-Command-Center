"""
ui/pages_clinical.py — Secciones D (Módulo clínico y farmacia) y E (Portal del paciente).
Toda escritura pasa por pharmacy_service; toda lectura de datos de un paciente pasa por authorize().
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

import clinical_records as cr
import demo_seed as ds
import pharmacy_service as ps
from agent import fmt_num
from ui import context as ctx
from ui import pages_hc as hc
from ui.theme import (AMBER, BLUE, EMERALD, EVENT_STYLE, GREY, RED, RX_STATE_TONE, banner, card, chip, esc,
                      grid, section_title)

FMT = "%Y-%m-%d %H:%M:%S"
MOTIVO = {"PRIMERA_VEZ": "Primera vez", "CONTROL": "Control", "REEVALUACION_FORMULA": "Reevaluación",
          "INTERCONSULTA": "Interconsulta"}


def _dt(ts: str) -> datetime:
    return datetime.strptime(ts, FMT)


def _hours_left(limit: str) -> float:
    return (_dt(limit) - _dt(ctx.clock())).total_seconds() / 3600


def _fmt_ts(ts: str) -> str:
    return _dt(ts).strftime("%d/%m/%Y %H:%M")


def _left_text(limit: str) -> str:
    h = _hours_left(limit)
    if h < 0:
        return "Vencido"
    return f"{h:.0f} h" if h < 24 else f"{h / 24:.0f} días"


def _countdown(limit: str) -> str:
    h = _hours_left(limit)
    if h < 0:
        return chip("Vencida · pendiente del job", "danger")
    if h < 24:
        return chip(f"Vence en {h:.0f} h", "warn")
    return chip(f"Vence en {h / 24:.1f} días", "info")


# ===========================================================================
# D. MÓDULO CLÍNICO Y FARMACIA
# ===========================================================================
def page_clinico() -> None:
    tabs, views = [], []
    if ctx.can("pacientes.registrar"):
        tabs.append("Pacientes"); views.append(hc.patients_tab)
    if ctx.can("hc.ver_notas") or ctx.can("hc.ver_completa"):
        tabs.append("Historia clínica"); views.append(hc.history_tab)
    if ctx.can("hc.buscar"):
        tabs.append("Buscar historias"); views.append(hc.search_tab)
    if ctx.can("prescripcion.crear"):
        tabs.append("Prescripción"); views.append(_prescription_tab)
    if ctx.can("dispensacion.registrar") or ctx.can("prescripcion.crear"):
        tabs.append("Entregas y apartados"); views.append(_dispensing_tab)
    for tab, view in zip(st.tabs(tabs), views):
        with tab:
            view()


def _prescription_tab() -> None:
    clin = ctx.get_clin()
    id_paciente = hc.pick_patient("rx")
    if id_paciente is None:
        return
    decision = hc.gate("prescripcion.crear", id_paciente, "rx")
    if not decision.allowed:
        return
    products = {r["codigo"]: r["nombre"] for r in clin.execute(
        "SELECT codigo, nombre FROM productos_farmacia WHERE tipo_item = 'Medicamento' ORDER BY nombre")}
    codigo = st.selectbox("Medicamento", list(products), format_func=lambda c: f"{products[c]} · {c}",
                          index=None, placeholder="Escribe para buscar: enoxaparina, amoxicilina…")
    if codigo is None:
        st.caption("Selecciona un medicamento para ver su stock, semáforo y restricciones.")
    else:
        info = ps.product_status(clin, codigo, id_paciente)
        badges = [chip(f"Semáforo {info.get('semaforo', '—').lower()}",
                       {"ROJO": "danger", "AMARILLO": "warn", "VERDE": "ok"}.get(info.get("semaforo"), "neutral"), "●"),
                  chip(f"{fmt_num(info.get('disponible', 0))} disponibles", "neutral"),
                  chip(f"{fmt_num(info.get('dias_cobertura'), 1)} días de cobertura", "neutral")]
        if info.get("critico_continuidad"):
            badges.append(chip("Continuidad crítica", "info"))
        st.markdown(" ".join(badges), unsafe_allow_html=True)
        if info["bloqueado_reevaluacion"] and not info.get("critico_continuidad"):
            banner("<b>Bloqueado:</b> el paciente tiene una fórmula caducada de este medicamento. Requiere una cita "
                   "de reevaluación cumplida (ver abajo).", "danger")
        elif info["bloqueado_reevaluacion"]:
            banner("Fórmula previa caducada, pero es un medicamento de <b>continuidad crítica</b>: se permite "
                   "formular para no interrumpir el tratamiento. La alerta de búsqueda activa ya está en la HC.", "info")

        c1, c2, c3 = st.columns(3)
        dosis = c1.text_input("Dosis y vía", value="1 unidad vía oral")
        freq = c2.selectbox("Frecuencia (horas)", [4, 6, 8, 12, 24], index=2)
        days = c3.number_input("Duración (días)", 1, 90, 7)
        c4, c5, c6 = st.columns(3)
        units = c4.number_input("Dosis a dispensar", 1, 2000, math.ceil(days * 24 / freq),
                                help="Calculado como duración × 24 / frecuencia; se puede ajustar.")
        scope = c5.selectbox("Ámbito", ["AMBULATORIA", "HOSPITALARIA"])
        c6.markdown(f"<div style='padding-top:1.9rem;font-size:0.9rem'>"
                    + (f"Se apartan para el paciente por <b>{ps.RESERVE_DAYS} días</b>" if scope == "AMBULATORIA"
                       else "Se entrega en piso (no se aparta)") + "</div>", unsafe_allow_html=True)
        if st.button("Formular y reservar stock", type="primary"):
            try:
                pid = ps.prescribe(clin, medico_id=ctx.user_id(), id_paciente=id_paciente, codigo=codigo,
                                   dosis=dosis, frecuencia_horas=int(freq), duracion_dias=int(days),
                                   dosis_prescritas=int(units), ambito=scope, now=ctx.clock())
                st.success(f"Fórmula #{pid} creada. " + (f"Las {int(units)} unidades quedan apartadas para el paciente "
                           f"{ps.RESERVE_DAYS} días; si no las reclama, vuelven a estar disponibles."
                           if scope == "AMBULATORIA" else "Queda en la cola de dispensación hospitalaria."))
                st.toast("Fórmula registrada en la historia clínica", icon=":material/edit_note:")
            except sqlite3.IntegrityError as exc:
                st.error(f"No se pudo formular: {exc}")

    section_title("Citas de reevaluación del paciente")
    pending = [c for c in ps.patient_appointments(clin, id_paciente)
               if c["motivo"] == "REEVALUACION_FORMULA" and c["estado"] == "PROGRAMADA"]
    if not pending:
        st.caption("No hay citas de reevaluación pendientes. El paciente las solicita desde su portal.")
    for c in pending:
        col1, col2 = st.columns([3, 1], vertical_alignment="center")
        col1.markdown(f"**{_fmt_ts(c['fecha_hora'])}** · {esc(c['especialidad'].title())} · desbloquea "
                      f"**{esc((c['producto_origen'] or '').capitalize())}**")
        if col2.button("Atender cita", key=f"attend_{c['id']}", width="stretch"):
            ps.complete_appointment(clin, c["id"], ctx.user_id(), ctx.clock())
            st.toast("Cita cumplida: bloqueo levantado", icon=":material/check_circle:")
            st.rerun()


def _dispensing_tab() -> None:
    clin = ctx.get_clin()
    queue = ps.dispensing_queue(clin)
    reserved = sum(r["pendientes"] for r in queue if r["ambito"] == "AMBULATORIA")
    soon = sum(1 for r in queue if r["ambito"] == "AMBULATORIA" and _hours_left(r["fecha_limite_reclamo"]) < 24)

    # --- Simulación para el pitch: adelantar el reloj clínico y ejecutar el job de caducidad ---
    st.markdown(" ".join([chip(f"Reloj clínico: {_fmt_ts(ctx.clock())}", "info"),
                          chip(f"{len(queue)} fórmulas en cola", "neutral"),
                          chip(f"{fmt_num(reserved)} unidades apartadas", "neutral"),
                          chip(f"{soon} vencen en < 24 h", "warn" if soon else "ok")]), unsafe_allow_html=True)
    left, right = st.columns([2, 1], vertical_alignment="bottom")
    jump = left.segmented_control("Simular el paso del tiempo", ["1 día", "7 días", f"{ps.RESERVE_DAYS} días"],
                                  default=f"{ps.RESERVE_DAYS} días", key="sim_jump",
                                  help="Adelanta el reloj de la demo y ejecuta el job que devuelve a stock lo no reclamado")
    if right.button("Avanzar el reloj", icon=":material/fast_forward:", type="primary", width="stretch"):
        hours = int((jump or "1").split()[0]) * 24
        before_clock = ctx.clock()
        codes = list({r["codigo_producto"] for r in queue})
        before = {c: dict(v) for c, v in ps.stock_of(clin, codes).items()}
        new_clock = ds.shift_clock(clin, hours)
        expired = ps.expire_prescriptions(clin, new_clock)
        after = {c: dict(v) for c, v in ps.stock_of(clin, codes).items()}
        st.session_state.last_simulation = {"from": before_clock, "to": new_clock, "before": before,
                                            "after": after, "details": [dict(d) for d in ps.expiry_details(clin, expired)]}
        st.rerun()

    sim = st.session_state.get("last_simulation")
    if sim:
        with st.container(border=True):
            st.markdown(f"**Resultado de la simulación** · {_fmt_ts(sim['from'])} → {_fmt_ts(sim['to'])}")
            if not sim["details"]:
                st.caption("Ninguna fórmula venció en este intervalo.")
            cards = []
            for d in sim["details"]:
                b, a = sim["before"].get(d["codigo_producto"], {}), sim["after"].get(d["codigo_producto"], {})
                body = (f"Paciente {d['id_paciente']} · venció {_fmt_ts(d['fecha_limite_reclamo'])}<br>"
                        f"Disponible: {fmt_num(b.get('disponible'))} → <b>{fmt_num(a.get('disponible'))}</b> · "
                        f"Apartado: {fmt_num(b.get('reservado'))} → {fmt_num(a.get('reservado'))}<br>"
                        "<i>HC: “Fórmula caducada - Medicamentos no reclamados en el periodo permitido”</i>")
                if d["critico_continuidad"]:
                    body += "<br><b>Continuidad crítica:</b> alerta de búsqueda activa, sin bloqueo de nueva fórmula."
                cards.append(card(d["producto"].capitalize()[:60], body, RED, chip("Caducada", "danger"),
                                  big=f"+{d['devueltas']} und. vuelven a disponibles"))
            if cards:
                grid(cards)
            if st.button("Ocultar resultado"):
                st.session_state.pop("last_simulation")
                st.rerun()

    # --- Apartados por paciente ---
    section_title("Unidades apartadas para pacientes")
    held = ps.reservations(clin)
    if not held:
        st.caption("No hay unidades apartadas.")
    else:
        st.caption(f"Al formular, las unidades salen de “disponibles” y quedan apartadas para ese paciente hasta que "
                   f"las reclame (máximo {ps.RESERVE_DAYS} días). Si no las reclama, vuelven solas a disponibles.")
        st.dataframe(pd.DataFrame([{
            "Paciente": h["paciente"], "Medicamento": h["producto"].capitalize(), "Apartadas": h["apartadas"],
            "Formulada": _fmt_ts(h["fecha_prescripcion"]), "Apartado hasta": _fmt_ts(h["fecha_limite_reclamo"]),
            "Quedan": _left_text(h["fecha_limite_reclamo"]), "Médico": h["medico"]} for h in held]),
            hide_index=True, width="stretch", height=min(38 * (len(held) + 1), 260))

    # --- Cola de entrega ---
    section_title("Cola de entrega")
    can_dispense = ctx.can("dispensacion.registrar")
    if not can_dispense:
        st.caption("Vista de consulta: la entrega la registra enfermería o farmacia.")
    if not queue:
        st.info("No hay fórmulas pendientes de entrega.")
    names = {h["id_paciente"]: h["paciente"] for h in held}
    for r in queue:
        pt = cr.get_patient(clin, r["id_paciente"])
        who = names.get(r["id_paciente"]) or (cr.display_name(pt) if pt else f"Paciente {r['id_paciente']}")
        with st.container(border=True):
            info_col, act_col = st.columns([3, 1.2], vertical_alignment="center")
            tags = [chip(r["estado"].capitalize(), RX_STATE_TONE[r["estado"]]), chip(r["ambito"].capitalize(), "neutral")]
            if r["ambito"] == "AMBULATORIA":
                tags.append(_countdown(r["fecha_limite_reclamo"]))
            if r["critico_continuidad"]:
                tags.append(chip("Continuidad crítica", "info"))
            info_col.markdown(
                f"**{esc(r['producto'].capitalize())}** · {esc(who)}<br>"
                f"<span class='muted'>{esc(r['dosis'])} · {r['dosis_entregadas']}/{r['dosis_prescritas']} entregadas · "
                f"{esc(r['medico'])}</span><br>" + " ".join(tags), unsafe_allow_html=True)
            qty = act_col.number_input("Cantidad", 1, int(r["pendientes"]), min(int(r["pendientes"]), 7),
                                       key=f"qty_{r['id']}", disabled=not can_dispense, label_visibility="collapsed")
            if act_col.button("Entregar", key=f"give_{r['id']}", width="stretch", disabled=not can_dispense):
                decision = ctx.authorize_once("dispensacion.registrar", r["id_paciente"],
                                              ctx.emergency_for(r["id_paciente"]))
                if not decision.allowed:
                    st.error(f"No autorizado: {decision.reason}. Usa “romper el vidrio” desde Historias clínicas.")
                else:
                    try:
                        ps.dispense(clin, r["id"], ctx.user_id(), int(qty), ctx.clock())
                        st.toast(f"Entregadas {qty} dosis", icon=":material/inventory_2:")
                        st.rerun()
                    except sqlite3.IntegrityError as exc:
                        st.error(f"No se pudo entregar: {exc}")


# ===========================================================================
# E. PORTAL DEL PACIENTE
# ===========================================================================
def page_portal() -> None:
    user = ctx.current_user()
    id_paciente = user["id_paciente"]
    decision = ctx.authorize_once("portal.propio", id_paciente)
    if not decision.allowed:
        banner(f"{esc(decision.reason)}", "danger")
        return
    clin = ctx.get_clin()
    st.markdown(f'<div class="brand"><h1 style="color:#111827">Hola, {esc(user["nombre_mostrado"])}</h1></div>',
                unsafe_allow_html=True)
    st.caption("Aquí solo ves tu propia información. Ningún otro paciente puede verla.")
    tab_rx, tab_appt, tab_hc = st.tabs(["Mis fórmulas", "Mis citas", "Mi historia clínica"])
    with tab_hc:
        st.markdown(hc.CSS, unsafe_allow_html=True)
        st.caption("Tus registros clínicos y documentos. Tienes derecho a conocerlos (Res. 1995 de 1999).")
        recs = cr.records(clin, id_paciente, include_annulled=False)
        files = [dict(a) for a in cr.attachments(clin, id_paciente, include_annulled=False)]
        if not recs and not files:
            st.info("Aún no hay registros en tu historia clínica.")
        for r in recs:
            hc._record_card(r, [a for a in files if a["registro_id"] == r["id"]], id_paciente, False)
        loose = [a for a in files if a["registro_id"] is None]
        if loose:
            section_title("Documentos")
            cols = st.columns(3)
            for i, a in enumerate(loose):
                hc._download_button(cols[i % 3], a, id_paciente, "portal")
    with tab_rx:
        rx = ps.patient_prescriptions(clin, id_paciente)
        if not rx:
            st.info("No tienes fórmulas registradas.")
        cards = []
        for r in rx:
            body = (f"{esc(r['dosis'])} cada {r['frecuencia_horas']} h durante {r['duracion_dias']} días<br>"
                    f"Médico: {esc(r['medico'])} · formulada {_fmt_ts(r['fecha_prescripcion'])}<br>"
                    f"Entregadas {r['dosis_entregadas']} de {r['dosis_prescritas']} dosis")
            if r["estado"] in ("VIGENTE", "PARCIAL") and r["ambito"] == "AMBULATORIA":
                body += (f"<br>Apartado para ti hasta el <b>{_fmt_ts(r['fecha_limite_reclamo'])}</b> "
                         + _countdown(r["fecha_limite_reclamo"]))
            if r["estado"] == "CADUCADA":
                body += "<br>Pasaron los 30 días sin reclamarlo; las unidades apartadas volvieron a la farmacia."
            cards.append(card(r["producto"].capitalize()[:60], body,
                              {"CADUCADA": RED, "ENTREGADA": EMERALD, "PARCIAL": AMBER}.get(r["estado"], BLUE),
                              chip(r["estado"].capitalize(), RX_STATE_TONE[r["estado"]]),
                              progress=r["dosis_entregadas"] / r["dosis_prescritas"]))
        grid(cards)
        blocked = [r for r in rx if r["estado"] == "CADUCADA" and r["requiere_reevaluacion"]]
        appts = ps.patient_appointments(clin, id_paciente)
        for r in blocked:
            already = any(c["prescripcion_origen_id"] == r["id"] and c["estado"] == "PROGRAMADA" for c in appts)
            col1, col2 = st.columns([3, 1.3], vertical_alignment="center")
            col1.markdown(f"Para volver a recibir **{esc(r['producto'].capitalize())}** necesitas una cita de reevaluación.")
            if already:
                col2.markdown(chip("Cita solicitada", "ok"), unsafe_allow_html=True)
            elif col2.button("Solicitar cita", key=f"reeval_{r['id']}", type="primary", width="stretch"):
                when = (_dt(ctx.clock()) + timedelta(days=1)).replace(hour=8, minute=0, second=0).strftime(FMT)
                try:
                    ps.request_reevaluation(clin, id_paciente, r["id"], when, ctx.clock())
                    st.toast("Cita de reevaluación solicitada", icon=":material/event:")
                    st.rerun()
                except sqlite3.IntegrityError as exc:
                    st.error(str(exc))
    with tab_appt:
        appts = ps.patient_appointments(clin, id_paciente)
        tone = {"PROGRAMADA": "info", "CUMPLIDA": "ok", "CANCELADA": "neutral", "NO_ASISTIO": "danger"}
        grid([card(_fmt_ts(c["fecha_hora"]),
                   f"{esc(c['especialidad'].title())} · {esc(MOTIVO.get(c['motivo'], c['motivo']))}"
                   + (f" de {esc(c['producto_origen'].capitalize())}" if c["producto_origen"] else "")
                   + f"<br>Profesional: {esc(c['medico'])}",
                   BLUE if c["estado"] == "PROGRAMADA" else GREY, chip(c["estado"].capitalize(), tone[c["estado"]]))
              for c in appts] or [card("Sin citas", "No tienes citas registradas.")])
