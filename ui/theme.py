"""
ui/theme.py — Identidad visual y componentes reutilizables (tarjetas, chips, gráficos).

Paleta: esmeralda = normalidad · azul institucional = actividad · ámbar/rojo = saturación y alertas.
Todo el CSS se inyecta una vez por ejecución; no hay fuentes remotas ni librerías de animación.
"""
from __future__ import annotations

import html

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import config

EMERALD, BLUE, BLUE_LIGHT = "#059669", "#1F4E79", "#6B93BD"
AMBER, RED, GREY = "#D97706", "#DC2626", "#9CA3AF"
TEXT, MUTED, BORDER, PAGE_BG = "#111827", "#6B7280", "#E5E7EB", "#F8F9FA"
FONT_STACK = 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'
ACTIVITY_SEQ = [BLUE, "#3A6EA5", BLUE_LIGHT, "#0F766E", EMERALD, GREY]
PLOTLY_CONFIG = {"displayModeBar": False, "displaylogo": False, "responsive": True}
SEVERITY = {  # etiqueta, acento, fondo del chip, texto del chip
    "crítica": ("Crítica", RED, "#FEE2E2", "#991B1B"),
    "alta": ("Alta", AMBER, "#FFEDD5", "#9A3412"),
    "media": ("Media", "#CA8A04", "#FEF9C3", "#854D0E"),
    "info": ("Info", BLUE, "#DBEAFE", "#1E3A8A"),
}
CHIP_TONES = {"ok": ("#D1FAE5", "#065F46"), "warn": ("#FEF3C7", "#92400E"), "danger": ("#FEE2E2", "#991B1B"),
              "info": ("#DBEAFE", "#1E3A8A"), "neutral": ("#F3F4F6", "#374151")}
EVENT_STYLE = {  # color del punto en la línea de tiempo de la historia clínica
    "NOTA_EVOLUCION": BLUE, "PRESCRIPCION": "#7C3AED", "DISPENSACION": EMERALD,
    "ADMINISTRACION_DOSIS": EMERALD, "FORMULA_CADUCADA": RED, "ALERTA": AMBER, "CITA": BLUE_LIGHT,
    "INTERCONSULTA": BLUE_LIGHT,
}
RX_STATE_TONE = {"VIGENTE": "info", "PARCIAL": "warn", "ENTREGADA": "ok", "CADUCADA": "danger", "ANULADA": "neutral"}

CSS = f"""
<style>
  /* Interfaz nativa oculta (se conserva la navegación superior y el botón de la barra lateral) */
  [data-testid="stToolbarActions"], [data-testid="stMainMenu"], [data-testid="stAppDeployButton"],
  [data-testid="stStatusWidget"], [data-testid="stDecoration"], #MainMenu, footer {{display: none !important;}}
  header[data-testid="stHeader"] {{background: #FFFFFF; border-bottom: 1px solid {BORDER};}}

  html, body, .stApp, [data-testid="stSidebar"], button, input, textarea {{font-family: {FONT_STACK};}}
  .stApp, [data-testid="stAppViewContainer"] {{background: {PAGE_BG};}}
  [data-testid="stMainBlockContainer"] {{padding-top: 4.2rem; padding-bottom: 2rem; max-width: 1600px;}}
  [data-testid="stSidebar"] {{background: #FFFFFF; border-right: 1px solid {BORDER};}}

  /* Cabecera contextual */
  .brand h1 {{font-size: 1.45rem; font-weight: 700; margin: 0; padding: 0; color: {BLUE}; line-height: 1.2;}}
  .brand p {{margin: 0.15rem 0 0; color: {MUTED}; font-size: 0.85rem;}}
  .ctx-chips {{display: flex; flex-wrap: wrap; gap: 0.4rem; align-items: center; margin-top: 0.35rem;}}
  .chip {{display: inline-flex; align-items: center; gap: 0.3rem; font-size: 0.76rem; font-weight: 600;
      border-radius: 999px; padding: 0.22rem 0.65rem; white-space: nowrap;}}
  .section-title {{font-size: 1.05rem; font-weight: 650; color: {TEXT}; margin: 0.4rem 0 0.6rem;}}
  .muted {{color: {MUTED}; font-size: 0.85rem;}}

  /* Tarjetas KPI */
  .kpi-grid {{display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 0.85rem; margin: 0.3rem 0 0.9rem;}}
  .kpi-card {{background: #FFFFFF; border: 1px solid {BORDER}; border-radius: 10px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.05); padding: 0.85rem 1rem; display: flex; flex-direction: column;
      gap: 0.25rem; min-height: 128px; border-top: 3px solid var(--tone);}}
  .kpi-label {{font-size: 0.8rem; color: {MUTED}; font-weight: 550;}}
  .kpi-value {{font-size: clamp(1.4rem, 2.2vw, 1.85rem); font-weight: 700; color: {TEXT}; line-height: 1.15;
      font-variant-numeric: tabular-nums;}}
  .kpi-sub {{font-size: 0.78rem; color: {MUTED}; margin-top: auto;}}
  .kpi-delta {{display: inline-block; font-size: 0.74rem; font-weight: 600; border-radius: 999px;
      padding: 0.1rem 0.5rem; width: fit-content;}}
  .kpi-delta.good {{background: #D1FAE5; color: #065F46;}}
  .kpi-delta.bad {{background: #FEE2E2; color: #991B1B;}}

  /* Tarjetas genéricas en grilla adaptable (semáforo, fórmulas, cola, citas) */
  .card-grid {{display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 0.75rem;
      margin: 0.3rem 0 0.8rem;}}
  .card {{background: #FFFFFF; border: 1px solid {BORDER}; border-left: 4px solid var(--accent, {BORDER});
      border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); padding: 0.75rem 0.9rem;}}
  .card-top {{display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; margin-bottom: 0.35rem;}}
  .card-title {{font-size: 0.93rem; font-weight: 650; color: {TEXT}; line-height: 1.3; margin-bottom: 0.2rem;}}
  .card-body {{font-size: 0.82rem; color: #4B5563; line-height: 1.45;}}
  .card-big {{font-size: 1.35rem; font-weight: 700; color: {TEXT}; font-variant-numeric: tabular-nums;}}
  .bar {{height: 6px; background: #F3F4F6; border-radius: 999px; overflow: hidden; margin: 0.4rem 0 0.2rem;}}
  .bar > span {{display: block; height: 100%; background: var(--accent, {EMERALD});}}

  /* Alertas prescriptivas */
  .pill {{display: inline-block; font-size: 0.72rem; font-weight: 700; border-radius: 999px; padding: 0.15rem 0.6rem;}}
  .alert-card {{background: #FFFFFF; border: 1px solid {BORDER}; border-left: 4px solid var(--accent);
      border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); padding: 0.8rem 1rem; margin-bottom: 0.7rem;}}
  .alert-top {{display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.35rem; flex-wrap: wrap;}}
  .alert-cat {{font-size: 0.75rem; color: {MUTED}; font-weight: 600;}}
  .alert-title {{font-size: 0.97rem; font-weight: 650; color: {TEXT}; margin: 0 0 0.3rem;}}
  .alert-detail {{font-size: 0.83rem; color: #4B5563; margin: 0 0 0.45rem; line-height: 1.45;}}
  .alert-action {{font-size: 0.86rem; color: {TEXT}; background: #F3F4F6; border-radius: 8px;
      padding: 0.45rem 0.65rem; line-height: 1.45;}}
  .alert-action b {{color: {BLUE};}}

  /* Línea de tiempo de la historia clínica */
  .timeline {{border-left: 2px solid {BORDER}; margin: 0.4rem 0 0.4rem 0.45rem; padding-left: 1.1rem;}}
  .tl-item {{position: relative; margin-bottom: 0.85rem;}}
  .tl-item::before {{content: ""; position: absolute; left: -1.52rem; top: 0.3rem; width: 0.7rem; height: 0.7rem;
      border-radius: 50%; background: var(--dot); border: 2px solid #FFFFFF; box-shadow: 0 0 0 1px {BORDER};}}
  .tl-meta {{font-size: 0.74rem; color: {MUTED};}}
  .tl-text {{font-size: 0.87rem; color: {TEXT}; line-height: 1.45;}}

  /* Banners */
  .banner {{border-radius: 10px; padding: 0.6rem 0.85rem; font-size: 0.84rem; margin: 0.2rem 0 0.8rem;
      border: 1px solid var(--bd); background: var(--bg); color: var(--fg);}}

  [data-testid="stPlotlyChart"] {{background: #FFFFFF; border: 1px solid {BORDER}; border-radius: 10px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.05); padding: 0.35rem 0.25rem;}}
  [data-testid="stDataFrame"] {{border: 1px solid {BORDER}; border-radius: 10px; overflow: hidden;}}
  .chart-note {{font-size: 0.78rem; color: {MUTED}; margin: -0.3rem 0 0.8rem 0.2rem;}}
  .stButton > button, .stDownloadButton > button {{border-radius: 8px; font-weight: 550;}}
  [data-testid="stChatMessage"] {{background: #FFFFFF; border: 1px solid {BORDER}; border-radius: 10px;}}
  [data-testid="stExpander"] details {{background: #FFFFFF; border-radius: 10px;}}

  /* Móvil y tabletas */
  @media (max-width: 1150px) {{.kpi-grid {{grid-template-columns: repeat(3, minmax(0, 1fr));}}}}
  @media (max-width: 700px) {{
    .kpi-grid {{grid-template-columns: repeat(2, minmax(0, 1fr));}}
    [data-testid="stMainBlockContainer"] {{padding-left: 0.8rem; padding-right: 0.8rem;}}
    .brand h1 {{font-size: 1.15rem;}}
    .kpi-card {{min-height: 110px; padding: 0.7rem;}}
  }}
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Componentes HTML
# ---------------------------------------------------------------------------
def esc(value) -> str:
    return html.escape(str(value))


def chip(text: str, tone: str = "neutral", icon: str = "") -> str:
    bg, fg = CHIP_TONES.get(tone, CHIP_TONES["neutral"])
    return f'<span class="chip" style="background:{bg};color:{fg}">{icon} {esc(text)}</span>'


def banner(text: str, tone: str = "info") -> None:
    bg, fg = CHIP_TONES.get(tone, CHIP_TONES["info"])
    st.markdown(f'<div class="banner" style="--bg:{bg};--fg:{fg};--bd:{fg}33">{text}</div>',
                unsafe_allow_html=True)


def section_title(text: str, container=st) -> None:
    container.markdown(f'<div class="section-title">{esc(text)}</div>', unsafe_allow_html=True)


def severity_pill(severity: str) -> str:
    label, _, bg, fg = SEVERITY.get(severity, ("•", GREY, "#F3F4F6", TEXT))
    return f'<span class="pill" style="background:{bg};color:{fg}">{label}</span>'


def kpi_card(label: str, value: str, sub: str = "", tone: str = BLUE, hint: str = "",
             delta: str | None = None, delta_good: bool = True) -> str:
    delta_html = (f'<span class="kpi-delta {"good" if delta_good else "bad"}">{esc(delta)}</span>'
                  if delta else "")
    return (f'<div class="kpi-card" style="--tone:{tone}" title="{esc(hint)}">'
            f'<div class="kpi-label">{esc(label)}</div><div class="kpi-value">{esc(value)}</div>'
            f'{delta_html}<div class="kpi-sub">{esc(sub)}</div></div>')


def card(title: str, body: str = "", accent: str = BORDER, badge: str = "", big: str = "",
         progress: float | None = None) -> str:
    """Tarjeta genérica. `body` y `badge` son HTML ya escapado por quien llama."""
    bar = ""
    if progress is not None:
        bar = f'<div class="bar"><span style="width:{max(0, min(progress, 1)) * 100:.0f}%"></span></div>'
    big_html = f'<div class="card-big">{esc(big)}</div>' if big else ""
    return (f'<div class="card" style="--accent:{accent}"><div class="card-top">'
            f'<div class="card-title">{esc(title)}</div>{badge}</div>{big_html}{bar}'
            f'<div class="card-body">{body}</div></div>')


def grid(cards: list[str]) -> None:
    st.markdown(f'<div class="card-grid">{"".join(cards)}</div>', unsafe_allow_html=True)


def occupancy_color(pct: float) -> str:
    if pct >= config.OCCUPANCY_CRITICAL_PCT:
        return RED
    if pct >= config.OCCUPANCY_WARNING_PCT:
        return AMBER
    return EMERALD


# ---------------------------------------------------------------------------
# Gráficos
# ---------------------------------------------------------------------------
def style_fig(fig: go.Figure, height: int = 340, title: str | None = None) -> go.Figure:
    fig.update_layout(
        template="simple_white", height=height, paper_bgcolor="#FFFFFF", plot_bgcolor="#FFFFFF",
        margin=dict(l=10, r=10, t=30 if not title else 42, b=10),
        font=dict(family=FONT_STACK, size=12, color="#374151"),
        title=dict(text=title, x=0.01, xanchor="left", font=dict(size=14, color=TEXT)) if title else None,
        legend=dict(orientation="h", y=-0.18, x=0, title=None, font=dict(size=11)),
        hoverlabel=dict(bgcolor="white", font_family=FONT_STACK), bargap=0.25)
    fig.update_xaxes(showgrid=False, linecolor=BORDER, tickfont=dict(color=MUTED), title_font=dict(color=MUTED))
    fig.update_yaxes(automargin=True, gridcolor="#F1F5F9", linecolor=BORDER, tickfont=dict(color="#374151"),
                     title_font=dict(color=MUTED))
    return fig


def show(fig: go.Figure, key: str | None = None) -> None:
    st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG, key=key)


def short_labels(texts: pd.Series, width: int = 58) -> pd.Series:
    """Recorta etiquetas sin fusionar ítems distintos que compartan prefijo."""
    base = texts.astype(str).map(lambda t: t if len(t) <= width else t[: width - 1].rstrip() + "…")
    counts = base.groupby(base).cumcount()
    return base.where(counts == 0, base + " (" + (counts + 1).astype(str) + ")")
