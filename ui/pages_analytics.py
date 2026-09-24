"""
ui/pages_analytics.py — Secciones A (Tablero), B (Asistente IA), C (Alertas) y F (Datos y método).
La lógica analítica sigue en database.py / agent.py; aquí solo se presenta según los permisos.
"""
from __future__ import annotations

from datetime import timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import config
import database as db
import pharmacy_service as ps
import reports
from agent import KEY_QUESTIONS, AgentResponse, fmt_minutes, fmt_num
from ui import chat_bubble as chat
from ui import context as ctx
from ui.session import LOGO_ICON
from ui.theme import (AMBER, ACTIVITY_SEQ, BLUE, BLUE_LIGHT, BORDER, EMERALD, GREY, MUTED, RED, SEVERITY,
                      banner, card, chip, esc, grid, kpi_card, occupancy_color, section_title, severity_pill,
                      short_labels, show, style_fig)


def _period_selector():
    """Periodo de análisis en la barra lateral (persistente entre páginas)."""
    ref, first = ctx.ref_date(), ctx.data_start()
    options = ["Últimos 7 días", "Mes en curso", "Últimos 30 días", "Todo el extracto"]
    with st.sidebar:
        st.markdown("**Periodo de análisis**")
        period = st.radio("Periodo", options, index=options.index(st.session_state.get("period", "Mes en curso")),
                          label_visibility="collapsed", key="period_radio")
        st.session_state.period = period
    start = {"Últimos 7 días": ref - timedelta(days=6), "Mes en curso": ref.replace(day=1),
             "Últimos 30 días": ref - timedelta(days=29), "Todo el extracto": first}[period]
    st.sidebar.caption(f"Del {start:%d/%m/%Y} al {ref:%d/%m/%Y} · corte analítico")
    return start, ref


# ===========================================================================
# A. TABLERO
# ===========================================================================
def page_tablero() -> None:
    start, end = _period_selector()
    managerial, beds = ctx.can("tablero.gerencial.ver"), ctx.can("camas.ver")

    # --- 1.1 KPIs directivos: solo cifras ---
    if managerial:
        occ_global = ctx.cached("kpi_global_occupancy", end)
        waits = ctx.cached("kpi_wait_times", start, end)
        span = (end - start).days + 1
        prev = ctx.cached("kpi_wait_times", start - timedelta(days=span), start - timedelta(days=1))
        crit = ctx.cached("kpi_critical_stock", limit=10_000)
        surg = ctx.cached("kpi_surgeries", start, end)
        adm = ctx.cached("kpi_admissions_daily", start, end)
        delta, good = None, True
        if waits["promedio_min"] and prev["promedio_min"]:
            diff = waits["promedio_min"] - prev["promedio_min"]
            delta, good = f"{diff:+.0f} min vs. anterior", diff <= 0
        total_adm = int(adm["ingresos"].sum()) if not adm.empty else 0
        pct = surg["cumplimiento_pct"]
        st.markdown('<div class="kpi-grid">' + "".join([
            kpi_card(f"Ocupación · {end:%d/%m}", f"{fmt_num(occ_global['porcentaje'], 1)} %",
                     f"{occ_global['ocupadas']} de {occ_global['capacidad']} camas",
                     tone=occupancy_color(occ_global["porcentaje"]),
                     hint="Camas físicas con paciente en el día. Excluye camas virtuales de Urgencias."),
            kpi_card("Espera en urgencias", fmt_minutes(waits["promedio_min"]),
                     f"Mediana {fmt_minutes(waits['mediana_min'])} · P90 {fmt_minutes(waits['p90_min'])}",
                     tone=BLUE, delta=delta, delta_good=good),
            kpi_card("Stock crítico", f"{fmt_num(len(crit))} ítems", "< 5 días · stock simulado",
                     tone=RED if len(crit) else EMERALD),
            kpi_card("Cumplimiento quirúrgico", f"{fmt_num(pct, 1)} %" if pct is not None else "N/D",
                     f"{surg['realizadas']} de {surg['programadas']} programadas",
                     tone=EMERALD if (pct or 0) >= config.SURGERY_COMPLIANCE_MIN_PCT else AMBER),
            kpi_card("Ingresos", fmt_num(total_adm), f"{fmt_num(total_adm / span, 1)} por día", tone=BLUE),
        ]) + "</div>", unsafe_allow_html=True)
        critical = [a for a in ctx.alerts() if a.severity == "crítica"]
        if critical:
            c1, c2 = st.columns([3, 1], vertical_alignment="center")
            c1.markdown(chip(f"{len(critical)} alertas críticas activas", "danger", "●") + " "
                        + " · ".join(esc(a.title) for a in critical[:2]), unsafe_allow_html=True)
            if "alertas" in ctx.PAGES:
                c2.page_link(ctx.PAGES["alertas"], label="Ver acciones recomendadas", icon=":material/arrow_forward:")

    # --- 1.2 y 1.3 en sub-pestañas ---
    if beds:
        # Selector en lugar de pestañas: st.tabs calcula TODAS las secciones en cada clic; así solo la visible.
        sections = {"Tendencias": lambda: _trends_section(start, end), "Capacidad y camas": lambda: _beds_section(end),
                    "Urgencias y espera": lambda: _emergency_section(start, end),
                    "Consumo de medicamentos": lambda: _consumption_section(start, end),
                    "Ingresos": lambda: _admissions_list(start, end)}
        choice = st.segmented_control("Sección", list(sections), default="Tendencias", key="ind_section",
                                      label_visibility="collapsed") or "Tendencias"
        sections[choice]()

    # --- 1.4 colapsable ---
    if managerial:
        with st.expander("Epidemiología y producción", expanded=False):
            if st.toggle("Mostrar", key="show_epi"):
                _epidemiology_section(start, end)

    if not (managerial or beds):
        st.info("Tu rol no tiene indicadores asignados en el tablero.")


def _beds_section(end) -> None:
    occ = ctx.cached("kpi_bed_occupancy", end, by="subgrupo_cama")
    occ = occ[occ["servicio"] != "Urgencias"].sort_values("porcentaje_ocupacion")
    left, right = st.columns([1.15, 1], gap="medium")
    with left:
        fig = go.Figure(go.Bar(
            x=occ["porcentaje_ocupacion"], y=occ["subgrupo_cama"].map(db.unit_label), orientation="h",
            marker_color=[occupancy_color(p) for p in occ["porcentaje_ocupacion"]],
            text=[f"{int(o)}/{int(c)}" for o, c in zip(occ["camas_ocupadas"], occ["capacidad"])],
            textposition="outside", textfont=dict(size=11, color=MUTED), cliponaxis=False,
            hovertemplate="%{y}: %{x:.1f} %<extra></extra>"))
        fig.add_vline(x=config.OCCUPANCY_WARNING_PCT, line_dash="dot", line_color=AMBER, line_width=1.5)
        fig.update_xaxes(range=[0, 118], title="% ocupación")
        fig = style_fig(fig, 460, f"Ocupación por unidad funcional · {end:%d/%m/%Y}")
        fig.update_layout(margin_r=48)
        show(fig)
    with right:
        conn = ctx.get_conn()
        services = [r[0] for r in conn.execute("SELECT DISTINCT servicio FROM camas ORDER BY 1")]
        service = st.selectbox("Servicio", services, index=services.index("UCI") if "UCI" in services else 0)
        lo, hi = db.day_bounds(end, end)
        beds = pd.read_sql_query("""
            SELECT c.codigo_cama AS cama, c.subgrupo_cama AS unidad,
                   CASE WHEN o.codigo_cama IS NULL THEN 'Libre' ELSE 'Ocupada' END AS estado,
                   CASE WHEN c.es_virtual = 1 THEN 'Virtual' ELSE 'Física' END AS tipo
              FROM camas c LEFT JOIN (SELECT DISTINCT codigo_cama FROM ingresos
                                      WHERE fecha_inicio_estancia <= ? AND fecha_fin_estimada >= ?) o
                ON o.codigo_cama = c.codigo_cama
             WHERE c.servicio = ? ORDER BY estado DESC, unidad, cama""", conn, params=(hi, lo, service))
        occupied = int((beds["estado"] == "Ocupada").sum())
        st.markdown(" ".join([chip(f"{len(beds)} camas", "neutral"), chip(f"{occupied} ocupadas", "warn"),
                              chip(f"{len(beds) - occupied} libres", "ok")]), unsafe_allow_html=True)
        only_free = st.toggle("Mostrar solo camas libres")
        view = beds[beds["estado"] == "Libre"] if only_free else beds
        st.dataframe(view, hide_index=True, width="stretch", height=330)
    st.markdown('<div class="chart-note">Censo del día de corte. El HIS registra la última cama de cada episodio: '
                'la foto del día es confiable; la serie histórica subestima unidades de paso.</div>',
                unsafe_allow_html=True)


@st.cache_data(ttl=600, show_spinner=False)
def _physical_trend(start_iso: str, end_iso: str) -> pd.DataFrame:
    from datetime import date
    return db.physical_occupancy_trend(ctx.get_conn(), date.fromisoformat(start_iso), date.fromisoformat(end_iso))


def _trends_section(start, end) -> None:
    """Las dos gráficas que pide el reto: evolución de la ocupación (línea) y distribución por servicio (pastel)."""
    left, right = st.columns([1.5, 1], gap="medium")
    with left:
        span_start = min(start, end - timedelta(days=13))  # al menos dos semanas para ver la tendencia
        trend = _physical_trend(span_start.isoformat(), end.isoformat())
        by_service = st.toggle("Ver por servicio", key="trend_by_service")
        if by_service:
            fig = px.line(trend, x="fecha", y="porcentaje_ocupacion", color="servicio",
                          color_discrete_sequence=ACTIVITY_SEQ,
                          labels={"porcentaje_ocupacion": "% ocupación", "fecha": "", "servicio": ""})
        else:
            total = trend.groupby("fecha", as_index=False)[["camas_ocupadas", "capacidad"]].sum()
            total["porcentaje_ocupacion"] = (total["camas_ocupadas"] / total["capacidad"] * 100).round(1)
            fig = go.Figure(go.Scatter(x=total["fecha"], y=total["porcentaje_ocupacion"], mode="lines+markers",
                                       line=dict(color=BLUE, width=3),
                                       customdata=total[["camas_ocupadas", "capacidad"]],
                                       hovertemplate="%{x}: %{y:.1f} % (%{customdata[0]}/%{customdata[1]})"
                                                     "<extra></extra>"))
            fig.update_yaxes(title="% ocupación")
        fig.add_hline(y=config.OCCUPANCY_WARNING_PCT, line_dash="dot", line_color=AMBER, line_width=1.5)
        fig.update_yaxes(range=[0, 105])
        show(style_fig(fig, 360, "Evolución de la ocupación de camas físicas"))
    with right:
        adm = ctx.cached("kpi_admissions_by_service", start, end)
        pie = adm.groupby("servicio", as_index=False)["pacientes"].sum().sort_values("pacientes", ascending=False)
        if len(pie) > 6:
            top, rest = pie.head(5), pie.iloc[5:]
            pie = pd.concat([top, pd.DataFrame({"servicio": ["Otros"], "pacientes": [rest["pacientes"].sum()]})])
        fig = px.pie(pie, names="servicio", values="pacientes", hole=0.5, color_discrete_sequence=ACTIVITY_SEQ)
        fig.update_traces(textinfo="percent", sort=False)
        show(style_fig(fig, 360, "Pacientes por servicio"))
    st.caption(f"Periodo del menú lateral ({start:%d/%m} al {end:%d/%m}). Línea punteada: umbral de alerta "
               f"({fmt_num(config.OCCUPANCY_WARNING_PCT)} %). La ocupación usa camas físicas, igual que la página Hoy.")


def _consumption_section(start, end) -> None:
    """Mayor y menor rotación de medicamentos e insumos en el periodo (dispensación real del HIS)."""
    kind = st.segmented_control("Tipo", ["Medicamento", "Insumo / dispositivo"], default="Medicamento",
                                key="cons_kind") or "Medicamento"
    left, right = st.columns(2, gap="medium")
    for col, lowest, title, color in ((left, False, "Mayor rotación", BLUE), (right, True, "Menor rotación", GREY)):
        with col:
            df = ctx.cached("kpi_item_consumption", start, end, 10, item_type=kind, lowest=lowest)
            if df.empty:
                st.caption("Sin dispensaciones en el periodo.")
                continue
            df = df.assign(item=short_labels(df["nombre"].str.capitalize(), 42)).sort_values(
                "unidades", ascending=lowest)
            fig = go.Figure(go.Bar(x=df["unidades"], y=df["item"], orientation="h", marker_color=color,
                                   hovertemplate="%{y}: %{x:,.0f} unidades<extra></extra>"))
            fig.update_xaxes(title="unidades dispensadas")
            show(style_fig(fig, 380, title), key=f"cons_{lowest}")
    st.caption("Unidades dispensadas en el periodo del menú lateral. Los ítems sin ningún movimiento no aparecen "
               "aquí; se consultan en Inventario.")


def _admissions_list(start, end) -> None:
    """Listado de ingresos sin datos sensibles (sin nombre, documento, ni identificadores)."""
    lo, hi = db.day_bounds(start, end)
    df = pd.read_sql_query("""
        SELECT date(fecha_ingreso) AS fecha_ingreso, servicio, subgrupo_cama AS unidad, via_ingreso,
               CASE WHEN nivel_triage IS NULL THEN '—' ELSE 'Triage ' || nivel_triage END AS triage,
               capitulo_cie10 AS grupo_diagnostico, ROUND(tiempo_espera_min) AS espera_min,
               ROUND(estancia_horas / 24.0, 1) AS estancia_dias
          FROM ingresos WHERE fecha_ingreso BETWEEN ? AND ? ORDER BY fecha_ingreso DESC LIMIT 3000""",
                           ctx.get_conn(), params=(lo, hi))
    c1, c2 = st.columns(2)
    services = c1.multiselect("Servicio", sorted(df["servicio"].dropna().unique()), key="list_srv")
    groups = c2.multiselect("Grupo de diagnóstico", sorted(df["grupo_diagnostico"].dropna().unique()),
                            key="list_dx")
    view = df[df["servicio"].isin(services)] if services else df
    view = view[view["grupo_diagnostico"].isin(groups)] if groups else view
    view = view.assign(unidad=view["unidad"].map(db.unit_label))
    st.dataframe(view, hide_index=True, width="stretch", height=380)
    st.caption(f"{fmt_num(len(view))} ingresos. Sin nombre, documento ni diagnóstico específico: solo el grupo "
               "CIE-10. El extracto no trae médico asignado.")


def _emergency_section(start, end) -> None:
    waits = ctx.cached("kpi_wait_times", start, end)
    bt, heat = waits["por_triage"], waits["por_turno_triage"]
    t2 = bt.query("triage == 'Triage 2'")
    if not t2.empty:
        value = t2["espera_promedio_min"].iat[0]
        ok = value <= config.WAIT_TARGET_TRIAGE2_MIN
        st.markdown(chip(f"Triage 2: {fmt_minutes(value)} · meta ≤ {fmt_num(config.WAIT_TARGET_TRIAGE2_MIN)} min",
                         "ok" if ok else "warn", "●"), unsafe_allow_html=True)
    left, right = st.columns(2, gap="medium")
    with left:
        if not bt.empty:
            late = (bt["triage"] == "Triage 2") & (bt["espera_promedio_min"] > config.WAIT_TARGET_TRIAGE2_MIN)
            fig = go.Figure(go.Bar(
                x=bt["triage"], y=bt["espera_promedio_min"], marker_color=[AMBER if f else BLUE for f in late],
                text=[f"{fmt_num(a)} atenc." for a in bt["atenciones"]], textposition="outside",
                textfont=dict(size=11, color=MUTED), cliponaxis=False,
                hovertemplate="%{x}: %{y:.1f} min<extra></extra>"))
            fig.add_hline(y=config.WAIT_TARGET_TRIAGE2_MIN, line_dash="dot", line_color=AMBER, line_width=1.5)
            fig.update_yaxes(title="minutos", range=[0, bt["espera_promedio_min"].max() * 1.18])
            show(style_fig(fig, 360, "Espera por nivel de triage · línea: meta T2"))
    with right:
        if not heat.empty:
            pv = heat.pivot_table(index="turno", columns="triage", values="espera_promedio_min").reindex(
                ["Mañana", "Tarde", "Noche"])
            fig = px.imshow(pv, text_auto=".0f", aspect="auto", color_continuous_scale=["#FFFFFF", "#FDE68A", AMBER, RED])
            fig.update_layout(coloraxis_colorbar=dict(title="min", thickness=10))
            fig.update_xaxes(title=None)
            fig.update_yaxes(title=None)
            show(style_fig(fig, 360, "Causa raíz: espera (min) por turno y triage"))


def _epidemiology_section(start, end) -> None:
    demo = ctx.cached("kpi_demographics", start, end)
    c1, c2 = st.columns(2, gap="medium")
    with c1:
        fig = px.pie(demo["sexo"], names="categoria", values="pacientes", hole=0.55,
                     color_discrete_sequence=[BLUE, EMERALD])
        fig.update_traces(textinfo="percent", textfont=dict(color="white"))
        show(style_fig(fig, 300, "Pacientes por sexo"))
    with c2:
        fig = go.Figure(go.Bar(x=demo["regimen"]["categoria"], y=demo["regimen"]["pacientes"], marker_color=BLUE))
        show(style_fig(fig, 300, "Pacientes por régimen"))
    beddays = ctx.cached("kpi_billed_bed_days", ctx.data_start(), ctx.ref_date())
    if not beddays.empty:
        fig = px.bar(beddays, x="mes", y="dias_cama", color="unidad", barmode="group",
                     color_discrete_sequence=ACTIVITY_SEQ, labels={"dias_cama": "días-cama", "mes": ""})
        show(style_fig(fig, 360, "Días-cama facturados por mes (CUPS de internación)"))
    st.markdown('<div class="chart-note">Diagnósticos, especialidades y rotación de medicamentos se consultan '
                'en el Asistente IA para no saturar el tablero.</div>', unsafe_allow_html=True)


# ===========================================================================
# B. ASISTENTE IA
# ===========================================================================
ENGINE_LABEL = {"llm": "LLM (NL2SQL)", "reglas": "Plan B · SQL validado",
                "reglas (respaldo)": "Plan B de respaldo (el LLM falló)", "seguridad": "Bloqueado",
                "sql directo": "⌨️ SQL escrito por el usuario (validado)"}


def _render_chart(df: pd.DataFrame, spec: dict | None, key: str) -> None:
    if not spec or df is None or df.empty or spec.get("x") not in df or spec.get("y") not in df:
        return
    x, y, color = spec["x"], spec["y"], spec.get("color")
    color = color if color in df else None
    pretty = {c: c.replace("_", " ").capitalize() for c in df.columns}
    data = df.head(25)
    if spec["type"] == "line":
        fig, height = px.line(df, x=x, y=y, color=color, markers=True, labels=pretty,
                              color_discrete_sequence=ACTIVITY_SEQ), 320
    elif spec["type"] == "pie":
        fig, height = px.pie(data, names=x, values=y, hole=0.5, color_discrete_sequence=ACTIVITY_SEQ), 320
    else:
        data = data.assign(**{x: short_labels(data[x])}).sort_values(y)
        colors = [occupancy_color(v) for v in data[y]] if "ocupacion" in y else BLUE
        fig = go.Figure(go.Bar(x=data[y], y=data[x], orientation="h", marker_color=colors,
                               hovertemplate="%{y}: %{x:,.1f}<extra></extra>"))
        fig.update_layout(xaxis_title=pretty[y], yaxis_title=None)
        height = max(240, 30 * len(data) + 70)
    show(style_fig(fig, height), key=key)


def _render_response(resp: AgentResponse, idx: int) -> None:
    st.markdown(resp.answer)
    for rec in resp.recommendations[:3]:
        st.markdown(f'{severity_pill(rec.severity)} <b>{esc(rec.title)}</b> — {esc(rec.action)}',
                    unsafe_allow_html=True)
    if resp.data is not None and not resp.data.empty and resp.engine != "seguridad":
        _render_chart(resp.data, resp.chart, key=f"chart_{ctx.user_id()}_{idx}")
        if "severity" not in resp.data.columns:
            st.dataframe(resp.data, width="stretch", hide_index=True, height=min(38 * len(resp.data) + 40, 300))
    with st.expander(f"Trazabilidad · {ENGINE_LABEL.get(resp.engine, resp.engine)} · {resp.elapsed_ms} ms",
                     expanded=False):
        st.markdown(chip("Solo lectura (mode=ro)", "ok") + " " + chip("Authorizer SQLite", "ok") + " "
                    + chip("Sin identificadores de pacientes", "ok"), unsafe_allow_html=True)
        if resp.sql:
            st.code(resp.sql, language="sql")
        else:
            st.caption("Respuesta generada por el motor de recomendaciones (sin SQL).")
        if resp.error:
            st.caption(f"Detalle: {resp.error}")


def page_asistente() -> None:
    agent = ctx.get_agent()
    with st.sidebar:
        st.markdown("**Motor del asistente**")
        options = {"Automático (LLM + respaldo)": "llm_first", "Híbrido (reglas primero)": "hybrid",
                   "Solo reglas (Plan B)": "rules"}
        default = 0 if config.LLM_PROVIDER != "none" else 2
        label = st.radio("Motor", list(options), index=default, label_visibility="collapsed", key="engine")
        agent.mode = options[label]
        if agent.llm:
            st.success(f"LLM activo: {agent.llm.name}", icon=":material/psychology:")
        else:
            st.info("Sin LLM configurado: responde el Plan B (SQL validado, sin red).", icon=":material/verified_user:")
    banner("<b>Consultas de solo lectura</b> sobre la base analítica anonimizada (<code>mode=ro</code> + authorizer "
           "SQLite). El asistente no tiene acceso a historias clínicas ni a prescripciones.", "ok")

    labels = ["Camas UCI hoy", "Medicamentos < 5 días", "Espera urgencias (7 d)", "Servicio con más ingresos"]
    cols = st.columns(4)
    for i, q in enumerate(KEY_QUESTIONS):
        if cols[i].button(labels[i], key=f"key_q_{i}", help=q, width="stretch"):
            st.session_state.pending = q

    history = st.session_state.setdefault("history", {}).setdefault(ctx.user_id(), [])
    chat_box = st.container()
    prompt = st.chat_input("Pregunta en lenguaje natural, p. ej.: ¿Qué especialidades son más solicitadas este mes?")
    prompt = prompt or st.session_state.pop("pending", None)
    if prompt:
        with st.spinner("Consultando la base del hospital…"):
            history.append((prompt, chat.ask(prompt)))  # incluye la intención de ubicar camas
    with chat_box:
        if not history:
            st.caption("Usa una de las 4 preguntas del reto o escribe la tuya.")
        for i, (question, resp) in enumerate(history):
            with st.chat_message("user"):
                st.markdown(question)
            with st.chat_message("assistant", avatar=str(LOGO_ICON)):
                _render_response(resp, i)
    if history and st.button("Limpiar conversación"):
        history.clear()
        st.rerun()


# ===========================================================================
# C. ALERTAS Y ACCIONES
# ===========================================================================
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SEM_STYLE = {"ROJO": (RED, "danger", "Urgente"), "AMARILLO": (AMBER, "warn", "Pronto")}
SEM_LEVEL = {"Urgente": "ROJO", "Pronto": "AMARILLO"}


def page_alertas() -> None:
    tab_sem, tab_ops = st.tabs(["Farmacia: qué pedir", "Acciones operativas"])
    with tab_sem:
        clin = ctx.get_clin()
        rows = [dict(r) for r in ps.stock_semaphore(clin)]
        df = pd.DataFrame(rows)
        n_red = int((df["semaforo"] == "ROJO").sum()) if not df.empty else 0
        n_yellow = int((df["semaforo"] == "AMARILLO").sum()) if not df.empty else 0
        c1, c2, c3 = st.columns([1.3, 1.3, 1], vertical_alignment="bottom")
        level = c1.segmented_control("Qué pedir", ["Urgente", "Pronto", "Ambos"], default="Urgente",
                                     help="Urgente: se agota en menos de 5 días. Pronto: entre 5 y 10 días.")
        kind = c2.segmented_control("Tipo", ["Medicamento", "Insumo / dispositivo", "Todos"], default="Todos")
        allowed = ctx.can("farmacia.orden_compra")
        order = reports.purchase_order_xlsx([dict(r) for r in ps.stock_semaphore(clin, ("ROJO",))],
                                            ctx.ref_date().isoformat(), ctx.current_user()["nombre_mostrado"])
        c3.download_button("Orden de compra (Excel)", order, f"orden_compra_{ctx.ref_date().isoformat()}.xlsx",
                           XLSX, icon=":material/download:", type="primary", width="stretch",
                           disabled=not allowed,
                           help=None if allowed else "Exportar la orden requiere el permiso de Administrador")
        query = st.text_input("Buscar ítem", placeholder="Ej.: enoxaparina, catéter, dipirona…")
        st.markdown(chip(f"{n_red} urgentes · se agotan esta semana", "danger", "●") + " "
                    + chip(f"{n_yellow} para pedir pronto", "warn", "●") + " "
                    + chip("Existencias simuladas · consumo real", "neutral"), unsafe_allow_html=True)
        view = df
        if not view.empty:
            if level in SEM_LEVEL:
                view = view[view["semaforo"] == SEM_LEVEL[level]]
            if kind in ("Medicamento", "Insumo / dispositivo"):
                view = view[view["tipo_item"] == kind]
            if query:
                view = view[view["nombre"].str.contains(query, case=False, na=False)]
        if "inventario" in ctx.PAGES:
            st.page_link(ctx.PAGES["inventario"], label="Registrar llegada de pedidos o ajustes en Inventario",
                         icon=":material/inventory_2:")
        cards = []
        for r in view.head(12).itertuples():
            color, tone, label = SEM_STYLE[r.semaforo]
            body = (f"Se usan unas {fmt_num(r.consumo_diario_promedio)} al día."
                    f"<br><b>Recomendado pedir: {fmt_num(r.orden_sugerida_15d)} und.</b>")
            if r.critico_continuidad:
                body += "<br><b>Tratamiento que no se puede suspender:</b> priorizar"
            cards.append(card(r.nombre.capitalize()[:70], body, color, chip(label, tone),
                              big=f"Quedan {fmt_num(r.disponible)} und."))
        if cards:
            grid(cards)
        else:
            st.info("Ningún ítem coincide con el filtro.")
        if len(view) > 12:
            with st.expander(f"Ver los {len(view)} ítems en tabla"):
                table = view[["nombre", "tipo_item", "semaforo", "disponible", "consumo_diario_promedio",
                              "orden_sugerida_15d"]].rename(columns={
                    "nombre": "Ítem", "tipo_item": "Tipo", "semaforo": "Prioridad", "disponible": "Quedan",
                    "consumo_diario_promedio": "Uso por día", "orden_sugerida_15d": "Recomendado pedir"})
                table["Prioridad"] = table["Prioridad"].map({"ROJO": "Urgente", "AMARILLO": "Pronto"})
                st.dataframe(table, hide_index=True, width="stretch", height=360)

    with tab_ops:
        alerts = [a for a in ctx.alerts() if a.category != "Farmacia"]
        chosen = st.pills("Severidad", ["Crítica", "Alta", "Media", "Info"], default=["Crítica", "Alta", "Media"],
                          selection_mode="multi")
        chosen = {c.lower() for c in (chosen or [])}
        counts = pd.Series([a.severity for a in alerts], dtype="object").value_counts()
        st.markdown(" ".join(severity_pill(s) + f" <b>{int(counts.get(s, 0))}</b>" for s in SEVERITY),
                    unsafe_allow_html=True)
        cards_html = []
        for a in alerts:
            if a.severity not in chosen:
                continue
            accent = SEVERITY[a.severity][1]
            cards_html.append(
                f'<div class="alert-card" style="--accent:{accent}"><div class="alert-top">{severity_pill(a.severity)}'
                f'<span class="alert-cat">{esc(a.category)}</span></div><div class="alert-title">{esc(a.title)}</div>'
                f'<div class="alert-detail">{esc(a.detail)}</div><div class="alert-action"><b>Acción:</b> '
                f'{esc(a.action)}</div></div>')
        st.markdown("".join(cards_html) or "Sin alertas para la severidad elegida.", unsafe_allow_html=True)
        st.caption("Las alertas de farmacia se gestionan en el semáforo para no duplicar información.")


# ===========================================================================
# F. DATOS Y MÉTODO
# ===========================================================================
def page_datos() -> None:
    tab_load, tab_audit = st.tabs(["Carga de extractos", "Bitácora de auditoría"])
    with tab_load:
        info = ctx.meta()
        counts = {t: ctx.get_conn().execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("pacientes", "ingresos", "servicios", "medicamentos_insumos", "cirugias")}
        st.markdown(" ".join([chip(f"Corte {info['fecha_referencia']}", "info"),
                              chip(f"Datos desde {info['fecha_min_datos']}", "neutral"),
                              chip(f"Base generada {info.get('construida_en', '')}", "neutral"),
                              chip("Stock simulado" if info.get("stock_simulado") == "1" else "Stock real",
                                   "warn" if info.get("stock_simulado") == "1" else "ok")]), unsafe_allow_html=True)
        grid([card(t.replace("_", " ").capitalize(), "filas", BLUE, big=fmt_num(n)) for t, n in counts.items()])
        section_title("Cargar un nuevo extracto del HIS")
        st.caption("Sube los 7 archivos .txt delimitados por '|' (y opcionalmente Inventario.txt con "
                   "CodigoServicio|Stock). La base analítica se reconstruye en frío; la base clínica no se toca.")
        uploads = st.file_uploader("Archivos", type=["txt"], accept_multiple_files=True,
                                   label_visibility="collapsed")
        if uploads and st.button("Reconstruir base analítica", type="primary"):
            valid = set(db.SOURCE_FILES.values()) | {db.OPTIONAL_STOCK_FILE}
            saved = [f.name for f in uploads if f.name in valid]
            for f in uploads:
                if f.name in valid:
                    (config.DATA_DIR / f.name).write_bytes(f.getbuffer())
            ignored = [f.name for f in uploads if f.name not in valid]
            if ignored:
                st.warning("Se ignoraron archivos con nombre no reconocido: " + ", ".join(ignored))
            if saved:
                ctx.reset_analytics_resources()
                ctx.build_analytics_with_status(f"Reconstruyendo con {len(saved)} archivo(s)...", rebuild=True)
                st.rerun()
        with st.expander("Método de cálculo y supuestos"):
            st.markdown(f"""
- **'Hoy' analítico** = {info['fecha_referencia']}, última fecha de ingreso del extracto.
- **Ocupación:** estancia estimada desde la hospitalización hasta la última prestación registrada (no hay egresos).
  El HIS guarda la última cama del episodio: la foto del día es confiable; la serie histórica subestima UCI.
- **Espera en urgencias:** FechaAtencion − FechaIngreso, entre 0 y 24 h.
- **Inventario:** consumo real de 30 días; existencias simuladas hasta recibir `Inventario.txt`.
- **Módulo clínico:** usuarios, historias, fórmulas y citas son **sintéticos** (el extracto no los trae), sembrados
  sobre pacientes e ingresos reales. Su reloj es independiente y se puede adelantar para la demostración.
""")

    with tab_audit:
        rows = pd.DataFrame([dict(r) for r in ps.audit_log(ctx.get_clin())])
        if rows.empty:
            st.info("Aún no hay accesos registrados. Entra como Dra. Ruiz y abre una historia clínica.")
        else:
            denied = rows["accion"].str.match(r"DENEGADO|LOGIN_FALLIDO|LOGIN_BLOQUEADO")
            emerg = rows["acceso_emergencia"] == 1
            st.markdown(" ".join([chip(f"{len(rows)} eventos", "neutral"),
                                  chip(f"{int((~denied).sum())} permitidos", "ok"),
                                  chip(f"{int(denied.sum())} denegados", "danger"),
                                  chip(f"{int(emerg.sum())} accesos de emergencia", "warn")]), unsafe_allow_html=True)
            only = st.segmented_control("Filtro", ["Todos", "Denegados", "Emergencia"], default="Todos")
            view = rows[denied] if only == "Denegados" else rows[emerg] if only == "Emergencia" else rows
            if view.empty:
                st.info({"Denegados": "Todavía no hay accesos denegados: nadie ha intentado algo que su rol no permite. "
                                      "Pruébalo: entra como Enfermería y pregúntale a la IA qué servicio tiene más "
                                      "pacientes, o intenta ingresar con una contraseña equivocada.",
                         "Emergencia": "Todavía nadie ha usado el acceso de emergencia. Pruébalo: pon el reloj en "
                                       "turno de noche, entra como Dra. Ruiz, abre una historia clínica y usa "
                                       "“Romper el vidrio” con una justificación."}.get(only, "Sin eventos."))
            else:
                st.dataframe(view.rename(columns={"id_paciente": "paciente", "acceso_emergencia": "emergencia"})
                             .fillna("—"), hide_index=True, width="stretch", height=380,
                             column_config={"fecha": "Fecha", "usuario": "Usuario", "rol": "Rol", "accion": "Acción",
                                            "recurso": "Motivo de la decisión", "paciente": "Paciente",
                                            "emergencia": st.column_config.CheckboxColumn("Emergencia"),
                                            "justificacion": "Justificación"})
        with st.expander("Usuarios, estados de cuenta y turnos"):
            users = pd.read_sql_query("""
                SELECT u.nombre_mostrado AS usuario, r.nombre AS rol, u.estado_cuenta, u.registro_profesional,
                       (SELECT tipo || ' ' || servicio || ' hasta ' || substr(fin, 12, 5) FROM turnos t
                         WHERE t.usuario_id = u.id AND ? BETWEEN t.inicio AND t.fin) AS turno_actual
                  FROM usuarios u JOIN roles r ON r.id = u.rol_id ORDER BY u.id""",
                                      ctx.get_clin(), params=(ctx.clock(),))
            st.dataframe(users, hide_index=True, width="stretch")
