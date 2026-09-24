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

import demo_seed as ds
import pharmacy_service as ps
from agent import fmt_num
from ui import context as ctx
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


def _countdown(limit: str) -> str:
    h = _hours_left(limit)
    if h < 0:
        return chip("Vencida · pendiente del job", "danger", "⏰")
    if h < 24:
        return chip(f"Vence en {h:.0f} h", "warn", "⏰")
    return chip(f"Vence en {h / 24:.1f} días", "info", "⏰")


def _patient_selector(key: str) -> int | None:
    patients = ps.clinical_patients(ctx.get_clin())
    if not patients:
        st.info("No hay historias clínicas abiertas.")
        return None
    labels = {p["id_paciente"]: f"Paciente {p['id_paciente']} · {p['formulas_activas']} fórmula(s) activa(s)"
              for p in patients}
    return st.selectbox("Paciente", list(labels), format_func=labels.get, key=key)


def _gate(permission: str, id_paciente: int, key: str):
    """Autoriza el acceso a datos del paciente. Fuera de turno ofrece 'romper el vidrio'."""
    decision = ctx.authorize_once(permission, id_paciente, ctx.emergency_for(id_paciente))
    if decision.allowed:
        if decision.emergency:
            banner(f"🚨 <b>Acceso de emergencia activo</b> para el paciente {id_paciente}. "
                   f"Justificación: “{esc(ctx.emergency_for(id_paciente))}”. Quedó registrado en la bitácora.", "danger")
        return decision
    if "turno" in decision.reason.lower():
        banner(f"🔒 <b>Estás fuera de turno.</b> Para abrir los datos del paciente {id_paciente} debes activar el "
               "acceso de emergencia (“romper el vidrio”). La justificación quedará auditada.", "warn")
        with st.form(f"breakglass_{key}"):
            text = st.text_area("Justificación clínica", placeholder="Ej.: paciente en paro cardiorrespiratorio "
                                "en urgencias, requiero antecedentes y medicación actual.")
            if st.form_submit_button("🔓 Romper el vidrio y continuar", type="primary"):
                if len(text.strip()) < 20:
                    st.error("Escribe una justificación de al menos 20 caracteres.")
                else:
                    ctx.set_emergency(id_paciente, text.strip())
                    st.rerun()
    else:
        banner(f"⛔ Acceso denegado: {esc(decision.reason)}.", "danger")
    return decision


# ===========================================================================
# D. MÓDULO CLÍNICO Y FARMACIA
# ===========================================================================
def page_clinico() -> None:
    tabs, views = [], []
    if ctx.can("hc.ver_notas") or ctx.can("hc.ver_completa"):
        tabs.append("📋 Historias clínicas"); views.append(_history_tab)
    if ctx.can("prescripcion.crear"):
        tabs.append("✍️ Prescripción"); views.append(_prescription_tab)
    tabs.append("📦 Dispensación y retorno a stock"); views.append(_dispensing_tab)
    for tab, view in zip(st.tabs(tabs), views):
        with tab:
            view()


def _history_tab() -> None:
    id_paciente = _patient_selector("hc_patient")
    if id_paciente is None:
        return
    full = ctx.can("hc.ver_completa")
    decision = _gate("hc.ver_completa" if full else "hc.ver_notas", id_paciente, "hc")
    if not decision.allowed:
        return
    clin = ctx.get_clin()
    events = ps.patient_timeline(clin, id_paciente)
    if not full:  # enfermería: notas clínicas y órdenes, sin gestión de citas
        events = [e for e in events if e["tipo"] not in ("CITA", "INTERCONSULTA")]
    left, right = st.columns([1.35, 1], gap="large")
    with left:
        section_title("Línea de tiempo (inmutable)")
        items = []
        for e in events:
            author = esc(e["autor"])
            ingreso = f" · ingreso {e['oid_ingreso']}" if e["oid_ingreso"] else ""
            items.append(f'<div class="tl-item" style="--dot:{EVENT_STYLE.get(e["tipo"], GREY)}">'
                         f'<div class="tl-meta">{_fmt_ts(e["fecha"])} · {esc(e["tipo"].replace("_", " ").capitalize())}'
                         f' · {author}{ingreso}</div><div class="tl-text">{esc(e["descripcion"])}</div></div>')
        st.markdown(f'<div class="timeline">{"".join(items)}</div>' if items else "Sin eventos registrados.",
                    unsafe_allow_html=True)
        st.caption("Los eventos no se editan ni se eliminan: las correcciones se registran como un evento nuevo.")
    with right:
        section_title("Fórmulas" if full else "Órdenes vigentes")
        rx = ps.patient_prescriptions(clin, id_paciente)
        if not full:
            rx = [r for r in rx if r["estado"] in ("VIGENTE", "PARCIAL")]
        cards = [card(r["producto"].capitalize()[:60],
                      f"{esc(r['dosis'])} cada {r['frecuencia_horas']} h · {r['ambito'].lower()}<br>"
                      f"{r['dosis_entregadas']}/{r['dosis_prescritas']} dosis entregadas · {esc(r['medico'])}",
                      {"CADUCADA": RED, "ENTREGADA": EMERALD, "PARCIAL": AMBER}.get(r["estado"], BLUE),
                      chip(r["estado"].capitalize(), RX_STATE_TONE[r["estado"]]),
                      progress=r["dosis_entregadas"] / r["dosis_prescritas"]) for r in rx]
        if cards:
            grid(cards)
        else:
            st.caption("Sin fórmulas.")


def _prescription_tab() -> None:
    clin = ctx.get_clin()
    id_paciente = _patient_selector("rx_patient")
    if id_paciente is None:
        return
    decision = _gate("prescripcion.crear", id_paciente, "rx")
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
            badges.append(chip("Continuidad crítica", "info", "🛡️"))
        st.markdown(" ".join(badges), unsafe_allow_html=True)
        if info["bloqueado_reevaluacion"] and not info.get("critico_continuidad"):
            banner("⛔ <b>Bloqueado:</b> el paciente tiene una fórmula caducada de este medicamento. Requiere una cita "
                   "de reevaluación cumplida (ver abajo).", "danger")
        elif info["bloqueado_reevaluacion"]:
            banner("🛡️ Fórmula previa caducada, pero es un medicamento de <b>continuidad crítica</b>: se permite "
                   "formular para no interrumpir el tratamiento. La alerta de búsqueda activa ya está en la HC.", "info")

        c1, c2, c3 = st.columns(3)
        dosis = c1.text_input("Dosis y vía", value="1 unidad vía oral")
        freq = c2.selectbox("Frecuencia (horas)", [4, 6, 8, 12, 24], index=2)
        days = c3.number_input("Duración (días)", 1, 90, 7)
        c4, c5, c6 = st.columns(3)
        units = c4.number_input("Dosis a dispensar", 1, 2000, math.ceil(days * 24 / freq),
                                help="Calculado como duración × 24 / frecuencia; se puede ajustar.")
        scope = c5.selectbox("Ámbito", ["AMBULATORIA", "HOSPITALARIA"])
        window = c6.slider("Ventana de reclamo (h)", 24, 168, 72, 12, disabled=scope == "HOSPITALARIA")
        if st.button("Formular y reservar stock", type="primary"):
            try:
                pid = ps.prescribe(clin, medico_id=ctx.user_id(), id_paciente=id_paciente, codigo=codigo,
                                   dosis=dosis, frecuencia_horas=int(freq), duracion_dias=int(days),
                                   dosis_prescritas=int(units), ambito=scope, horas_ventana=int(window), now=ctx.clock())
                st.success(f"Fórmula #{pid} creada. " + ("Se reservaron las unidades y el plazo de reclamo empezó a "
                           "correr." if scope == "AMBULATORIA" else "Queda en la cola de dispensación hospitalaria."))
                st.toast("Fórmula registrada en la historia clínica", icon="✍️")
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
            st.toast("Cita cumplida: bloqueo levantado", icon="✅")
            st.rerun()


def _dispensing_tab() -> None:
    clin = ctx.get_clin()
    queue = ps.dispensing_queue(clin)
    reserved = sum(r["pendientes"] for r in queue if r["ambito"] == "AMBULATORIA")
    soon = sum(1 for r in queue if r["ambito"] == "AMBULATORIA" and _hours_left(r["fecha_limite_reclamo"]) < 24)

    # --- Simulación para el pitch ---
    left, right = st.columns([2, 1], vertical_alignment="center")
    left.markdown(" ".join([chip(f"Reloj clínico: {_fmt_ts(ctx.clock())}", "info", "🕒"),
                            chip(f"{len(queue)} fórmulas en cola", "neutral"),
                            chip(f"{fmt_num(reserved)} dosis reservadas", "neutral"),
                            chip(f"{soon} vencen en < 24 h", "warn" if soon else "ok")]), unsafe_allow_html=True)
    if right.button("⏩ Simular avance de 72 horas", type="primary", width="stretch",
                    help="Adelanta el reloj clínico y ejecuta expire_prescriptions()"):
        before_clock = ctx.clock()
        codes = list({r["codigo_producto"] for r in queue})
        before = {c: dict(v) for c, v in ps.stock_of(clin, codes).items()}
        new_clock = ds.shift_clock(clin, 72)
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
                        f"Reservado: {fmt_num(b.get('reservado'))} → {fmt_num(a.get('reservado'))}<br>"
                        "<i>HC: “Fórmula caducada - Medicamentos no reclamados en el periodo permitido”</i>")
                if d["critico_continuidad"]:
                    body += "<br>🛡️ Continuidad crítica: alerta de búsqueda activa, sin bloqueo de nueva fórmula."
                cards.append(card(d["producto"].capitalize()[:60], body, RED, chip("Caducada", "danger"),
                                  big=f"+{d['devueltas']} dosis a stock"))
            if cards:
                grid(cards)
            if st.button("Ocultar resultado"):
                st.session_state.pop("last_simulation")
                st.rerun()

    # --- Cola de entrega ---
    section_title("Cola de dispensación")
    can_dispense = ctx.can("dispensacion.registrar")
    if not can_dispense:
        st.caption("Vista de consulta: la entrega la registra enfermería o farmacia.")
    if not queue:
        st.info("No hay fórmulas pendientes de entrega.")
    for r in queue:
        with st.container(border=True):
            info_col, act_col = st.columns([3, 1.2], vertical_alignment="center")
            tags = [chip(r["estado"].capitalize(), RX_STATE_TONE[r["estado"]]), chip(r["ambito"].capitalize(), "neutral")]
            if r["ambito"] == "AMBULATORIA":
                tags.append(_countdown(r["fecha_limite_reclamo"]))
            if r["critico_continuidad"]:
                tags.append(chip("Continuidad crítica", "info", "🛡️"))
            info_col.markdown(
                f"**{esc(r['producto'].capitalize())}** · paciente {r['id_paciente']}<br>"
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
                        st.toast(f"Entregadas {qty} dosis", icon="📦")
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
        banner(f"⛔ {esc(decision.reason)}", "danger")
        return
    clin = ctx.get_clin()
    st.markdown(f'<div class="brand"><h1 style="color:#111827">Hola, {esc(user["nombre_mostrado"])}</h1></div>',
                unsafe_allow_html=True)
    st.caption("Aquí solo ves tu propia información. Ningún otro paciente puede verla.")
    tab_rx, tab_appt = st.tabs(["💊 Mis fórmulas", "📅 Mis citas"])
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
                body += f"<br>Reclama antes de <b>{_fmt_ts(r['fecha_limite_reclamo'])}</b> " + _countdown(r["fecha_limite_reclamo"])
            if r["estado"] == "CADUCADA":
                body += "<br>Venció el plazo de reclamo; las dosis pendientes volvieron a la farmacia."
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
                col2.markdown(chip("Cita solicitada", "ok", "✅"), unsafe_allow_html=True)
            elif col2.button("Solicitar cita", key=f"reeval_{r['id']}", type="primary", width="stretch"):
                when = (_dt(ctx.clock()) + timedelta(days=1)).replace(hour=8, minute=0, second=0).strftime(FMT)
                try:
                    ps.request_reevaluation(clin, id_paciente, r["id"], when, ctx.clock())
                    st.toast("Cita de reevaluación solicitada", icon="📅")
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
