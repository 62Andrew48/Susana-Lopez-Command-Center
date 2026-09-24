"""
ui/pages_predicciones.py — Pronósticos de los microservicios (solo gerencia: permiso tablero.gerencial.ver).

Pensada para el administrador del hospital, no para el equipo técnico. La pantalla responde, en este orden:
  1. Resumen para el administrador: qué pasa, en qué periodo, qué cambia, qué recursos podrían verse
     afectados y qué conviene revisar (forecast_text.admin_summary).
  2. Una tarjeta por servicio: periodo, cifra esperada frente a lo habitual, por qué importa, qué revisar,
     qué tan confiable es y los datos recientes que lo respaldan (forecast_text.interpret).
El detalle técnico (error medio, validación, línea base) queda en un desplegable de cada tarjeta.
Arriba, el informe ejecutivo descargable (reports.py) consolida situación actual, pronósticos y compras.
Cada servicio se consulta por separado con tiempo máximo: si uno está apagado, su tarjeta lo dice y las
demás siguen funcionando.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import streamlit as st

import database as db
import forecast_text as ft
import ml_services as ml
import pharmacy_service as ps
import month_report as mr
import reports
from ui import context as ctx
from ui.theme import chip, esc, icon, page_header, section_title

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TONE_COLOR = {"danger": "var(--c-danger)", "warn": "var(--c-warn)", "ok": "var(--c-ok)", "neutral": "var(--border)"}
CSS = """
<style>
  /* Resumen para el administrador */
  .adm {background:var(--surface); border:1px solid var(--border); border-left:5px solid var(--c);
      border-radius:12px; box-shadow:var(--shadow); padding:1rem 1.15rem 0.9rem; margin:0.2rem 0 1.1rem;}
  .adm-title {display:flex; align-items:center; gap:0.5rem; font-size:1.08rem; font-weight:700; color:var(--text);
      margin-bottom:0.15rem;}
  .adm-title .ico {color:var(--c);}
  .adm-lead {font-size:1.02rem; color:var(--text); line-height:1.45; margin:0.2rem 0 0.8rem; font-weight:550;}
  .adm-grid {display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:0.8rem 1.2rem;}
  .adm-q {font-size:0.72rem; font-weight:700; color:var(--muted); text-transform:uppercase; letter-spacing:0.04em;
      display:flex; align-items:center; gap:0.35rem; margin-bottom:0.25rem;}
  .adm-a {font-size:0.87rem; color:var(--text); line-height:1.45;}
  .adm-a ul {margin:0; padding-left:1.05rem;} .adm-a li {margin-bottom:0.2rem;}
  .adm-note {font-size:0.76rem; color:var(--muted); margin-top:0.7rem; border-top:1px solid var(--border);
      padding-top:0.5rem;}

  /* Tarjeta por servicio */
  .ml-card {background:var(--surface); border:1px solid var(--border); border-top:4px solid var(--c);
      border-radius:12px; padding:0.95rem 1.05rem 0.8rem; box-shadow:var(--shadow);}
  .ml-head {display:flex; justify-content:space-between; align-items:center; gap:0.5rem; flex-wrap:wrap;}
  .ml-head b {font-size:1.05rem; color:var(--text);}
  .ml-period {font-size:0.8rem; color:var(--muted); display:flex; align-items:center; gap:0.35rem; margin-top:0.15rem;}
  .ml-fig {display:flex; align-items:baseline; gap:0.5rem; flex-wrap:wrap; margin:0.55rem 0 0.1rem;}
  .ml-fig strong {font-size:2rem; line-height:1; color:var(--text); font-variant-numeric:tabular-nums;}
  .ml-fig span {font-size:0.9rem; color:var(--text-2);}
  .ml-change {display:inline-flex; align-items:center; gap:0.35rem; font-size:0.84rem; font-weight:650;
      border-radius:8px; padding:0.2rem 0.55rem; margin:0.35rem 0 0.1rem;}
  .ml-sub {font-size:0.8rem; color:var(--muted); margin-top:0.2rem; line-height:1.4;}
  .ml-sec {font-size:0.72rem; font-weight:700; color:var(--muted); text-transform:uppercase; letter-spacing:0.04em;
      margin:0.75rem 0 0.2rem;}
  .ml-why {font-size:0.87rem; color:var(--text); line-height:1.45;}
  .ml-list {font-size:0.85rem; color:var(--text); margin:0; padding-left:1.1rem; line-height:1.45;}
  .stApp ul.ml-list {font-size:0.85rem;} .stApp ul.ml-list.facts {font-size:0.82rem;}
  .stApp .adm-a ul {font-size:0.87rem;}
  .stApp .ml-list li, .stApp .adm-a li {margin-bottom:0.15rem; font-size:inherit !important; line-height:1.45;}
  .ml-list.facts {color:var(--text-2); font-size:0.82rem;}
  .ml-tags {display:flex; flex-wrap:wrap; gap:0.3rem; margin-top:0.35rem;}
  .ml-tags span {font-size:0.74rem; border:1px solid var(--border-strong); color:var(--text-2); border-radius:999px;
      padding:0.12rem 0.55rem; background:var(--surface-3);}
  .ml-conf {font-size:0.8rem; margin-top:0.75rem; padding:0.45rem 0.6rem; border-radius:8px; line-height:1.4;
      display:flex; gap:0.45rem; align-items:flex-start;}
  .ml-conf .ico {margin-top:0.1rem;}
  .ml-off {font-size:0.87rem; color:var(--muted); margin-top:0.6rem; line-height:1.45;}
  .glossary {display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:0.6rem 1rem; font-size:0.8rem;
      color:var(--text-2); margin-top:0.4rem;}
  .glossary b {color:var(--text);}
  @media (max-width: 1150px) {.adm-grid, .glossary {grid-template-columns:repeat(2, minmax(0, 1fr));}}
  @media (max-width: 640px) {.adm-grid, .glossary {grid-template-columns:1fr;} .ml-fig strong {font-size:1.7rem;}}
</style>
"""
CONF_TONE = {"ok": "t-ok", "warn": "t-warn"}
CHANGE_ICON = {"up": "trend-up", "down": "trend-down", "flat": "minus"}


@st.cache_data(ttl=60, show_spinner=False)
def _fetch(code: str) -> dict:
    """Predicción del servicio. Cacheada 60 s; los errores no se cachean (se reintentan)."""
    return ml.predict(code)


@st.cache_data(ttl=60, show_spinner=False)
def _fetch_kpis(code: str) -> dict:
    return ml.kpis(code)


def _safe(fn, code):
    try:
        return fn(code)
    except ml.ServiceUnavailable:
        return None


def _prefetch() -> None:
    """Los 4 servicios (predicción y KPI) en paralelo: si están caídos, la espera máxima es 2 s y no 16 s."""
    jobs = [(fn, s.code) for s in ml.SERVICES for fn in (_fetch, _fetch_kpis)]
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        list(pool.map(lambda job: _safe(*job), jobs))


def _urgent_items() -> list[dict]:
    return [dict(r) for r in ps.stock_semaphore(ctx.get_clin(), ("ROJO",))]


def _reading(service: ml.Service) -> ft.Reading | None:
    pred = _safe(_fetch, service.code)
    if pred is None:
        return None
    urgent = len(_urgent_items()) if service.code == "farmacia" else None
    return ft.interpret(service.code, pred, _safe(_fetch_kpis, service.code), urgent)


def _change_badge(r: ft.Reading) -> str:
    if r.change_pct is None or not r.change_text:
        return ""
    direction = "flat" if r.level == "Habitual" else ("up" if r.change_pct > 0 else "down")
    tone = {"Alta": "t-danger", "Baja": "t-info"}.get(r.level, "t-ok")
    return (f'<div class="ml-change {tone}">{icon(CHANGE_ICON[direction], 15)}'
            f'{esc(r.change_text)} frente a lo habitual</div>')


def _card(service: ml.Service) -> None:
    r = _reading(service)
    if r is None:
        st.markdown(f'<div class="ml-card" style="--c:var(--border)"><div class="ml-head"><b>{esc(service.name)}</b>'
                    f'{chip("No disponible", "neutral", "●")}</div><div class="ml-off">El pronóstico de esta área '
                    'no está disponible en este momento. Las demás tarjetas y el resumen siguen funcionando; '
                    'prueba con «Actualizar» en unos segundos.</div></div>', unsafe_allow_html=True)
        return
    pred = _fetch(service.code)
    value = ft.num(pred.get("prediccion"))
    unit = ft.UNITS.get(service.code, "")
    review = "".join(f"<li>{esc(a)}</li>" for a in r.review)
    facts = "".join(f"<li>{esc(a)}</li>" for a in r.actions[:3])
    tags = "".join(f"<span>{esc(x)}</span>" for x in r.resources)
    conf_icon = icon("check" if r.reliable else "info", 15)
    st.markdown(
        f'<div class="ml-card" style="--c:{TONE_COLOR.get(r.level_tone, "var(--border)")}">'
        f'<div class="ml-head"><b>{esc(service.name)}</b>{chip(r.level, r.level_tone, "●")}</div>'
        f'<div class="ml-period">{icon("calendar", 14)}<span>Periodo: {esc(r.period)}</span></div>'
        f'<div class="ml-fig"><strong>{esc(value)}</strong><span>{esc(unit)} {"esperadas" if service.code in ft.ABOUT else "esperados"}</span></div>'
        f'{_change_badge(r)}'
        f'<div class="ml-sub">{esc(r.range_text)} {esc(r.level_text)}</div>'
        f'<div class="ml-sec">Por qué importa</div><div class="ml-why">{esc(r.why)}</div>'
        + (f'<div class="ml-sec">Recursos que podrían verse afectados</div><div class="ml-tags">{tags}</div>'
           if tags else "")
        + (f'<div class="ml-sec">Qué conviene revisar</div><ul class="ml-list">{review}</ul>' if review else "")
        + (f'<div class="ml-sec">Lo que muestran los datos recientes</div><ul class="ml-list facts">{facts}</ul>'
           if facts else "")
        + f'<div class="ml-conf {CONF_TONE.get(r.confidence_tone, "t-neutral")}">{conf_icon}'
          f'<div><b>{esc(r.confidence)}.</b> {esc(r.confidence_text)}</div></div></div>',
        unsafe_allow_html=True)
    with st.expander("Detalle técnico y reporte Excel del servicio"):
        m, v = pred.get("metricas", {}), pred.get("validacion", {})
        st.markdown(f"**Qué predice:** {pred.get('objetivo', '—')} · **Modelo:** Random Forest\n\n"
                    f"**Error medio (MAE):** {m.get('mae')} · **Referencia (mismo día de la semana anterior):** "
                    f"{m.get('mae_baseline')}\n\n**Entrenado** {' a '.join(v.get('entrenamiento', []))} · "
                    f"**probado** {' a '.join(v.get('prueba', []))}")
        for w in pred.get("advertencias") or []:
            st.caption(f"• {w}")
        if st.button("Preparar Excel del servicio", key=f"xlsx_{service.code}", width="stretch",
                     icon=":material/table_view:"):
            try:
                st.session_state[f"xlsx_{service.code}_data"] = ml.report(service.code)
            except ml.ServiceUnavailable as exc:
                st.error(str(exc))
        data = st.session_state.get(f"xlsx_{service.code}_data")
        if data:
            st.download_button("Descargar reporte .xlsx", data, f"reporte_{service.code}.xlsx", XLSX,
                               key=f"dl_{service.code}", type="primary", width="stretch",
                               icon=":material/download:")


def _summary_block(summary: ft.AdminSummary) -> None:
    """Las 5 preguntas del administrador en un solo bloque escaneable."""
    def ul(items: list[str], empty: str) -> str:
        return "<ul>" + "".join(f"<li>{esc(i)}</li>" for i in items) + "</ul>" if items else esc(empty)

    color = {"danger": "var(--c-danger)", "warn": "var(--c-warn)", "ok": "var(--c-ok)"}[summary.tone]
    q = [("calendar", "¿En qué periodo?", esc(summary.when)),
         ("activity", "¿Qué cambio se espera?", ul(summary.changes, "Sin datos.")),
         ("box", "¿Qué recursos podrían verse afectados?",
          ul(summary.resources, "Ninguno por encima de lo habitual.")),
         ("clipboard", "¿Qué conviene revisar?", ul(summary.review, "Nada adicional."))]
    cells = "".join(f'<div><div class="adm-q">{icon(ic, 14)}{esc(t)}</div><div class="adm-a">{body}</div></div>'
                    for ic, t, body in q)
    lead_icon = {"danger": "alert", "warn": "info", "ok": "check"}[summary.tone]
    st.markdown(f'<section class="adm" style="--c:{color}" aria-label="Resumen para el administrador">'
                f'<div class="adm-title">{icon(lead_icon, 20)}Resumen para el administrador</div>'
                f'<div class="adm-q" style="margin-top:0.5rem">{icon("eye", 14)}¿Qué está ocurriendo?</div>'
                f'<div class="adm-lead">{esc(summary.what)}</div><div class="adm-grid">{cells}</div>'
                '<div class="adm-note">Las sugerencias son puntos de revisión, no órdenes de compra ni de '
                'contratación: los modelos estiman cuántas atenciones habrá, no cuántos recursos se necesitan.'
                '</div></section>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Informe ejecutivo
# ---------------------------------------------------------------------------
def _pct(v) -> str:
    return f"{ft.num(v, 1)} %"


def report_data() -> dict:
    """Reúne las cifras reales para el informe."""
    conn, ref = ctx.get_conn(), ctx.ref_date()
    occ = db.kpi_global_occupancy(conn, ref)
    units = db.kpi_bed_occupancy(conn, ref, by="subgrupo_cama")
    units = units[units["servicio"] != "Urgencias"]
    full = units[units["porcentaje_ocupacion"] >= 95]
    full_names = [db.unit_label(n) for n in full["subgrupo_cama"]]
    uci = units[units["subgrupo_cama"].str.contains("INTENSIVOS ADULTOS", na=False)]
    waits = db.kpi_wait_times(conn, ref - timedelta(days=6), ref)
    by_shift = waits["por_turno_triage"]
    shifts = []
    if not by_shift.empty:
        g = by_shift.assign(tot=by_shift["atenciones"] * by_shift["espera_promedio_min"]).groupby("turno")
        agg = g[["atenciones", "tot"]].sum()
        shifts = [(t, int(r.atenciones), round(r.tot / r.atenciones, 1)) for t, r in agg.iterrows()]
    urgent = _urgent_items()
    soon = len(ps.stock_semaphore(ctx.get_clin(), ("AMARILLO",)))
    urg_kpis = _safe(_fetch_kpis, "urgencias") or {}

    free = occ["capacidad"] - occ["ocupadas"]
    situation = [
        ("Camas físicas ocupadas (sin urgencias)", f"{_pct(occ['porcentaje'])} · {occ['ocupadas']} de {occ['capacidad']}",
         f"Quedan {free} camas físicas libres para hospitalizar.",
         "danger" if occ["porcentaje"] >= 90 else "warn" if occ["porcentaje"] >= 85 else "ok"),
        ("Servicios al 95 % o más", str(len(full_names)),
         ", ".join(full_names) if full_names else "Ningún servicio está lleno.", "danger" if full_names else "ok"),
    ]
    if not uci.empty:
        u = uci.iloc[0]
        situation.append(("UCI adultos", f"{int(u.camas_ocupadas)} de {int(u.capacidad)} camas",
                          f"{int(u.capacidad - u.camas_ocupadas)} camas de UCI adultos libres.",
                          "danger" if u.porcentaje_ocupacion >= 90 else "ok"))
    situation.append(("Espera en urgencias (última semana)", f"{ft.num(waits['promedio_min'], 0)} min en promedio",
                      f"1 de cada 10 pacientes esperó más de {ft.num(waits['p90_min'], 0)} min "
                      f"({ft.num(waits['atenciones'])} atenciones).", "warn"))
    if urg_kpis.get("cumplimiento_meta_triage2_pct_7d") is not None:
        t2 = urg_kpis["cumplimiento_meta_triage2_pct_7d"]
        situation.append(("Triage II atendido a tiempo", _pct(t2),
                          f"Meta: atención en {urg_kpis.get('meta_triage2_min', 30)} min o menos.",
                          "danger" if t2 < 50 else "warn" if t2 < 80 else "ok"))
    order_total = sum(int(r["orden_sugerida_15d"]) for r in urgent)
    situation.append(("Farmacia: ítems que se agotan en < 5 días", str(len(urgent)),
                      f"{soon} más para pedir pronto. Orden sugerida: {ft.num(order_total)} unidades.",
                      "danger" if urgent else "ok"))

    forecasts, readings = [], {}
    for s in ml.SERVICES:
        r = _reading(s)
        readings[s.code] = r
        forecasts.append({"area": s.name, "unavailable": True} if r is None else
                         {"area": s.name, **r.__dict__, "notes": _fetch(s.code).get("advertencias") or []})

    first = (f"Al {ref.isoformat()} el hospital tiene el {_pct(occ['porcentaje'])} de sus camas físicas ocupadas "
             f"({occ['ocupadas']} de {occ['capacidad']})")
    parts = [first + (f" y {len(full_names)} servicios están al 95 % o más." if full_names else ".")]
    parts.append(f"En urgencias la espera promedio de la última semana fue de {ft.num(waits['promedio_min'], 0)} minutos.")
    if readings.get("urgencias"):
        r = readings["urgencias"]
        parts.append(f"{r.headline[:-1]} ({r.level.lower()}; pronóstico {r.confidence.lower()}).")
    parts.append(f"Farmacia tiene {len(urgent)} ítems que se agotan en menos de 5 días.")

    recs = []
    if full_names:
        recs.append(f"Revisar altas pendientes y traslados en {', '.join(full_names[:4])}: están al 95 % o más.")
    if urgent:
        recs.append(f"Emitir la orden de compra de los {len(urgent)} ítems urgentes ({ft.num(order_total)} unidades; "
                    "detalle en la hoja Compras urgentes).")
    for code in ("urgencias",):
        r = readings.get(code)
        if r and r.actions:
            recs.append(r.actions[0])
    if readings.get("urgencias") is None:
        recs.append("El pronóstico de urgencias no estuvo disponible al generar el informe.")

    admin = ft.admin_summary([(s.name, readings[s.code]) for s in ml.SERVICES])
    mtd = mr.month_to_date(conn, ref)
    return {"cutoff": ref.isoformat(), "generated_by": ctx.current_user()["nombre_mostrado"], "mtd": mtd,
            "admin": admin.__dict__,
            "situation": situation, "summary": " ".join(parts), "recommendations": recs,
            "forecasts": forecasts, "purchases": urgent, "shifts": shifts}


def page_predicciones() -> None:
    if not ctx.can("tablero.gerencial.ver"):
        st.error("Esta sección es solo para gerencia.")
        st.stop()
    st.markdown(CSS, unsafe_allow_html=True)
    head, refresh = st.columns([4, 1], vertical_alignment="top")
    page_header("Pronósticos de demanda por servicio",
                "Cuántas atenciones se esperan en cada servicio el próximo día, si eso es más o menos de lo "
                "habitual y qué conviene revisar. Se calcula con el historial real del hospital.", container=head)
    if refresh.button("Actualizar", icon=":material/refresh:", width="stretch",
                      help="Vuelve a consultar los pronósticos de todos los servicios"):
        _fetch.clear()
        _fetch_kpis.clear()
        ml.reset_breakers()
        st.session_state.pop("exec_report", None)
        st.rerun()
    _prefetch()

    readings = [(s.name, _reading(s)) for s in ml.SERVICES]
    _summary_block(ft.admin_summary(readings))

    with st.container(border=True):
        c1, c2, c3 = st.columns([2.6, 1.2, 1.2], vertical_alignment="center")
        c1.markdown('<div class="row-text"><b>Informe gerencial para la dirección</b> · resumen para el '
                    'administrador, situación actual, pronóstico por servicio, compras urgentes y plan de acción. '
                    'Solo cifras agregadas, sin datos de pacientes.</div>', unsafe_allow_html=True)
        if "exec_report" not in st.session_state:
            if c2.button("Preparar informe", icon=":material/description:", type="primary", width="stretch"):
                with st.spinner("Armando el informe…"):
                    data = report_data()
                    st.session_state.exec_report = (reports.executive_report_pdf(data),
                                                    reports.executive_report_xlsx(data))
                st.rerun()
        else:
            pdf, xlsx = st.session_state.exec_report
            day = ctx.ref_date().isoformat()
            c2.download_button("PDF", pdf, f"informe_gerencial_{day}.pdf", "application/pdf",
                               icon=":material/picture_as_pdf:", type="primary", width="stretch",
                               help="Informe para imprimir o enviar a la dirección")
            c3.download_button("Excel", xlsx, f"informe_gerencial_{day}.xlsx", XLSX,
                               icon=":material/table_view:", width="stretch",
                               help="Las mismas cifras en hojas de cálculo")

    section_title("Detalle por servicio")
    services = list(ml.SERVICES)
    for i in range(0, len(services), 2):  # filas de a dos: las tarjetas de una fila quedan alineadas
        cols = st.columns(2, gap="medium")
        for col, service in zip(cols, services[i:i + 2]):
            with col:
                _card(service)
    if all(ml.is_down(s.code) for s in ml.SERVICES):
        st.info("Ningún servicio de pronóstico responde. El equipo de sistemas puede iniciarlos con: "
                "`python microservicios/run_services.py`")
    st.markdown(
        '<div class="glossary">'
        '<div><b>Lo habitual</b>: el promedio del mismo servicio en las 4 semanas previas (o el mismo día de la '
        'semana). Hasta ±15 % se considera normal.</div>'
        '<div><b>Rango probable</b>: entre qué valores es más probable que quede la cifra real.</div>'
        '<div><b>Confiable</b>: en las últimas 4 semanas acertó mejor que repetir lo del mismo día de la semana '
        'anterior.</div>'
        '<div><b>Solo orientativo</b>: no logró ese nivel; se muestra por transparencia, no para decidir.</div>'
        '</div>', unsafe_allow_html=True)
