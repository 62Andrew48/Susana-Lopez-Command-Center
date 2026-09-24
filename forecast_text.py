"""
forecast_text.py — Traduce la salida técnica de los microservicios (/predict y /kpis) a lenguaje de gestión.

Regla: todo lo que se dice sale de los números que devuelve el servicio. No se inventan ratios de personal,
porcentajes de ocupación de quirófanos ni cantidades de insumos: si el dato no existe, no se afirma.

  * Nivel      : el pronóstico frente a lo habitual del propio servicio (promedio de las 4 semanas previas o
                 del mismo día de la semana). ±15 % se considera habitual.
  * Confianza  : si el modelo mejoró o no a la referencia simple (repetir el mismo día de la semana anterior),
                 y de cuánto suele ser el error, en las unidades del servicio.
  * Qué hacer  : 1-3 acciones apoyadas en los KPI reales del servicio.
No depende de Streamlit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre"]
BAND = 0.15  # ±15 % respecto a lo habitual = "habitual"

UNITS = {"urgencias": "ingresos a urgencias", "quirofanos": "cirugías",
         "farmacia": "entregas de medicamentos e insumos", "consultas": "ingresos por consulta externa"}
ABOUT = {"quirofanos": "unas", "farmacia": "unas"}  # el resto es masculino: "unos"


@dataclass
class Reading:
    code: str
    headline: str                 # "Se esperan unos 112 ingresos a urgencias el martes 22 de septiembre."
    range_text: str               # "Lo normal sería entre 63 y 162."
    level: str                    # Alta | Habitual | Baja | Sin datos suficientes
    level_tone: str               # danger | ok | neutral | warn  (para chips)
    level_text: str               # "Similar a lo habitual (119 por día en las 4 semanas previas)."
    confidence: str               # Confiable | Solo orientativo
    confidence_tone: str
    confidence_text: str
    actions: list[str] = field(default_factory=list)


def num(v, decimals: int | None = None) -> str:
    """Número a la colombiana: 4.388 · 0,4 · 9."""
    if v is None:
        return "—"
    if decimals is None:
        decimals = 0 if abs(v) >= 5 else 1
    text = f"{v:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return text[:-2] if text.endswith(",0") else text


def day_text(iso: str | None) -> str:
    try:
        d = date.fromisoformat(str(iso)[:10])
    except (TypeError, ValueError):
        return "el próximo día"
    return f"el {DAYS[d.weekday()]} {d.day} de {MONTHS[d.month - 1]}"


def _level(value, usual, label_usual: str) -> tuple[str, str, str]:
    if value is None or not usual:
        return "Sin referencia", "neutral", "No hay un promedio previo con qué compararlo."
    ratio = value / usual
    if ratio >= 1 + BAND:
        return "Alta", "danger", f"{num((ratio - 1) * 100, 0)} % por encima de lo habitual ({num(usual)} {label_usual})."
    if ratio <= 1 - BAND:
        return "Baja", "neutral", f"{num((1 - ratio) * 100, 0)} % por debajo de lo habitual ({num(usual)} {label_usual})."
    return "Habitual", "ok", f"Similar a lo habitual ({num(usual)} {label_usual})."


def _confidence(pred: dict, unit: str) -> tuple[str, str, str]:
    m = pred.get("metricas") or {}
    mae, base = m.get("mae"), m.get("mae_baseline")
    if m.get("supera_baseline"):
        return ("Confiable", "ok",
                f"En las últimas 4 semanas acertó mejor que repetir la semana anterior. Suele desviarse "
                f"en {num(mae)} {unit} por día.")
    return ("Solo orientativo", "warn",
            f"En las últimas 4 semanas no acertó mejor que mirar el mismo día de la semana anterior "
            f"(se desvía en {num(mae)} {unit} por día frente a {num(base)}). Úsalo como referencia, no para decidir.")


def _share(rows: list[dict], key: str, value: str) -> tuple[dict | None, float]:
    total = sum(r.get(value) or 0 for r in rows)
    top = max(rows, key=lambda r: r.get(value) or 0, default=None)
    return top, ((top or {}).get(value) or 0) / total * 100 if total else 0.0


def _actions_urgencias(k: dict) -> list[str]:
    out = []
    turns = k.get("por_turno_7d") or []
    top, pct = _share(turns, "turno", "ingresos")
    if top:
        out.append(f"El turno de la {top['turno'].lower()} recibe el {num(pct, 0)} % de los ingresos de la "
                   f"última semana: es el primero a reforzar si sube la demanda.")
    slow = max(turns, key=lambda r: r.get("espera_promedio_min") or 0, default=None)
    if slow and slow is not top:
        out.append(f"La espera más larga está en el turno de la {slow['turno'].lower()} "
                   f"({num(slow['espera_promedio_min'], 0)} min en promedio).")
    t2 = k.get("cumplimiento_meta_triage2_pct_7d")
    if t2 is not None:
        out.append(f"Solo el {num(t2, 0)} % de los triage II se atendió dentro de los "
                   f"{k.get('meta_triage2_min', 30)} minutos de la meta.")
    return out


def _actions_quirofanos(k: dict) -> list[str]:
    out = []
    areas = k.get("por_area_quirofano_28d") or []
    if areas:
        a = areas[0]
        name = str(a["area_quirofano"]).replace("QUIROFANOS - ", "").replace("QUIROFANOS ", "").capitalize()
        out.append(f"{name} es el área con más cirugías ({num(a['promedio_diario'])} por día en 4 semanas).")
    days = k.get("promedio_por_dia_semana_8sem") or []
    if days:
        peak = max(days, key=lambda r: r.get("promedio_cirugias") or 0)
        out.append(f"El día más cargado es el {peak['dia_semana']} ({num(peak['promedio_cirugias'])} cirugías en promedio).")
    prog = k.get("programacion_con_ingreso_en_periodo") or {}
    if prog.get("cumplimiento_pct") is not None:
        out.append(f"El {num(prog['cumplimiento_pct'], 0)} % de lo programado tiene evidencia de haberse realizado.")
    return out


def _actions_farmacia(k: dict, urgent_items: int | None) -> list[str]:
    out = []
    if urgent_items is not None:
        out.append(f"{urgent_items} ítems se agotan en menos de 5 días: la orden de compra está en "
                   f"Alertas y acciones.")
    top = (k.get("top10_items_30d") or [])[:1]
    if top:
        t = top[0]
        out.append(f"El de mayor salida es {str(t['nombre']).capitalize()} ({num(t['unidades'])} unidades en 30 días).")
    return out


def interpret(code: str, pred: dict, kpis: dict | None = None, urgent_items: int | None = None) -> Reading:
    k = kpis or {}
    unit = UNITS.get(code, "unidades")
    value = pred.get("prediccion")
    lo, hi = (pred.get("intervalo") or [None, None])[:2]
    when = day_text(pred.get("fecha_objetivo"))
    headline = f"Se esperan {ABOUT.get(code, 'unos')} {num(value)} {unit} {when}."
    dec = 0 if value is not None and abs(value) >= 5 else 1
    range_text = f"Lo normal sería entre {num(lo, dec)} y {num(hi, dec)}."
    conf, conf_tone, conf_text = _confidence(pred, unit)
    actions: list[str] = []

    if code == "urgencias":
        level = _level(value, k.get("promedio_diario_28d_previos"), "por día en las 4 semanas previas")
        actions = _actions_urgencias(k)
    elif code == "quirofanos":
        usual = None
        try:
            dname = DAYS[date.fromisoformat(pred["fecha_objetivo"]).weekday()]
            usual = next((r["promedio_cirugias"] for r in k.get("promedio_por_dia_semana_8sem") or []
                          if r.get("dia_semana") == dname), None)
            label = f"un {dname} normal"
        except (KeyError, TypeError, ValueError):
            label = "por día"
        level = _level(value, usual or k.get("promedio_diario_28d_previos"), label)
        actions = _actions_quirofanos(k)
    elif code == "farmacia":
        level = _level(value, k.get("promedio_diario_dispensaciones_28d_previos"), "por día en las 4 semanas previas")
        actions = _actions_farmacia(k, urgent_items)
    elif code == "consultas":
        share = k.get("participacion_ambulatoria_28d_pct")
        if share is not None and share < 1:
            level = ("Sin datos suficientes", "warn",
                     f"La consulta externa es solo el {num(share)} % de los ingresos del extracto.")
            headline = f"Consulta externa: casi sin movimiento en los datos ({num(value)} ingresos por día)."
            actions = ["El extracto no trae citas agendadas ni inasistencias, así que este pronóstico no sirve "
                       "todavía para planear la consulta externa."]
        else:
            level = _level(value, k.get("promedio_diario_28d_previos"), "por día en las 4 semanas previas")
    else:
        level = ("Sin referencia", "neutral", "")

    return Reading(code, headline, range_text, *level, conf, conf_tone, conf_text, actions)
