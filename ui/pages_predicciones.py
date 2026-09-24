"""
ui/pages_predicciones.py — Pronósticos de los microservicios (solo gerencia: permiso tablero.gerencial.ver).

Cada tarjeta habla en lenguaje de gestión (forecast_text.py): qué se espera, si es alto o habitual, qué tan
confiable es y qué hacer. El detalle técnico (MAE, validación, línea base) queda en un desplegable.
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
import reports
from ui import context as ctx
from ui.theme import AMBER, BORDER, EMERALD, MUTED, RED, TEXT, chip, esc

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TONE_COLOR = {"danger": RED, "warn": AMBER, "ok": EMERALD, "neutral": BORDER}
CSS = f"""
<style>
  .ml-card {{background:#FFF; border:1px solid {BORDER}; border-top:4px solid var(--c); border-radius:12px;
      padding:0.9rem 1rem 0.7rem; box-shadow:0 1px 3px rgba(0,0,0,.05); min-height:250px;}}
  .ml-head {{display:flex; justify-content:space-between; align-items:center; gap:0.4rem; margin-bottom:0.2rem;}}
  .ml-head b {{font-size:1rem; color:{TEXT};}}
  .ml-main {{font-size:1.02rem; color:{TEXT}; line-height:1.35; margin-top:0.3rem;}}
  .ml-main strong {{font-size:1.6rem; font-variant-numeric:tabular-nums;}}
  .ml-sub {{font-size:0.82rem; color:{MUTED}; margin-top:0.15rem;}}
  .ml-conf {{font-size:0.8rem; margin-top:0.55rem; padding:0.4rem 0.55rem; border-radius:8px;}}
  .ml-do {{font-size:0.82rem; color:{TEXT}; margin:0.55rem 0 0; padding-left:1.1rem;}}
  .ml-do li {{margin-bottom:0.15rem;}}
  .ml-off {{font-size:0.85rem; color:{MUTED}; margin-top:0.8rem;}}
</style>
"""
CONF_STYLE = {"ok": "background:#ECFDF5;color:#065F46", "warn": "background:#FEF3C7;color:#92400E"}


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


def _card(service: ml.Service) -> None:
    r = _reading(service)
    if r is None:
        st.markdown(f'<div class="ml-card" style="--c:{BORDER}"><div class="ml-head"><b>{esc(service.name)}</b>'
                    f'{chip("No disponible", "neutral", "●")}</div><div class="ml-off">Estimación no disponible '
                    'para esta área en este momento. Las demás tarjetas siguen funcionando.</div></div>',
                    unsafe_allow_html=True)
        return
    pred = _fetch(service.code)
    value = ft.num(pred.get("prediccion"))
    main = esc(r.headline).replace(esc(value), f"<strong>{esc(value)}</strong>", 1)
    actions = "".join(f"<li>{esc(a)}</li>" for a in r.actions[:3])
    st.markdown(f'<div class="ml-card" style="--c:{TONE_COLOR.get(r.level_tone, BORDER)}">'
                f'<div class="ml-head"><b>{esc(service.name)}</b>{chip(r.level, r.level_tone, "●")}</div>'
                f'<div class="ml-main">{main}</div>'
                f'<div class="ml-sub">{esc(r.range_text)} {esc(r.level_text)}</div>'
                f'<div class="ml-conf" style="{CONF_STYLE.get(r.confidence_tone, "")}"><b>{esc(r.confidence)}.</b> '
                f'{esc(r.confidence_text)}</div>'
                f'{f"<ul class=ml-do>{actions}</ul>" if actions else ""}</div>', unsafe_allow_html=True)
    with st.expander("Detalle técnico y reporte del servicio"):
        m, v = pred.get("metricas", {}), pred.get("validacion", {})
        st.markdown(f"**Qué predice:** {pred.get('objetivo', '—')} · **Modelo:** Random Forest\n\n"
                    f"**Error medio (MAE):** {m.get('mae')} · **Referencia (mismo día de la semana anterior):** "
                    f"{m.get('mae_baseline')}\n\n**Entrenado** {' a '.join(v.get('entrenamiento', []))} · "
                    f"**probado** {' a '.join(v.get('prueba', []))}")
        for w in pred.get("advertencias") or []:
            st.caption(f"• {w}")
        if st.button("Preparar Excel del servicio", key=f"xlsx_{service.code}", width="stretch"):
            try:
                st.session_state[f"xlsx_{service.code}_data"] = ml.report(service.code)
            except ml.ServiceUnavailable as exc:
                st.error(str(exc))
        data = st.session_state.get(f"xlsx_{service.code}_data")
        if data:
            st.download_button("Descargar reporte .xlsx", data, f"reporte_{service.code}.xlsx", XLSX,
                               key=f"dl_{service.code}", type="primary", width="stretch")


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

    return {"cutoff": ref.isoformat(), "generated_by": ctx.current_user()["nombre_mostrado"],
            "situation": situation, "summary": " ".join(parts), "recommendations": recs,
            "forecasts": forecasts, "purchases": urgent, "shifts": shifts}


def page_predicciones() -> None:
    if not ctx.can("tablero.gerencial.ver"):
        st.error("Esta sección es solo para gerencia.")
        st.stop()
    st.markdown(CSS, unsafe_allow_html=True)
    head, refresh = st.columns([4, 1], vertical_alignment="center")
    head.markdown("### Pronósticos del siguiente día")
    if refresh.button("Actualizar", icon=":material/refresh:", width="stretch"):
        _fetch.clear()
        _fetch_kpis.clear()
        ml.reset_breakers()
        st.session_state.pop("exec_report", None)
        st.rerun()
    _prefetch()

    with st.container(border=True):
        c1, c2, c3 = st.columns([2.6, 1.2, 1.2], vertical_alignment="center")
        c1.markdown("**Informe gerencial** · situación actual, pronóstico por servicio, compras urgentes y plan de "
                    "acción. Solo cifras agregadas, sin datos de pacientes.")
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
                               icon=":material/picture_as_pdf:", type="primary", width="stretch")
            c3.download_button("Excel", xlsx, f"informe_gerencial_{day}.xlsx", XLSX,
                               icon=":material/table_view:", width="stretch")

    cols = st.columns(2, gap="medium")
    for i, service in enumerate(ml.SERVICES):
        with cols[i % 2]:
            _card(service)
    if all(ml.is_down(s.code) for s in ml.SERVICES):
        st.info("Ningún servicio responde. Levántalos en otra terminal con: `python microservicios/run_services.py`")
    st.caption("“Confiable” = en las últimas 4 semanas acertó mejor que repetir lo del mismo día de la semana "
               "anterior. “Solo orientativo” = no lo logró; se muestra igual para ser transparentes.")
