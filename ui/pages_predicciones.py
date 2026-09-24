"""
ui/pages_predicciones.py — Pronósticos de los microservicios (solo gerencia: permiso tablero.gerencial.ver).

Una tarjeta por servicio con el pronóstico, su rango probable, la validación honesta frente a la línea base y
el reporte Excel. Cada servicio se consulta por separado con tiempo máximo: si uno está apagado, su tarjeta lo
dice y las demás siguen funcionando (aislamiento de fallos).
"""
from __future__ import annotations

import streamlit as st

import ml_services as ml
from ui import context as ctx
from ui.theme import AMBER, BORDER, EMERALD, MUTED, TEXT, chip, esc

CSS = f"""
<style>
  .ml-card {{background:#FFF; border:1px solid {BORDER}; border-top:4px solid var(--c); border-radius:12px;
      padding:0.9rem 1rem 0.6rem; box-shadow:0 1px 3px rgba(0,0,0,.05); min-height:230px;}}
  .ml-head {{display:flex; justify-content:space-between; align-items:center; margin-bottom:0.3rem;}}
  .ml-head b {{font-size:1rem; color:{TEXT};}}
  .ml-what {{font-size:0.8rem; color:{MUTED}; min-height:2.2em;}}
  .ml-value {{font-size:1.9rem; font-weight:750; color:{TEXT}; line-height:1.1; margin-top:0.35rem;
      font-variant-numeric:tabular-nums;}}
  .ml-range {{font-size:0.8rem; color:{MUTED};}}
  .ml-eval {{font-size:0.8rem; margin-top:0.55rem; padding:0.4rem 0.55rem; border-radius:8px;}}
  .ml-off {{font-size:0.85rem; color:{MUTED}; margin-top:0.8rem;}}
</style>
"""


def _num(v) -> str:
    if v is None:
        return "—"
    return f"{v:,.0f}".replace(",", ".") if abs(v) >= 20 else f"{v:.1f}".replace(".", ",")


def _err(v) -> str:
    return "—" if v is None else f"{v:.1f}".replace(".", ",")


@st.cache_data(ttl=60, show_spinner=False)
def _fetch(code: str) -> dict:
    """Predicción general del servicio. Cacheada 60 s; los errores no se cachean (se reintentan)."""
    return ml.predict(code)


def _card(service: ml.Service) -> None:
    try:
        pred = _fetch(service.code)
    except ml.ServiceUnavailable as exc:
        st.markdown(f'<div class="ml-card" style="--c:{BORDER}"><div class="ml-head"><b>{service.icon} '
                    f'{esc(service.name)}</b>{chip("No disponible", "neutral", "●")}</div>'
                    f'<div class="ml-off">{esc(str(exc))}.<br>Las demás tarjetas siguen funcionando.</div></div>',
                    unsafe_allow_html=True)
        return
    m = pred.get("metricas", {})
    ok = bool(m.get("supera_baseline"))
    lo, hi = (pred.get("intervalo") or [None, None])[:2]
    evaluation = (f'<div class="ml-eval" style="background:#ECFDF5;color:#065F46">✓ Mejora a la referencia: error '
                  f'{_err(m.get("mae"))} vs {_err(m.get("mae_baseline"))}</div>' if ok else
                  f'<div class="ml-eval" style="background:#FEF3C7;color:#92400E">⚠ No supera a repetir la semana '
                  f'anterior (error {_err(m.get("mae"))} vs {_err(m.get("mae_baseline"))}): solo orientativo</div>')
    st.markdown(f'<div class="ml-card" style="--c:{EMERALD if ok else AMBER}"><div class="ml-head"><b>{service.icon} '
                f'{esc(service.name)}</b>{chip("En línea", "ok", "●")}</div>'
                f'<div class="ml-what">{esc(pred.get("objetivo", ""))} · {esc(pred.get("fecha_objetivo", ""))}</div>'
                f'<div class="ml-value">{_num(pred.get("prediccion"))}</div>'
                f'<div class="ml-range">rango probable {_num(lo)} – {_num(hi)}</div>{evaluation}</div>',
                unsafe_allow_html=True)
    warnings = pred.get("advertencias") or []
    with st.expander("Detalle y reporte"):
        v = pred.get("validacion", {})
        st.markdown(f"**Validación temporal:** entrenado {' a '.join(v.get('entrenamiento', []))}, probado "
                    f"{' a '.join(v.get('prueba', []))}. Referencia: {v.get('linea_base', '—')}.")
        for w in warnings:
            st.caption(f"• {w}")
        if st.button("Preparar Excel", key=f"xlsx_{service.code}", width="stretch"):
            try:
                st.session_state[f"xlsx_{service.code}_data"] = ml.report(service.code)
            except ml.ServiceUnavailable as exc:
                st.error(str(exc))
        data = st.session_state.get(f"xlsx_{service.code}_data")
        if data:
            st.download_button("Descargar reporte .xlsx", data, f"reporte_{service.code}.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               key=f"dl_{service.code}", type="primary", width="stretch")


def page_predicciones() -> None:
    if not ctx.can("tablero.gerencial.ver"):
        st.error("Esta sección es solo para gerencia.")
        st.stop()
    st.markdown(CSS, unsafe_allow_html=True)
    head, action = st.columns([4, 1], vertical_alignment="center")
    head.markdown("### Pronósticos por servicio")
    if action.button("↻ Actualizar", width="stretch"):
        _fetch.clear()
        ml.reset_breakers()
        st.rerun()
    st.caption("Random Forest por servicio, validado con las últimas 4 semanas que el modelo no vio. Cada tarjeta "
               "dice si el modelo mejora o no a la referencia simple (repetir la semana anterior).")
    cols = st.columns(2, gap="medium")
    for i, service in enumerate(ml.SERVICES):
        with cols[i % 2]:
            _card(service)
    offline = [s for s in ml.SERVICES if ml.is_down(s.code)]
    if len(offline) == len(ml.SERVICES):
        st.info("Ningún servicio responde. Levántalos en otra terminal con: `python microservicios/run_services.py`")
    st.caption("Pregúntale también a la IA, por ejemplo: “¿cuántos ingresos a urgencias se esperan mañana?”.")
