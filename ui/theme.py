"""
ui/theme.py — Identidad visual, tokens de diseño y componentes reutilizables (tarjetas, chips, iconos, gráficos).

Paleta: esmeralda = normalidad · azul institucional = actividad · ámbar/rojo = saturación y alertas.

Tokens: todos los colores de superficie, texto y borde son variables CSS (--surface, --text, --muted, --border…).
El modo claro las define en :root y el modo oscuro las redefine (inject_dark): no se invierten colores, cada
componente propio y nativo de Streamlit tiene su valor oscuro pensado para contraste (WCAG AA en textos).
Las constantes en hexadecimal (TEXT, MUTED, BORDER…) se conservan para Plotly y Python, que no leen CSS.

Todo el CSS se inyecta una vez por ejecución; no hay fuentes remotas ni librerías de animación.
"""
from __future__ import annotations

import html

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import config

# Identidad del hospital (tomada del logo): azul institucional y los dos verdes de la flor
NAVY, BRAND_GREEN, BRAND_LIME = "#2E3378", "#507643", "#8AB94F"
EMERALD, BLUE, BLUE_LIGHT = "#059669", NAVY, "#6F74B8"
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
SEVERITY_TONE = {"crítica": "danger", "alta": "orange", "media": "warn", "info": "info"}
CHIP_TONES = {"ok": ("#D1FAE5", "#065F46"), "warn": ("#FEF3C7", "#92400E"), "danger": ("#FEE2E2", "#991B1B"),
              "info": ("#DBEAFE", "#1E3A8A"), "neutral": ("#F3F4F6", "#374151")}
EVENT_STYLE = {  # color del punto en la línea de tiempo de la historia clínica
    "NOTA_EVOLUCION": BLUE, "PRESCRIPCION": "#7C3AED", "DISPENSACION": EMERALD,
    "ADMINISTRACION_DOSIS": EMERALD, "FORMULA_CADUCADA": RED, "ALERTA": AMBER, "CITA": BLUE_LIGHT,
    "INTERCONSULTA": BLUE_LIGHT, "REGISTRO_HC": "#0F766E", "ADJUNTO": GREY, "DATOS_PACIENTE": GREY,
}
RX_LABEL = {"PENDIENTE_STOCK": "En espera de existencias"}
RX_STATE_TONE = {"PENDIENTE_STOCK": "warn", "VIGENTE": "info", "PARCIAL": "warn", "ENTREGADA": "ok", "CADUCADA": "danger", "ANULADA": "neutral"}

# ---------------------------------------------------------------------------
# Tokens de diseño (variables CSS). Mismo nombre en los dos modos; solo cambia el valor.
# ---------------------------------------------------------------------------
LIGHT_TOKENS = {
    "bg": PAGE_BG, "surface": "#FFFFFF", "surface-2": "#F3F4F6", "surface-3": "#F8FAFC", "brand-tint": "#EEF5E6",
    "text": TEXT, "text-2": "#374151", "muted": MUTED, "placeholder": "#6B7280",
    "border": BORDER, "border-strong": "#D1D5DB", "input-bg": "#F3F6F4",
    "brand": NAVY, "brand-green": BRAND_GREEN, "brand-green-hover": "#3F5F35", "lime": BRAND_LIME,
    "link": NAVY, "focus": "#2563EB", "shadow": "0 1px 3px rgba(0,0,0,0.05)",
    "shadow-lg": "0 18px 50px rgba(15,23,42,.22)",
    "c-ok": EMERALD, "c-warn": AMBER, "c-danger": RED, "c-info": NAVY, "c-orange": "#EA580C",
    "ok-bg": "#D1FAE5", "ok-fg": "#065F46", "ok-bd": "#A7F3D0",
    "warn-bg": "#FEF3C7", "warn-fg": "#92400E", "warn-bd": "#FCD34D",
    "danger-bg": "#FEE2E2", "danger-fg": "#991B1B", "danger-bd": "#FCA5A5",
    "info-bg": "#DBEAFE", "info-fg": "#1E3A8A", "info-bd": "#BFDBFE",
    "orange-bg": "#FFEDD5", "orange-fg": "#9A3412", "orange-bd": "#FDBA74",
    "neutral-bg": "#F3F4F6", "neutral-fg": "#374151", "neutral-bd": "#E5E7EB",
    "chat-me": NAVY, "chat-me-fg": "#FFFFFF",
}
DARK_TOKENS = {
    "bg": "#0F1318", "surface": "#171C23", "surface-2": "#1F252E", "surface-3": "#1A2029", "brand-tint": "#1E2A1B",
    "text": "#E7EAEE", "text-2": "#C9CFD7", "muted": "#9AA4B1", "placeholder": "#8A94A1",
    "border": "#2B323D", "border-strong": "#3B4450", "input-bg": "#1F252E",
    "brand": "#B4B8F2", "brand-green": "#5B8A4C", "brand-green-hover": "#6E9E5E", "lime": "#8AB94F",
    "link": "#B4B8F2", "focus": "#7AA7FF", "shadow": "0 1px 2px rgba(0,0,0,0.5)",
    "shadow-lg": "0 18px 50px rgba(0,0,0,.55)",
    "c-ok": "#34D399", "c-warn": "#FBBF24", "c-danger": "#F87171", "c-info": "#A5B4FC", "c-orange": "#FB923C",
    "ok-bg": "rgba(16,185,129,.16)", "ok-fg": "#6EE7B7", "ok-bd": "rgba(52,211,153,.35)",
    "warn-bg": "rgba(245,158,11,.16)", "warn-fg": "#FCD34D", "warn-bd": "rgba(251,191,36,.35)",
    "danger-bg": "rgba(239,68,68,.17)", "danger-fg": "#FCA5A5", "danger-bd": "rgba(248,113,113,.40)",
    "info-bg": "rgba(99,102,241,.20)", "info-fg": "#C7D2FE", "info-bd": "rgba(165,180,252,.35)",
    "orange-bg": "rgba(249,115,22,.17)", "orange-fg": "#FDBA74", "orange-bd": "rgba(251,146,60,.38)",
    "neutral-bg": "#262D37", "neutral-fg": "#D1D5DB", "neutral-bd": "#3B4450",
    "chat-me": "#3B4191", "chat-me-fg": "#FFFFFF",
}


def _vars(tokens: dict) -> str:
    return "".join(f"--{k}:{v};" for k, v in tokens.items())


CSS = f"""
<style>
  :root {{{_vars(LIGHT_TOKENS)}}}

  /* Interfaz nativa oculta (se conserva la navegación superior y el botón de la barra lateral) */
  [data-testid="stToolbarActions"], [data-testid="stMainMenu"], [data-testid="stAppDeployButton"],
  [data-testid="stStatusWidget"], [data-testid="stDecoration"], #MainMenu, footer {{display: none !important;}}
  header[data-testid="stHeader"] {{background: var(--surface); border-bottom: 3px solid var(--lime);}}
  [data-testid="stSidebarNav"] a[aria-current="page"] {{background: var(--brand-tint);}}
  [data-testid="stSidebarNav"] a[aria-current="page"] span {{color: var(--brand-green); font-weight: 700;}}
  .brand {{display:flex; align-items:center; gap:0.7rem;}}

  html, body, .stApp, [data-testid="stSidebar"], button, input, textarea {{font-family: {FONT_STACK};}}
  .stApp, [data-testid="stAppViewContainer"] {{background: var(--bg);}}
  [data-testid="stMainBlockContainer"] {{padding-top: 4.2rem; padding-bottom: 2rem; max-width: 1600px;}}
  [data-testid="stSidebar"] {{background: var(--surface); border-right: 1px solid var(--border);}}

  /* Foco visible para teclado (accesibilidad) */
  .stApp a:focus-visible, .stApp button:focus-visible, .stApp [role="tab"]:focus-visible,
  .stApp summary:focus-visible {{outline: 2px solid var(--focus) !important; outline-offset: 2px;}}
  @media (prefers-reduced-motion: reduce) {{
    .stApp *, .stApp *::before, .stApp *::after {{transition: none !important; animation: none !important;}}
  }}

  /* Cabecera contextual */
  .brand h1 {{font-size: 1.45rem; font-weight: 700; margin: 0; padding: 0; color: var(--brand); line-height: 1.2;}}
  .brand p {{margin: 0.15rem 0 0; color: var(--muted); font-size: 0.85rem;}}
  .page-head {{margin: 0.2rem 0 0.9rem;}}
  .page-head h2 {{font-size: 1.55rem; font-weight: 700; color: var(--text); margin: 0; padding: 0; line-height: 1.25;}}
  .page-head p {{color: var(--muted); font-size: 0.9rem; margin: 0.25rem 0 0; max-width: 72ch; line-height: 1.45;}}
  .ctx-chips {{display: flex; flex-wrap: wrap; gap: 0.4rem; align-items: center; margin-top: 0.35rem;}}
  .chip {{display: inline-flex; align-items: center; gap: 0.35rem; font-size: 0.76rem; font-weight: 600;
      border-radius: 999px; padding: 0.22rem 0.65rem; white-space: nowrap; line-height: 1.3;
      background: var(--neutral-bg); color: var(--neutral-fg);}}
  .chip .dot, .dot {{width: 0.5rem; height: 0.5rem; border-radius: 50%; background: currentColor; flex: none;
      display: inline-block;}}
  .chip .tri {{width: 0; height: 0; border-left: 0.3rem solid transparent; border-right: 0.3rem solid transparent;
      border-bottom: 0.5rem solid currentColor; flex: none; display: inline-block;}}
  .t-ok {{background: var(--ok-bg); color: var(--ok-fg);}} .t-warn {{background: var(--warn-bg); color: var(--warn-fg);}}
  .t-danger {{background: var(--danger-bg); color: var(--danger-fg);}}
  .t-info {{background: var(--info-bg); color: var(--info-fg);}}
  .t-orange {{background: var(--orange-bg); color: var(--orange-fg);}}
  .t-neutral {{background: var(--neutral-bg); color: var(--neutral-fg);}}
  .ico {{display: inline-block; vertical-align: -0.18em; flex: none;}}
  .section-title {{font-size: 1.05rem; font-weight: 650; color: var(--text); margin: 0.4rem 0 0.6rem;}}
  .muted {{color: var(--muted); font-size: 0.85rem;}}

  /* Tarjetas KPI */
  .kpi-grid {{display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 0.85rem; margin: 0.3rem 0 0.9rem;}}
  .kpi-card {{background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
      box-shadow: var(--shadow); padding: 0.85rem 1rem; display: flex; flex-direction: column;
      gap: 0.25rem; min-height: 128px; border-top: 3px solid var(--tone);}}
  .kpi-label {{font-size: 0.8rem; color: var(--muted); font-weight: 550;}}
  .kpi-value {{font-size: clamp(1.4rem, 2.2vw, 1.85rem); font-weight: 700; color: var(--text); line-height: 1.15;
      font-variant-numeric: tabular-nums;}}
  .kpi-sub {{font-size: 0.78rem; color: var(--muted); margin-top: auto;}}
  .kpi-delta {{display: inline-block; font-size: 0.74rem; font-weight: 600; border-radius: 999px;
      padding: 0.1rem 0.5rem; width: fit-content;}}
  .kpi-delta.good {{background: var(--ok-bg); color: var(--ok-fg);}}
  .kpi-delta.bad {{background: var(--danger-bg); color: var(--danger-fg);}}

  /* Tarjetas genéricas en grilla adaptable (semáforo, fórmulas, cola, citas) */
  .card-grid {{display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 0.75rem;
      margin: 0.3rem 0 0.8rem;}}
  .card {{background: var(--surface); border: 1px solid var(--border); border-left: 4px solid var(--accent, var(--border));
      border-radius: 10px; box-shadow: var(--shadow); padding: 0.75rem 0.9rem;}}
  .card-top {{display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; margin-bottom: 0.35rem;}}
  .card-title {{font-size: 0.93rem; font-weight: 650; color: var(--text); line-height: 1.3; margin-bottom: 0.2rem;}}
  .card-body {{font-size: 0.82rem; color: var(--text-2); line-height: 1.45;}}
  .card-big {{font-size: 1.35rem; font-weight: 700; color: var(--text); font-variant-numeric: tabular-nums;}}
  .bar {{height: 6px; background: var(--surface-2); border-radius: 999px; overflow: hidden; margin: 0.4rem 0 0.2rem;}}
  .bar > span {{display: block; height: 100%; background: var(--accent, {EMERALD});}}

  /* Alertas prescriptivas */
  .pill {{display: inline-block; font-size: 0.72rem; font-weight: 700; border-radius: 999px; padding: 0.15rem 0.6rem;}}
  .alert-card {{background: var(--surface); border: 1px solid var(--border); border-left: 4px solid var(--accent);
      border-radius: 10px; box-shadow: var(--shadow); padding: 0.8rem 1rem; margin-bottom: 0.7rem;}}
  .alert-top {{display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.35rem; flex-wrap: wrap;}}
  .alert-cat {{font-size: 0.75rem; color: var(--muted); font-weight: 600;}}
  .alert-title {{font-size: 0.97rem; font-weight: 650; color: var(--text); margin: 0 0 0.3rem;}}
  .alert-detail {{font-size: 0.83rem; color: var(--text-2); margin: 0 0 0.45rem; line-height: 1.45;}}
  .alert-action {{font-size: 0.86rem; color: var(--text); background: var(--surface-2); border-radius: 8px;
      padding: 0.45rem 0.65rem; line-height: 1.45;}}
  .alert-action b {{color: var(--brand);}}

  /* Línea de tiempo de la historia clínica */
  .timeline {{border-left: 2px solid var(--border); margin: 0.4rem 0 0.4rem 0.45rem; padding-left: 1.1rem;}}
  .tl-item {{position: relative; margin-bottom: 0.85rem;}}
  .tl-item::before {{content: ""; position: absolute; left: -1.52rem; top: 0.3rem; width: 0.7rem; height: 0.7rem;
      border-radius: 50%; background: var(--dot); border: 2px solid var(--surface); box-shadow: 0 0 0 1px var(--border);}}
  .tl-meta {{font-size: 0.74rem; color: var(--muted);}}
  .tl-text {{font-size: 0.87rem; color: var(--text); line-height: 1.45;}}

  /* Banners */
  .banner {{border-radius: 10px; padding: 0.6rem 0.85rem; font-size: 0.84rem; margin: 0.2rem 0 0.8rem;
      border: 1px solid var(--bd); background: var(--bg-t); color: var(--fg);}}
  .banner.t-ok {{--bg-t: var(--ok-bg); --fg: var(--ok-fg); --bd: var(--ok-bd);}}
  .banner.t-warn {{--bg-t: var(--warn-bg); --fg: var(--warn-fg); --bd: var(--warn-bd);}}
  .banner.t-danger {{--bg-t: var(--danger-bg); --fg: var(--danger-fg); --bd: var(--danger-bd);}}
  .banner.t-info {{--bg-t: var(--info-bg); --fg: var(--info-fg); --bd: var(--info-bd);}}
  .banner.t-neutral {{--bg-t: var(--neutral-bg); --fg: var(--neutral-fg); --bd: var(--neutral-bd);}}

  [data-testid="stPlotlyChart"] {{background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
      box-shadow: var(--shadow); padding: 0.35rem 0.25rem;}}
  [data-testid="stDataFrame"] {{border: 1px solid var(--border); border-radius: 10px; overflow: hidden;}}
  .chart-note {{font-size: 0.78rem; color: var(--muted); margin: -0.3rem 0 0.8rem 0.2rem;}}
  .stButton > button, .stDownloadButton > button {{border-radius: 8px; font-weight: 550;}}
  [data-testid="stChatMessage"] {{background: var(--surface); border: 1px solid var(--border); border-radius: 10px;}}
  [data-testid="stExpander"] details {{background: var(--surface); border-radius: 10px;}}

  /* Cabecera y encabezados de página: los botones toman su ancho natural (no se recortan en laptops/tabletas) */
  [data-testid="stHorizontalBlock"]:has(.brand) {{flex-wrap: wrap; row-gap: 0.5rem;}}
  [data-testid="stHorizontalBlock"]:has(.brand) > [data-testid="stColumn"],
  [data-testid="stHorizontalBlock"]:has(.page-head) > [data-testid="stColumn"]:not(:first-child),
  [data-testid="stHorizontalBlock"]:has(.row-text) > [data-testid="stColumn"]:not(:first-child)
      {{flex: 0 0 auto !important; width: auto !important; min-width: 0;}}
  [data-testid="stHorizontalBlock"]:has(.brand) > [data-testid="stColumn"]:first-child,
  [data-testid="stHorizontalBlock"]:has(.page-head) > [data-testid="stColumn"]:first-child,
  [data-testid="stHorizontalBlock"]:has(.row-text) > [data-testid="stColumn"]:first-child
      {{flex: 1 1 280px !important; min-width: 0;}}
  [data-testid="stHorizontalBlock"]:has(.page-head), [data-testid="stHorizontalBlock"]:has(.row-text)
      {{flex-wrap: wrap; row-gap: 0.5rem;}}
  [data-testid="stHorizontalBlock"]:has(.page-head) > [data-testid="stColumn"]:not(:first-child) button,
  [data-testid="stHorizontalBlock"]:has(.row-text) > [data-testid="stColumn"]:not(:first-child) button
      {{min-width: 130px;}}

  /* Móvil y tabletas */
  @media (max-width: 1150px) {{.kpi-grid {{grid-template-columns: repeat(3, minmax(0, 1fr));}}}}
  @media (max-width: 700px) {{
    .kpi-grid {{grid-template-columns: repeat(2, minmax(0, 1fr));}}
    [data-testid="stMainBlockContainer"] {{padding-left: 0.8rem; padding-right: 0.8rem; padding-top: 3.6rem;}}
    .brand h1 {{font-size: 1.15rem;}}
    [data-testid="stHorizontalBlock"]:has(.brand) > [data-testid="stColumn"]:first-child {{flex-basis: 100% !important;}}
    .page-head h2 {{font-size: 1.3rem;}}
    .kpi-card {{min-height: 110px; padding: 0.7rem;}}
  }}
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Modo oscuro ("cuidado de la vista"). Paleta propia, no inversión: se redefinen los tokens y se ajustan los
# componentes nativos de Streamlit (campos, menús, pestañas, desplegables, avisos, ventanas emergentes).
# Las tablas nativas (st.dataframe) se dibujan en un lienzo que no lee CSS: solo a ellas se les aplica una
# inversión controlada para que no queden como un bloque blanco. Se guarda por usuario.
# ---------------------------------------------------------------------------
DARK_CSS = f"""
<style>
  :root {{{_vars(DARK_TOKENS)} color-scheme: dark;}}
  html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {{background: var(--bg) !important;
      color: var(--text);}}
  header[data-testid="stHeader"] {{background: var(--surface) !important;}}
  [data-testid="stSidebar"], [data-testid="stSidebarContent"] {{background: var(--surface) !important;}}
  [data-testid^="stBaseButton-header"], [data-testid^="stBaseButton-header"] span,
  [data-testid="stExpandSidebarButton"], [data-testid="stExpandSidebarButton"] span {{color: var(--text-2) !important;}}

  /* Texto */
  .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6,
  [data-testid="stMarkdownContainer"], [data-testid="stText"], [data-testid="stWidgetLabel"],
  [data-testid="stWidgetLabel"] p, :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) label, [data-testid="stMetricValue"],
  [data-testid="stMetricLabel"] {{color: var(--text);}}
  .stApp .brand h1 {{color: var(--brand);}}
  [data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p,
  [data-testid="stMarkdownContainer"] small {{color: var(--muted) !important;}}
  .stApp a {{color: var(--link);}}
  .stApp hr {{border-color: var(--border) !important; background: var(--border);}}
  .stApp code {{background: var(--surface-2); color: #F3C98B;}}
  [data-testid="stCode"] pre, .stApp pre {{background: var(--surface-2) !important; border: 1px solid var(--border);}}

  /* Navegación lateral y enlaces de página */
  [data-testid="stSidebarNav"] a span, [data-testid="stSidebarNavLink"] span,
  [data-testid="stNavSectionHeader"], [data-testid="stNavSectionHeader"] span,
  [data-testid="stPageLink"] a span, [data-testid="stPageLink-NavLink"] span {{color: var(--text-2) !important;}}
  [data-testid="stSidebarNav"] a:hover, [data-testid="stPageLink"] a:hover {{background: var(--surface-2) !important;}}
  [data-testid="stSidebarNav"] a[aria-current="page"] {{background: var(--brand-tint) !important;}}
  [data-testid="stSidebarNav"] a[aria-current="page"] span {{color: #A6D07A !important;}}

  /* Campos de texto, número, fecha, área de texto y selectores */
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-baseweb="input"], :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-baseweb="base-input"], :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-baseweb="textarea"],
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-baseweb="select"] > div, :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stNumberInputContainer"],
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stDateInputField"], :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stTextInputRootElement"],
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stTextAreaRootElement"], :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stSelectbox"] [role="group"],
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stMultiSelect"] [role="group"], :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stTimeInput"] [role="group"]
      {{background: var(--input-bg) !important;
      border-color: var(--border-strong) !important; color: var(--text) !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) input, :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) textarea, :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-baseweb="select"] span, :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-baseweb="select"] div
      {{color: var(--text) !important; caret-color: var(--text);}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stDateInputField"] input, :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stTextInputRootElement"] input,
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stNumberInputField"] {{background: transparent !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) input::placeholder, :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) textarea::placeholder {{color: var(--placeholder) !important; opacity: 1;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-baseweb="select"] svg, :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-baseweb="input"] svg,
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stNumberInputContainer"] button {{color: var(--text-2) !important; fill: currentColor;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stTextInputRootElement"] button, :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stTextInputRootElement"] svg
      {{color: var(--text-2) !important; background: transparent !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stNumberInputContainer"] button {{background: var(--surface-2) !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) input:disabled, :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) textarea:disabled {{color: var(--muted) !important; -webkit-text-fill-color: var(--muted);}}

  /* Menús desplegables, ventanas emergentes, calendario y tooltips (se dibujan fuera de .stApp) */
  [data-baseweb="popover"] > div, [data-baseweb="menu"], [data-baseweb="popover"] ul, [role="listbox"],
  [data-testid$="VirtualDropdown"],
  [data-testid="stPopoverBody"], [data-baseweb="calendar"] {{background: var(--surface) !important;
      color: var(--text) !important; border-color: var(--border) !important;}}
  [data-testid="stPopoverBody"] {{border: 1px solid var(--border) !important; box-shadow: var(--shadow-lg) !important;}}
  [data-testid="stPopoverBody"] * {{border-color: var(--border);}}
  [role="option"], [role="listbox"] li {{color: var(--text) !important; background: transparent;}}
  [role="option"]:hover, [role="option"][aria-selected="true"] {{background: var(--surface-2) !important;}}
  [data-baseweb="calendar"] * {{color: var(--text); background-color: transparent;}}
  [data-baseweb="calendar"] [aria-selected="true"] div {{background: var(--brand-green) !important;}}
  [data-baseweb="tooltip"] > div, [data-baseweb="tooltip"] [data-testid="stTooltipContent"],
  [data-testid="stTooltipContent"] {{background: #2B323D !important; color: var(--text) !important;}}
  [data-testid="stTooltipIcon"] svg {{color: var(--muted) !important;}}
  [role="dialog"], [data-testid="stDialog"] > div > div {{background: var(--surface) !important; color: var(--text);}}

  /* Botones: secundarios visibles (no parecen deshabilitados), primarios con buen contraste, estados claros */
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid^="stBaseButton-secondary"], :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stBaseButton-tertiary"],
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stPopoverButton"], :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stBaseButton-segmented_control"],
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stBaseButton-pills"], :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stBaseButton-borderlessIcon"] {{
      background: var(--surface) !important; color: var(--text) !important; border-color: var(--border-strong) !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid^="stBaseButton-secondary"] p, :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stPopoverButton"] p,
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stBaseButton-segmented_control"] p, :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stBaseButton-pills"] p
      {{color: var(--text) !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid^="stBaseButton-secondary"]:hover, :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stPopoverButton"]:hover,
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stBaseButton-segmented_control"]:hover, :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stBaseButton-pills"]:hover {{
      border-color: #8AB94F !important; background: var(--surface-2) !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) button[data-variant="segmented_control"], :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) button[data-variant="pills"] {{
      background: var(--surface) !important; color: var(--text) !important; border-color: var(--border-strong) !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) button[data-variant="segmented_control"] p, :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) button[data-variant="pills"] p {{color: var(--text-2) !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) button[data-variant="segmented_control"]:hover, :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) button[data-variant="pills"]:hover
      {{background: var(--surface-2) !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) button[data-variant="segmented_control"][aria-checked="true"], :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) button[data-variant="pills"][aria-checked="true"] {{
      background: var(--brand-tint) !important; border-color: #6E9E5E !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) button[data-variant="segmented_control"][aria-checked="true"] p,
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) button[data-variant="pills"][aria-checked="true"] p {{color: #B9DD8F !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid^="stBaseButton-primary"] {{background: var(--brand-green) !important;
      border-color: var(--brand-green) !important; color: #FFFFFF !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid^="stBaseButton-primary"] p {{color: #FFFFFF !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid^="stBaseButton-primary"]:hover {{background: var(--brand-green-hover) !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) button:disabled, :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) button[disabled] {{opacity: .55; cursor: not-allowed;}}

  /* Pestañas, radios, casillas e interruptores */
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-baseweb="tab-list"] button p {{color: var(--muted) !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-baseweb="tab-list"] button[aria-selected="true"] p {{color: var(--text) !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-baseweb="tab-border"] {{background: var(--border) !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-baseweb="tab-highlight"] {{background: #8AB94F !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stRadioOption"]:has(input:not(:checked)) > div > div:first-child
      {{background: var(--border-strong) !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stRadioOption"]:has(input:not(:checked)) > div > div:first-child > div
      {{background: var(--surface-2) !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) label:has(input[role="switch"]:not(:checked)) > div:first-of-type {{background: var(--border-strong) !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) label:has(input[type="checkbox"]:not([role="switch"]):not(:checked)) > div:first-of-type
      {{background: var(--surface-2) !important; border-color: var(--border-strong) !important;}}

  /* Contenedores: desplegables, formularios, bordes, chat */
  [data-testid="stExpander"] details {{background: var(--surface) !important; border-color: var(--border) !important;}}
  [data-testid="stExpander"] summary, [data-testid="stExpander"] summary p, [data-testid="stExpander"] summary svg
      {{color: var(--text) !important;}}
  [data-testid="stExpander"] summary:hover p, [data-testid="stExpander"] summary:hover svg {{color: #A6D07A !important;}}
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stForm"], :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stVerticalBlock"],
  :is(.stApp, [data-testid="stPopoverBody"], [role="dialog"]) [data-testid="stVerticalBlockBorderWrapper"] {{border-color: var(--border-strong) !important;}}
  [data-testid="stChatInput"], [data-testid="stChatInput"] > div {{background: var(--surface-2) !important;
      border-color: var(--border-strong) !important;}}
  [data-testid="stChatInput"] textarea {{color: var(--text) !important;}}
  [data-testid="stChatInputSubmitButton"] {{color: var(--text-2) !important;}}
  [data-testid="stFileUploaderDropzone"] {{background: var(--surface-2) !important; border-color: var(--border-strong) !important;}}
  [data-testid="stFileUploaderDropzone"] span, [data-testid="stFileUploaderDropzone"] small {{color: var(--text-2) !important;}}
  [data-testid="stTable"] table, [data-testid="stTable"] th, [data-testid="stTable"] td {{color: var(--text) !important;
      border-color: var(--border) !important; background: var(--surface);}}
  [data-testid="stSpinner"] {{color: var(--text-2);}}
  [data-testid="stBottom"], [data-testid="stBottom"] > div, [data-testid="stBottomBlockContainer"]
      {{background: var(--bg) !important;}}

  /* Avisos nativos (st.info / success / warning / error) */
  [data-testid="stAlertContainer"] {{border: 1px solid var(--bd, transparent);}}
  [data-testid="stAlertContainer"]:has([data-testid="stAlertContentInfo"]) {{background: var(--info-bg) !important; --bd: var(--info-bd);}}
  [data-testid="stAlertContainer"]:has([data-testid="stAlertContentSuccess"]) {{background: var(--ok-bg) !important; --bd: var(--ok-bd);}}
  [data-testid="stAlertContainer"]:has([data-testid="stAlertContentWarning"]) {{background: var(--warn-bg) !important; --bd: var(--warn-bd);}}
  [data-testid="stAlertContainer"]:has([data-testid="stAlertContentError"]) {{background: var(--danger-bg) !important; --bd: var(--danger-bd);}}
  [data-testid="stAlertContentInfo"], [data-testid="stAlertContentInfo"] * {{color: var(--info-fg) !important;}}
  [data-testid="stAlertContentSuccess"], [data-testid="stAlertContentSuccess"] * {{color: var(--ok-fg) !important;}}
  [data-testid="stAlertContentWarning"], [data-testid="stAlertContentWarning"] * {{color: var(--warn-fg) !important;}}
  [data-testid="stAlertContentError"], [data-testid="stAlertContentError"] * {{color: var(--danger-fg) !important;}}
  [data-testid="stToast"] {{background: var(--surface-2) !important; color: var(--text) !important;}}

  /* Tablas nativas (lienzo): inversión solo del lienzo, las imágenes internas vuelven a su color */
  [data-testid="stDataFrame"] {{border-color: var(--border) !important; background: var(--surface);}}
  [data-testid="stDataFrame"] > div {{filter: invert(0.88) hue-rotate(180deg) contrast(0.95);}}
  [data-testid="stDataFrame"] img {{filter: invert(1) hue-rotate(180deg);}}

  /* Logo: sobre fondo oscuro se le da una base clara para conservar los colores de la marca */
  .brand img {{background: #F3F6F4; border-radius: 12px; padding: 3px;}}
</style>
"""

# Colores de las series que en fondo oscuro pierden contraste (azul institucional, grises): se aclaran
_DARK_SERIES = {NAVY.upper(): "#9EA3F0", "#3A6EA5": "#6FA8DC", BLUE_LIGHT.upper(): "#B4B8F2",
                "#0F766E": "#2DD4BF", GREY.upper(): "#8B95A3", TEXT.upper(): "#E7EAEE", MUTED.upper(): "#9AA4B1",
                BORDER.upper(): "#3B4450", "#FFFFFF": "#171C23", "#374151": "#C9CFD7"}


def inject_dark(enabled: bool) -> None:
    st.session_state["_dark_ui"] = bool(enabled)
    if enabled:
        st.markdown(DARK_CSS, unsafe_allow_html=True)


def is_dark() -> bool:
    try:
        return bool(st.session_state.get("_dark_ui", False))
    except Exception:  # fuera de una sesión de Streamlit (pruebas)
        return False


# ---------------------------------------------------------------------------
# Iconos SVG (trazo, 24x24, heredan el color del texto: funcionan en modo claro y oscuro)
# ---------------------------------------------------------------------------
_ICON_PATHS = {
    "activity": '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    "calendar": '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    "trend-up": '<path d="M22 7l-8.5 8.5-5-5L2 17"/><path d="M16 7h6v6"/>',
    "trend-down": '<path d="M22 17l-8.5-8.5-5 5L2 7"/><path d="M16 17h6v-6"/>',
    "minus": '<path d="M5 12h14"/>',
    "box": '<path d="M21 8l-9-5-9 5v8l9 5 9-5z"/><path d="M3 8l9 5 9-5M12 13v8"/>',
    "clipboard": '<rect x="8" y="2" width="8" height="4" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6'
                 'a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M9 12l2 2 4-4"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
    "alert": '<path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>'
             '<path d="M12 9v4M12 17h.01"/>',
    "check": '<circle cx="12" cy="12" r="10"/><path d="M8 12l3 3 5-6"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    "database": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5"/>'
                '<path d="M3 12c0 1.7 4 3 9 3s9-1.3 9-3"/>',
    "eye": '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8S1 12 1 12z"/><circle cx="12" cy="12" r="3"/>',
}


def icon(name: str, size: int = 16, label: str = "") -> str:
    """Icono SVG en línea. Sin `label` es decorativo (aria-hidden); con `label` lo anuncia el lector de pantalla."""
    a11y = f'role="img" aria-label="{esc(label)}"' if label else 'aria-hidden="true"'
    return (f'<svg class="ico" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" {a11y}>'
            f'{_ICON_PATHS.get(name, _ICON_PATHS["info"])}</svg>')


# ---------------------------------------------------------------------------
# Componentes HTML
# ---------------------------------------------------------------------------
def esc(value) -> str:
    return html.escape(str(value))


_MARKERS = {"●": '<span class="dot" aria-hidden="true"></span>', "▲": '<span class="tri" aria-hidden="true"></span>'}


def chip(text: str, tone: str = "neutral", icon: str = "") -> str:
    """Etiqueta de estado. `icon` admite "●" (punto) y "▲" (expansión), dibujados con CSS, o HTML de icon()."""
    tone = tone if tone in CHIP_TONES or tone == "orange" else "neutral"
    mark = _MARKERS.get(icon, icon)
    return f'<span class="chip t-{tone}">{mark}{esc(text)}</span>'


def banner(text: str, tone: str = "info") -> None:
    tone = tone if tone in CHIP_TONES else "info"
    st.markdown(f'<div class="banner t-{tone}">{text}</div>', unsafe_allow_html=True)


def page_header(title: str, subtitle: str = "", container=st) -> None:
    """Título de página con una línea que dice para qué sirve la pantalla (orientación para no técnicos)."""
    sub = f"<p>{esc(subtitle)}</p>" if subtitle else ""
    container.markdown(f'<div class="page-head"><h2>{esc(title)}</h2>{sub}</div>', unsafe_allow_html=True)


def section_title(text: str, container=st) -> None:
    container.markdown(f'<div class="section-title">{esc(text)}</div>', unsafe_allow_html=True)


def severity_pill(severity: str) -> str:
    label = SEVERITY.get(severity, ("•",))[0]
    return f'<span class="pill t-{SEVERITY_TONE.get(severity, "neutral")}">{label}</span>'


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
    accent = "var(--border)" if accent == BORDER else accent
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
def _remap(color):
    """Aclara en modo oscuro los colores de serie que se pierden sobre fondo oscuro."""
    if isinstance(color, str):
        return _DARK_SERIES.get(color.upper(), color)
    if isinstance(color, (list, tuple)):
        return [_remap(c) for c in color]
    return color


def _darken_traces(fig: go.Figure) -> None:
    for tr in fig.data:
        for attr in ("marker", "line", "textfont", "outsidetextfont", "insidetextfont"):
            obj = getattr(tr, attr, None)
            if obj is not None and getattr(obj, "color", None) is not None:
                try:
                    obj.color = _remap(obj.color)
                except ValueError:
                    pass
            inner = getattr(obj, "line", None) if obj is not None else None
            if inner is not None and getattr(inner, "color", None) is not None:
                try:
                    inner.color = _remap(inner.color)
                except ValueError:
                    pass
        mk = getattr(tr, "marker", None)
        if mk is not None and getattr(mk, "colors", None) is not None:  # tortas (px.pie usa marker.colors)
            mk.colors = _remap(list(mk.colors))
        if getattr(tr, "fillcolor", None):
            tr.fillcolor = _remap(tr.fillcolor)
        if tr.type == "heatmap" and getattr(tr, "colorscale", None):
            tr.colorscale = [[s, _remap(c)] for s, c in tr.colorscale]
    for attr in ("piecolorway", "colorway"):
        if getattr(fig.layout, attr, None):
            setattr(fig.layout, attr, _remap(list(getattr(fig.layout, attr))))
    for shape in fig.layout.shapes or []:
        if shape.line and shape.line.color:
            shape.line.color = _remap(shape.line.color)
    if fig.layout.coloraxis and fig.layout.coloraxis.colorscale:
        fig.layout.coloraxis.colorscale = [[s, _remap(c)] for s, c in fig.layout.coloraxis.colorscale]


def style_fig(fig: go.Figure, height: int = 340, title: str | None = None) -> go.Figure:
    dark = is_dark()
    surface, text, muted, grid_c, line_c, body = (("#171C23", "#E7EAEE", "#9AA4B1", "#262D37", "#3B4450", "#C9CFD7")
                                                  if dark else ("#FFFFFF", TEXT, MUTED, "#F1F5F9", BORDER, "#374151"))
    fig.update_layout(
        template="plotly_dark" if dark else "simple_white", height=height, paper_bgcolor=surface,
        plot_bgcolor=surface, margin=dict(l=10, r=10, t=30 if not title else 42, b=10),
        font=dict(family=FONT_STACK, size=12, color=body),
        title=dict(text=title, x=0.01, xanchor="left", font=dict(size=14, color=text)) if title else None,
        legend=dict(orientation="h", y=-0.18, x=0, title=None, font=dict(size=11, color=body)),
        hoverlabel=dict(bgcolor="#262D37" if dark else "white", font_family=FONT_STACK,
                        font_color=text, bordercolor=line_c), bargap=0.25)
    fig.update_xaxes(showgrid=False, linecolor=line_c, tickfont=dict(color=muted), title_font=dict(color=muted),
                     zerolinecolor=line_c)
    fig.update_yaxes(automargin=True, gridcolor=grid_c, linecolor=line_c, tickfont=dict(color=body),
                     title_font=dict(color=muted), zerolinecolor=line_c)
    if dark:
        _darken_traces(fig)
    return fig


def show(fig: go.Figure, key: str | None = None) -> None:
    st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG, key=key)


def short_labels(texts: pd.Series, width: int = 58) -> pd.Series:
    """Recorta etiquetas sin fusionar ítems distintos que compartan prefijo."""
    base = texts.astype(str).map(lambda t: t if len(t) <= width else t[: width - 1].rstrip() + "…")
    counts = base.groupby(base).cumcount()
    return base.where(counts == 0, base + " (" + (counts + 1).astype(str) + ")")
