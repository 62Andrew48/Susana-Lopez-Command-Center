"""
ui/pages_hoy.py — Página de inicio "Hoy": lo que requiere acción ahora, según el rol.

  * Personal (Admin, Doctor, Enfermería): 4 indicadores del día, acciones prioritarias con la cama libre
    compatible ya ubicada, cola de urgencias a la hora del reloj clínico, saturación y pendientes del rol.
  * Paciente: sus fórmulas por reclamar, su próxima cita y lo que tiene que hacer hoy.

El histórico (tendencias, epidemiología) vive en "Indicadores"; aquí solo va lo accionable.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import config
import database as db
import pharmacy_service as ps
from agent import fmt_minutes, fmt_num
from ui import context as ctx
from ui.theme import (AMBER, BLUE, BORDER, EMERALD, MUTED, RED, SEVERITY, TEXT, chip, esc, occupancy_color,
                      severity_pill, show, style_fig)

FMT = "%Y-%m-%d %H:%M:%S"
DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre"]
# Colores de triage (Resolución 5596 de 2015): I rojo, II naranja, III amarillo, IV verde, V azul
TRIAGE_COLOR = {1: "#DC2626", 2: "#EA580C", 3: "#CA8A04", 4: "#16A34A", 5: "#2563EB"}
TRIAGE_BG = {1: "#FEE2E2", 2: "#FFEDD5", 3: "#FEF9C3", 4: "#DCFCE7", 5: "#DBEAFE"}
ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V"}

CSS = f"""
<style>
  .today-head {{display:flex; justify-content:space-between; align-items:flex-end; gap:1rem; flex-wrap:wrap;
      margin: 0.2rem 0 0.9rem;}}
  .today-head h2 {{font-size:1.5rem; font-weight:700; color:{TEXT}; margin:0; padding:0;}}
  .today-head p {{margin:0.15rem 0 0; color:{MUTED}; font-size:0.9rem;}}
  .stat-grid {{display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:0.9rem; margin-bottom:1.1rem;}}
  .stat {{background:#FFF; border:1px solid {BORDER}; border-left:4px solid var(--c); border-radius:12px;
      box-shadow:0 1px 3px rgba(0,0,0,.05); padding:0.95rem 1.05rem; display:flex; flex-direction:column;
      gap:0.3rem; min-height:150px;}}
  .stat-top {{display:flex; justify-content:space-between; align-items:center;}}
  .stat-icon {{width:2.1rem; height:2.1rem; border-radius:8px; display:flex; align-items:center;
      justify-content:center; font-size:1.05rem; background:var(--bg);}}
  .stat-delta {{font-size:0.7rem; font-weight:700; border-radius:999px; padding:0.12rem 0.5rem;}}
  .stat-label {{font-size:0.82rem; color:{MUTED}; font-weight:600; margin-top:0.2rem;}}
  .stat-value {{font-size:1.75rem; font-weight:750; color:{TEXT}; line-height:1.1; font-variant-numeric:tabular-nums;}}
  .stat-value small {{font-size:0.85rem; font-weight:500; color:{MUTED}; margin-left:0.25rem;}}
  .stat-bar {{display:flex; height:6px; border-radius:999px; overflow:hidden; background:#F1F5F9; margin-top:auto;}}
  .stat-bar span {{display:block; height:100%;}}
  .stat-foot {{font-size:0.68rem; color:{MUTED}; text-transform:uppercase; letter-spacing:0.03em;}}
  .q-row {{display:flex; align-items:center; gap:0.7rem; background:#FFF; border:1px solid {BORDER};
      border-radius:10px; padding:0.55rem 0.75rem; margin-bottom:0.45rem;}}
  .q-av {{width:2rem; height:2rem; border-radius:50%; background:#F1F5F9; display:flex; align-items:center;
      justify-content:center; font-size:0.95rem; flex:none;}}
  .q-main {{flex:1; min-width:0;}}
  .q-code {{font-size:0.85rem; font-weight:700; color:{TEXT};}}
  .q-tag {{font-size:0.66rem; font-weight:700; border-radius:5px; padding:0.05rem 0.4rem; margin-left:0.35rem;}}
  .q-sub {{font-size:0.75rem; color:{MUTED}; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}}
  .q-wait {{text-align:right; flex:none;}}
  .q-wait small {{display:block; font-size:0.66rem; color:{MUTED};}}
  .q-wait b {{font-size:0.95rem; font-variant-numeric:tabular-nums;}}
  .bed-sug {{font-size:0.82rem; color:{TEXT}; background:#ECFDF5; border:1px solid #A7F3D0; border-radius:8px;
      padding:0.4rem 0.6rem; margin-top:0.35rem;}}
  .todo {{background:#FFF; border:1px solid {BORDER}; border-radius:10px; padding:0.75rem 0.9rem; height:100%;}}
  .todo-n {{font-size:1.6rem; font-weight:750; color:{TEXT}; font-variant-numeric:tabular-nums;}}
  .todo-t {{font-size:0.84rem; color:#374151; font-weight:600;}}
  .todo-s {{font-size:0.75rem; color:{MUTED};}}
  @media (max-width: 1150px) {{ .stat-grid {{grid-template-columns:repeat(2, minmax(0,1fr));}} }}
  @media (max-width: 560px)  {{ .stat-grid {{grid-template-columns:1fr;}} .stat {{min-height:0;}} }}
</style>
"""


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.strptime(ctx.clock(), FMT)


def _long_date(d) -> str:
    return f"{DAYS[d.weekday()]} {d.day} de {MONTHS[d.month - 1]} de {d.year}"


def _link(slug: str, label: str, icon: str | None = None, container=st) -> None:
    """Enlace a otra sección solo si el rol la tiene habilitada."""
    if slug in ctx.PAGES:
        container.page_link(ctx.PAGES[slug], label=label, icon=icon)


def _stat(label: str, value: str, unit: str, icon: str, color: str, bg: str, bar_html: str, foot: str,
          delta: str | None = None, delta_good: bool = True) -> str:
    delta_html = ""
    if delta:
        fg, dbg = ("#065F46", "#D1FAE5") if delta_good else ("#991B1B", "#FEE2E2")
        delta_html = f'<span class="stat-delta" style="color:{fg};background:{dbg}">{esc(delta)}</span>'
    return (f'<div class="stat" style="--c:{color};--bg:{bg}"><div class="stat-top">'
            f'<div class="stat-icon">{icon}</div>{delta_html}</div>'
            f'<div class="stat-label">{esc(label)}</div>'
            f'<div class="stat-value">{esc(value)}<small>{esc(unit)}</small></div>'
            f'{bar_html}<div class="stat-foot">{esc(foot)}</div></div>')


def _bar(parts: list[tuple[float, str]]) -> str:
    """Barra apilada: [(fracción 0-1, color), ...]."""
    spans = "".join(f'<span style="width:{max(0.0, min(f, 1.0)) * 100:.1f}%;background:{c}"></span>' for f, c in parts)
    return f'<div class="stat-bar">{spans}</div>'


# ---------------------------------------------------------------------------
# Datos del día (en caché: no cambian con el reloj salvo la cola)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False)
def _beds(day_iso: str):
    return db.bed_map(ctx.get_conn(), datetime.fromisoformat(day_iso).date())


def _physical_occupancy(day) -> dict:
    """Ocupación de camas FÍSICAS de internación (sin Urgencias ni camas virtuales de expansión)."""
    beds = _beds(day.isoformat())
    beds = beds[beds["servicio"] != "Urgencias"]
    phys, virt = beds[beds["es_virtual"] == 0], beds[beds["es_virtual"] == 1]
    cap, occ = len(phys), int(phys["ocupada"].sum())
    return {"capacidad": cap, "ocupadas": occ, "libres": cap - occ, "pct": occ / cap * 100 if cap else 0,
            "expansion_en_uso": int(virt["ocupada"].sum()), "expansion_total": len(virt)}


@st.cache_data(ttl=120, show_spinner=False)
def _queue(now_str: str):
    return db.triage_queue(ctx.get_conn(), now_str)


# ===========================================================================
# Página
# ===========================================================================
def page_hoy() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    if ctx.current_user()["rol"] == "PACIENTE":
        _patient_today()
    else:
        _staff_today()


def _staff_today() -> None:
    ref, now = ctx.ref_date(), _now()
    user = ctx.current_user()
    role = user["rol"]
    st.markdown(f'<div class="today-head"><div><h2>Hoy, {esc(_long_date(ref))}</h2>'
                f'<p>Lo que requiere acción en este momento · reloj clínico {now:%H:%M}</p></div></div>',
                unsafe_allow_html=True)

    occ, occ_prev = _physical_occupancy(ref), _physical_occupancy(ref - timedelta(days=1))
    w7 = ctx.cached("kpi_wait_times", ref - timedelta(days=6), ref)
    w7_prev = ctx.cached("kpi_wait_times", ref - timedelta(days=13), ref - timedelta(days=7))
    queue = _queue(now.strftime(FMT))
    alerts = ctx.get_agent().alerts()
    critical = [a for a in alerts if a.severity == "crítica"]

    # --- 1. Cuatro indicadores del día ---
    d_occ = occ["pct"] - occ_prev["pct"]
    occ_color = occupancy_color(occ["pct"])
    avg, prev_avg = w7["promedio_min"], w7_prev["promedio_min"]
    wait_delta = f"{avg - prev_avg:+.0f} min vs. semana previa" if avg and prev_avg else None
    t2 = w7["por_triage"].query("triage == 'Triage 2'")
    t2_txt = f"Triage II: {fmt_minutes(t2['espera_promedio_min'].iat[0])} · meta {fmt_num(config.WAIT_TARGET_TRIAGE2_MIN)} min" \
        if not t2.empty else "Sin atenciones Triage II"
    levels = queue["nivel_triage"].value_counts() if not queue.empty else {}
    n_q = len(queue)
    queue_bar = _bar([(levels.get(k, 0) / n_q if n_q else 0, TRIAGE_COLOR[k]) for k in (1, 2, 3, 4, 5)])
    queue_foot = " · ".join(f"{int(levels.get(k, 0))} T{ROMAN[k]}" for k in (1, 2, 3, 4) if levels.get(k, 0)) \
        or "Sin pacientes en espera"
    cats = sorted({a.category for a in critical})
    st.markdown('<div class="stat-grid">' + "".join([
        _stat("Ocupación de camas físicas", f"{fmt_num(occ['pct'], 1)} %", f"({occ['ocupadas']}/{occ['capacidad']})",
              "🛏️", occ_color, "#EFF6FF", _bar([(occ["pct"] / 100, occ_color)]),
              f"{occ['libres']} libres · {occ['expansion_en_uso']} en camas de expansión",
              f"{d_occ:+.1f} pts vs. ayer", d_occ <= 0),
        _stat("Espera en urgencias · 7 días", fmt_minutes(avg), "", "⏱️", AMBER, "#FFF7ED",
              _bar([(min((avg or 0) / 120, 1), AMBER)]), t2_txt, wait_delta,
              bool(avg and prev_avg and avg <= prev_avg)),
        _stat("Pacientes esperando atención", fmt_num(n_q), "en urgencias", "🚑", RED if levels.get(1, 0) or
              levels.get(2, 0) else BLUE, "#FEF2F2", queue_bar, queue_foot),
        _stat("Alertas críticas", fmt_num(len(critical)), "activas", "⚠️", RED if critical else EMERALD, "#FEF9C3",
              _bar([(min(len(critical) / 5, 1), RED if critical else EMERALD)]),
              " · ".join(cats) if cats else "Operación sin alertas críticas"),
    ]) + "</div>", unsafe_allow_html=True)

    if role == "ADMIN":
        # Gerencia: primero las decisiones (alertas con acción) y la presión de urgencias
        left, right = st.columns([1.35, 1], gap="large")
        with left:
            _actions(alerts, role)
        with right:
            _queue_panel(queue, now)
        left, right = st.columns([1.35, 1], gap="large")
        with left:
            _saturation(ref)
        with right:
            _role_todo(role, queue, occ, per_row=2)
    else:
        # Personal asistencial: primero su trabajo del turno, luego la cola y el contexto del hospital
        _role_todo(role, queue, occ, per_row=4)
        left, right = st.columns([1, 1.35], gap="large")
        with left:
            _queue_panel(queue, now)
        with right:
            _actions(alerts, role)
        _saturation(ref)


# ---------------------------------------------------------------------------
# Bloques del personal
# ---------------------------------------------------------------------------
def _actions(alerts, role: str) -> None:
    st.markdown("#### Requiere acción ahora")
    wanted = {"ADMIN": {"Ocupación", "Farmacia", "Urgencias", "Demanda", "Cirugías"},
              "DOCTOR": {"Ocupación", "Urgencias", "Farmacia"},
              "ENFERMERIA": {"Ocupación", "Farmacia", "Urgencias"}}.get(role, set())
    top = [a for a in alerts if a.severity in ("crítica", "alta") and a.category in wanted][:3]
    if not top:
        st.success("Sin alertas críticas ni altas en este momento.", icon="✅")
        return
    beds = _beds(ctx.ref_date().isoformat())
    for i, a in enumerate(top):
        with st.container(border=True):
            detail = a.detail if len(a.detail) <= 170 else a.detail[:167].rsplit(" ", 1)[0] + "…"
            st.markdown(f'{severity_pill(a.severity)} <span class="muted">{esc(a.category)}</span><br>'
                        f'<b>{esc(a.title)}</b><br><span class="muted">{esc(detail)}</span>'
                        f'<div class="alert-action" style="margin-top:0.4rem"><b>Acción:</b> {esc(a.action)}</div>',
                        unsafe_allow_html=True)
            if a.category == "Ocupación":
                unit = next((u for u in beds["subgrupo_cama"].unique() if a.title.startswith(str(u).title())), None)
                if unit is not None:
                    pop = db.bed_population(unit)
                    free = beds[(beds["ocupada"] == 0) & (beds["es_virtual"] == 0) & (beds["poblacion"] == pop)]
                    if free.empty:
                        st.markdown(f'<div class="bed-sug">Sin camas físicas libres para población '
                                    f'{esc(pop.lower())}: habilitar camas de expansión.</div>', unsafe_allow_html=True)
                    else:
                        where = " · ".join(f"<b>{esc(r.ubicacion)}</b>" for r in free.head(3).itertuples())
                        st.markdown(f'<div class="bed-sug">🛏️ Camas libres compatibles ({esc(pop.lower())}): '
                                    f'{where}</div>', unsafe_allow_html=True)
                _link("camas", "Ver en el mapa de camas", "🗺️")
            elif a.category == "Farmacia":
                _link("alertas", "Abrir semáforo y orden de compra", "💊")
            else:
                _link("alertas", "Ver acciones recomendadas", "🧭")
    _link("alertas", "Ver todas las alertas", "🚨")


def _queue_panel(queue, now: datetime) -> None:
    st.markdown("#### Cola de urgencias")
    st.caption(f"Pacientes que llegaron y aún no reciben la primera atención a las {now:%H:%M} del "
               f"{now:%d/%m}. Orden: nivel de triage y tiempo de espera.")
    if queue.empty:
        st.info("Nadie espera atención en este momento del reloj clínico. El extracto del HIS llega hasta el "
                "21/09 a las 14:33: cambia la hora en el 🕒 Reloj de demo.")
        return
    rows = []
    for r in queue.head(6).itertuples():
        lvl = int(r.nivel_triage) if pd.notna(r.nivel_triage) else None
        color, bg = TRIAGE_COLOR.get(lvl, MUTED), TRIAGE_BG.get(lvl, "#F3F4F6")
        tag = f"Triage {ROMAN[lvl]}" if lvl else "Sin triage"
        late = bool(r.fuera_de_meta)
        wait_color = RED if late else (AMBER if r.espera_min >= 30 else EMERALD)
        age = f" · {esc(r.grupo_etario)}" if isinstance(r.grupo_etario, str) else ""
        rows.append(
            f'<div class="q-row"><div class="q-av">👤</div><div class="q-main">'
            f'<span class="q-code">{esc(r.codigo)}</span>'
            f'<span class="q-tag" style="color:{color};background:{bg}">{tag}</span>'
            f'<div class="q-sub">{esc(r.area)}{age} · llegó {esc(r.llegada)}</div></div>'
            f'<div class="q-wait"><small>{"Fuera de meta" if late else "Espera"}</small>'
            f'<b style="color:{wait_color}">{fmt_minutes(r.espera_min)}</b></div></div>')
    st.markdown("".join(rows), unsafe_allow_html=True)
    if len(queue) > 6:
        with st.expander(f"Ver los {len(queue) - 6} pacientes restantes"):
            st.dataframe(queue.iloc[6:][["codigo", "nivel_triage", "area", "llegada", "espera_min"]],
                         hide_index=True, width="stretch")
    st.caption("Códigos URG-xxxx no reversibles: la cola no muestra nombres ni documentos.")


def _saturation(ref) -> None:
    st.markdown("#### Saturación por servicio")
    beds = _beds(ref.isoformat())
    phys = beds[(beds["es_virtual"] == 0) & (beds["servicio"] != "Urgencias")]
    by = (phys.groupby("servicio").agg(cap=("ocupada", "size"), occ=("ocupada", "sum")).reset_index())
    by["pct"] = by["occ"] / by["cap"] * 100
    by = by.sort_values("pct")
    fig = go.Figure(go.Bar(
        x=by["pct"], y=by["servicio"], orientation="h", marker_color=[occupancy_color(p) for p in by["pct"]],
        text=[f"{o}/{c}" for o, c in zip(by["occ"], by["cap"])], textposition="outside",
        textfont=dict(size=11, color=MUTED), cliponaxis=False,
        hovertemplate="%{y}: %{x:.1f} %<extra></extra>"))
    fig.add_vline(x=config.OCCUPANCY_WARNING_PCT, line_dash="dot", line_color=AMBER, line_width=1.5)
    fig.update_xaxes(range=[0, 115], title="% de camas físicas ocupadas")
    fig = style_fig(fig, 330)
    fig.update_layout(margin=dict(l=10, r=40, t=10, b=10))
    show(fig, key="hoy_saturation")
    st.caption(f"Línea punteada: umbral de alerta ({fmt_num(config.OCCUPANCY_WARNING_PCT)} %). "
               "Censo del día de corte, sin camas virtuales de expansión.")


def _todo_html(n, title: str, sub: str) -> str:
    return (f'<div class="todo"><div class="todo-n">{esc(n)}</div><div class="todo-t">{esc(title)}</div>'
            f'<div class="todo-s">{esc(sub)}</div></div>')


def _role_todo(role: str, queue, occ: dict, per_row: int = 2) -> None:
    """Pendientes del rol: (cifra, título, detalle, sección destino, texto del enlace)."""
    clin, now = ctx.get_clin(), ctx.clock()
    if role == "ADMIN":
        red = sum(1 for _ in ps.stock_semaphore(clin, only=("ROJO",)))
        units = ctx.cached("kpi_bed_occupancy", ctx.ref_date(), by="subgrupo_cama")
        hot = int((units.query("servicio != 'Urgencias'")["porcentaje_ocupacion"]
                   >= config.OCCUPANCY_WARNING_PCT).sum())
        breaks = clin.execute("SELECT COUNT(*) FROM auditoria_accesos WHERE acceso_emergencia = 1").fetchone()[0]
        items = [(red, "Ítems en rojo", "semáforo de farmacia (incluye reservas)", "alertas", "Orden de compra →"),
                 (hot, "Unidades sobre el umbral", f"≥ {fmt_num(config.OCCUPANCY_WARNING_PCT)} % de ocupación",
                  "camas", "Mapa de camas →"),
                 (breaks, "Accesos de emergencia", "“romper el vidrio” registrados", "datos", "Bitácora →"),
                 (occ["expansion_en_uso"], "Pacientes en expansión", f"de {occ['expansion_total']} camas virtuales",
                  "camas", "Ver unidades →")]
    elif role == "DOCTOR":
        reeval = clin.execute("SELECT COUNT(*) FROM citas WHERE motivo = 'REEVALUACION_FORMULA' "
                              "AND estado = 'PROGRAMADA'").fetchone()[0]
        active = sum(p["formulas_activas"] for p in ps.clinical_patients(clin))
        urgent = int((queue["nivel_triage"] <= 2).sum()) if not queue.empty else 0
        items = [(reeval, "Citas de reevaluación", "fórmulas caducadas por revisar", "clinico", "Atender →"),
                 (active, "Fórmulas activas", "pacientes con historia abierta", "clinico", "Historias →"),
                 (urgent, "Triage I–II esperando", "atención prioritaria en urgencias", None, ""),
                 (occ["libres"], "Camas físicas libres", "para decidir hospitalizaciones", "camas", "Ubicar cama →")]
    else:  # ENFERMERIA
        q = ps.dispensing_queue(clin)
        soon = sum(1 for r in q if r["ambito"] == "AMBULATORIA"
                   and datetime.strptime(r["fecha_limite_reclamo"], FMT) - datetime.strptime(now, FMT)
                   < timedelta(hours=24))
        hosp = sum(1 for r in q if r["ambito"] == "HOSPITALARIA")
        items = [(len(q), "Fórmulas por entregar", "cola de dispensación", "clinico", "Dispensar →"),
                 (soon, "Vencen en menos de 24 h", "si no se reclaman vuelven a stock", "clinico", "Priorizar →"),
                 (hosp, "Órdenes hospitalarias", "dosis a administrar en piso", "clinico", "Ver órdenes →"),
                 (occ["libres"], "Camas físicas libres", "para recibir traslados", "camas", "Ubicar cama →")]
    st.markdown("#### Mis pendientes")
    for start in range(0, len(items), per_row):
        cols = st.columns(per_row)
        for col, (n, title, sub, slug, label) in zip(cols, items[start:start + per_row]):
            with col:
                st.markdown(_todo_html(n, title, sub), unsafe_allow_html=True)
                if slug:
                    _link(slug, label)


# ---------------------------------------------------------------------------
# Paciente
# ---------------------------------------------------------------------------
def _patient_today() -> None:
    user = ctx.current_user()
    id_paciente = user["id_paciente"]
    decision = ctx.authorize_once("portal.propio", id_paciente)
    if not decision.allowed:
        st.error(decision.reason)
        return
    clin, now = ctx.get_clin(), _now()
    rx = ps.patient_prescriptions(clin, id_paciente)
    appts = ps.patient_appointments(clin, id_paciente)
    pending = [r for r in rx if r["estado"] in ("VIGENTE", "PARCIAL") and r["ambito"] == "AMBULATORIA"]
    blocked = [r for r in rx if r["estado"] == "CADUCADA" and r["requiere_reevaluacion"]]
    upcoming = sorted((c for c in appts if c["estado"] == "PROGRAMADA"), key=lambda c: c["fecha_hora"])

    st.markdown(f'<div class="today-head"><div><h2>Hola, {esc(user["nombre_mostrado"])} 👋</h2>'
                f'<p>{esc(_long_date(now.date())).capitalize()} · esto es lo que tienes pendiente</p></div></div>',
                unsafe_allow_html=True)

    if pending:
        nearest = min(pending, key=lambda r: r["fecha_limite_reclamo"])
        hours = (datetime.strptime(nearest["fecha_limite_reclamo"], FMT) - now).total_seconds() / 3600
        rx_value, rx_foot = str(len(pending)), (f"La primera vence en {hours:.0f} h" if hours < 48
                                                else f"La primera vence en {hours / 24:.1f} días")
        rx_color = RED if hours < 24 else AMBER
    else:
        rx_value, rx_foot, rx_color = "0", "No tienes medicamentos pendientes", EMERALD
    if upcoming:
        nxt = datetime.strptime(upcoming[0]["fecha_hora"], FMT)
        ap_value, ap_foot = f"{nxt:%d/%m}", f"{nxt:%H:%M} · {upcoming[0]['especialidad'].title()}"
    else:
        ap_value, ap_foot = "—", "Sin citas programadas"
    st.markdown('<div class="stat-grid" style="grid-template-columns:repeat(3,minmax(0,1fr))">' + "".join([
        _stat("Medicamentos por reclamar", rx_value, "fórmula" if rx_value == "1" else "fórmulas", "💊", rx_color, "#FFF7ED",
              _bar([(1.0 if pending else 0, rx_color)]), rx_foot),
        _stat("Próxima cita", ap_value, "", "📅", BLUE, "#EFF6FF", _bar([(1.0 if upcoming else 0, BLUE)]), ap_foot),
        _stat("Acciones pendientes", str(len(blocked)), "por resolver", "✅", RED if blocked else EMERALD, "#F0FDF4",
              _bar([(1.0 if blocked else 0, RED if blocked else EMERALD)]),
              "Pide cita para renovar tu fórmula" if blocked else "Todo al día"),
    ]) + "</div>", unsafe_allow_html=True)

    st.markdown("#### Qué hacer hoy")
    steps = []
    for r in pending:
        limit = datetime.strptime(r["fecha_limite_reclamo"], FMT)
        steps.append(("💊", f"Reclama **{r['producto'].capitalize()}** en farmacia antes del "
                            f"**{limit:%d/%m a las %H:%M}** ({r['dosis_prescritas'] - r['dosis_entregadas']} dosis pendientes)."))
    for r in blocked:
        steps.append(("📅", f"Tu fórmula de **{r['producto'].capitalize()}** venció. Solicita una cita de "
                            "reevaluación para volver a recibirla."))
    for c in upcoming[:2]:
        when = datetime.strptime(c["fecha_hora"], FMT)
        steps.append(("🩺", f"Asiste a tu cita de **{c['especialidad'].title()}** el **{when:%d/%m a las %H:%M}**."))
    if not steps:
        st.success("No tienes nada pendiente. ¡Que estés muy bien!", icon="🌿")
    for icon, text in steps:
        with st.container(border=True):
            st.markdown(f"{icon}  {text}")
    _link("portal", "Ver mis fórmulas y citas", "🧑")
    st.caption("Solo tú ves esta información. El personal del hospital accede a ella con registro en la bitácora.")
