"""
forecast_text.py — Traduce la salida técnica de los microservicios (/predict y /kpis) a lenguaje de gestión.

Regla: todo lo que se dice sale de los números que devuelve el servicio. No se inventan ratios de personal,
porcentajes de ocupación de quirófanos ni cantidades de insumos: si el dato no existe, no se afirma.

  * Nivel      : el pronóstico frente a lo habitual del propio servicio (promedio de las 4 semanas previas o
                 del mismo día de la semana). ±15 % se considera habitual.
  * Confianza  : si el modelo mejoró o no a la referencia simple (repetir el mismo día de la semana anterior),
                 y de cuánto suele ser el error, en las unidades del servicio.
  * Datos      : 1-3 hechos de los KPI reales del servicio que respaldan la lectura (campo `actions`).
  * Revisar    : qué recursos podrían verse afectados y qué conviene verificar. Es una lista de verificación
                 administrativa redactada en condicional ("conviene revisar", "podría requerir"): nunca dice
                 cuánto comprar ni a cuántas personas contratar, porque los modelos no predicen eso.
  * Resumen    : admin_summary() responde las 5 preguntas del administrador (qué pasa, cuándo, qué cambia,
                 qué recursos, qué revisar) a partir de las lecturas de todos los servicios.
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
    range_text: str               # "Rango probable: entre 63 y 162."
    level: str                    # Alta | Habitual | Baja | Sin datos suficientes
    level_tone: str               # danger | ok | neutral | warn  (para chips)
    level_text: str               # "Similar a lo habitual (119 por día en las 4 semanas previas)."
    confidence: str               # Confiable | Solo orientativo
    confidence_tone: str
    confidence_text: str
    actions: list[str] = field(default_factory=list)       # hechos de los datos recientes que respaldan la lectura
    period: str = ""              # "Martes 22 de septiembre · 1 día después del último dato"
    change_pct: float | None = None  # variación frente a lo habitual (+18.0 = 18 % más)
    change_text: str = ""         # "Aumento de 18 % frente a lo habitual"
    why: str = ""                 # por qué importa para la gestión del hospital
    resources: list[str] = field(default_factory=list)  # recursos que podrían verse afectados
    review: list[str] = field(default_factory=list)     # qué conviene revisar (lenguaje responsable)
    reliable: bool = False        # True si el modelo superó a la referencia simple
    target_date: str = ""         # fecha_objetivo ISO (para ordenar periodos)


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


def period_text(iso: str | None, horizon: int | None = None) -> str:
    """'Martes 22 de septiembre · próximo día' a partir de fecha_objetivo y horizonte_dias del servicio."""
    try:
        d = date.fromisoformat(str(iso)[:10])
    except (TypeError, ValueError):
        return "Próximo día"
    when = f"{DAYS[d.weekday()].capitalize()} {d.day} de {MONTHS[d.month - 1]}"
    if horizon and horizon > 1:
        return f"{when} · {horizon} días hacia adelante"
    return f"{when} · próximo día"


def _change(value, usual) -> tuple[float | None, str]:
    if value is None or not usual:
        return None, "Sin punto de comparación"
    pct = (value / usual - 1) * 100
    if abs(pct) < BAND * 100:
        return pct, f"Sin cambio importante ({'+' if pct >= 0 else '−'}{num(abs(pct), 0)} %)"
    word = "Aumento" if pct > 0 else "Disminución"
    return pct, f"{word} de {num(abs(pct), 0)} %"


# Qué significa para la gestión y qué conviene revisar, por servicio y nivel. Es un listado de verificación
# (lenguaje condicional), no una orden: los modelos predicen volumen de atención, no necesidades de compra.
WHY = {
    "urgencias": {
        "Alta": "Más ingresos a urgencias suelen alargar la espera y presionar las camas de observación y de "
                "hospitalización.",
        "Habitual": "La demanda esperada está dentro de lo normal para el servicio.",
        "Baja": "Se espera menos movimiento que de costumbre en urgencias.",
    },
    "quirofanos": {
        "Alta": "Un día con más cirugías de lo normal exige salas, equipos quirúrgicos y camas de recuperación "
                "disponibles a la vez.",
        "Habitual": "La carga quirúrgica esperada está dentro de lo normal para ese día de la semana.",
        "Baja": "Se esperan menos cirugías que en un día normal.",
    },
    "farmacia": {
        "Alta": "Más entregas de medicamentos e insumos consumen existencias más rápido de lo habitual.",
        "Habitual": "El movimiento esperado de farmacia está dentro de lo normal.",
        "Baja": "Se espera menos movimiento de farmacia que de costumbre.",
    },
}
RESOURCES = {
    "urgencias": ["Personal médico y de enfermería de urgencias", "Camas de observación y de hospitalización",
                  "Insumos de atención inicial (curaciones, líquidos, elementos de protección)"],
    "quirofanos": ["Salas de cirugía y equipo quirúrgico (cirujanos, anestesia, instrumentación)",
                   "Instrumental e insumos quirúrgicos", "Camas de recuperación y de cuidado intensivo"],
    "farmacia": ["Existencias de medicamentos e insumos de mayor salida", "Personal de dispensación"],
}
REVIEW = {
    "urgencias": ["Conviene revisar la cobertura de personal en el turno {turno}.",
                  "Se recomienda verificar la disponibilidad de camas libres para hospitalizar.",
                  "Conviene verificar las existencias de insumos de urgencias."],
    "quirofanos": ["Se recomienda confirmar la programación del día con cada área quirúrgica.",
                   "Conviene verificar la disponibilidad de camas de recuperación y UCI posquirúrgica.",
                   "Conviene revisar instrumental e insumos quirúrgicos."],
    "farmacia": ["Se recomienda revisar la orden de compra de los ítems urgentes (Alertas y acciones).",
                 "Conviene verificar las existencias de los medicamentos de mayor salida."],
}


def _guidance(code: str, level: str, reliable: bool, k: dict) -> tuple[str, list[str], list[str]]:
    """(por qué importa, recursos que podrían verse afectados, qué revisar) según el nivel del pronóstico."""
    if level == "Sin datos suficientes":
        return ("Los datos disponibles no alcanzan para anticipar la demanda de este servicio.", [],
                ["No se recomienda tomar decisiones con este pronóstico hasta contar con más datos."])
    why = WHY.get(code, {}).get(level, "")
    if level == "Alta":
        top = max(k.get("por_turno_7d") or [], key=lambda r: r.get("ingresos") or 0, default=None)
        turno = f"de la {top['turno'].lower()}" if top else "de mayor demanda"
        review = [r.format(turno=turno) for r in REVIEW.get(code, [])]
        if not reliable:
            review = ["Pronóstico solo orientativo: confirmar con el jefe del servicio antes de mover recursos.",
                      *review[:2]]
        return why, list(RESOURCES.get(code, [])), review
    if level == "Baja":
        return (why, [], ["Podría ser un buen momento para programar mantenimiento, capacitación o descansos; "
                          "conviene confirmarlo con el jefe del servicio."])
    if level == "Habitual":
        return why, [], ["No se identifica una necesidad adicional: mantener la operación normal."]
    return why, [], []


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
    range_text = f"Rango probable: entre {num(lo, dec)} y {num(hi, dec)}."
    conf, conf_tone, conf_text = _confidence(pred, unit)
    actions: list[str] = []
    usual = None

    if code == "urgencias":
        usual = k.get("promedio_diario_28d_previos")
        level = _level(value, usual, "por día en las 4 semanas previas")
        actions = _actions_urgencias(k)
    elif code == "quirofanos":
        usual = None
        try:
            dname = DAYS[date.fromisoformat(pred["fecha_objetivo"]).weekday()]
            usual = next((r["promedio_cirugias"] for r in k.get("promedio_por_dia_semana_8sem") or []
                          if r.get("dia_semana") == dname), None)
            label = f"en un {dname} normal"
        except (KeyError, TypeError, ValueError):
            label = "por día"
        usual = usual or k.get("promedio_diario_28d_previos")
        level = _level(value, usual, label)
        actions = _actions_quirofanos(k)
    elif code == "farmacia":
        usual = k.get("promedio_diario_dispensaciones_28d_previos")
        level = _level(value, usual, "por día en las 4 semanas previas")
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
            usual = k.get("promedio_diario_28d_previos")
            level = _level(value, usual, "por día en las 4 semanas previas")
    else:
        level = ("Sin referencia", "neutral", "")

    reliable = bool((pred.get("metricas") or {}).get("supera_baseline"))
    change_pct, change_text = _change(value, usual) if level[0] != "Sin datos suficientes" else (None, "")
    why, resources, review = _guidance(code, level[0], reliable, k)
    return Reading(code, headline, range_text, *level, conf, conf_tone, conf_text, actions,
                   period=period_text(pred.get("fecha_objetivo"), pred.get("horizonte_dias")),
                   change_pct=change_pct, change_text=change_text, why=why, resources=resources, review=review,
                   reliable=reliable, target_date=str(pred.get("fecha_objetivo") or "")[:10])


# ---------------------------------------------------------------------------
# Resumen para el administrador (las 5 preguntas)
# ---------------------------------------------------------------------------
@dataclass
class AdminSummary:
    what: str                 # ¿Qué está ocurriendo?
    when: str                 # ¿En qué periodo?
    changes: list[str]        # ¿Qué cambio se espera? (una línea por servicio)
    resources: list[str]      # ¿Qué recursos podrían verse afectados?
    review: list[str]         # ¿Qué debería revisar el administrador?
    tone: str                 # danger | warn | ok (para el color del bloque)


def admin_summary(readings: list[tuple[str, Reading | None]]) -> AdminSummary:
    """readings = [(nombre del área, lectura o None si el servicio no respondió)]. Solo usa lo que hay."""
    ok = [(n, r) for n, r in readings if r is not None]
    down = [n for n, r in readings if r is None]
    high = [(n, r) for n, r in ok if r.level == "Alta"]
    low = [(n, r) for n, r in ok if r.level == "Baja"]
    nodata = [n for n, r in ok if r.level == "Sin datos suficientes"]

    if not ok:
        return AdminSummary("No hay pronósticos disponibles en este momento.", "—", [], [],
                            ["Verificar que los servicios de pronóstico estén encendidos y volver a actualizar."],
                            "warn")
    if high:
        names = ", ".join(f"{n} ({'+' if (r.change_pct or 0) >= 0 else ''}{num(r.change_pct, 0)} %)" for n, r in high)
        what = f"Se espera más demanda de lo habitual en {names}."
        tone = "danger" if any(r.reliable for _, r in high) else "warn"
    else:
        what = "Ningún servicio muestra una demanda por encima de lo habitual."
        tone = "ok"
    if low:
        what += " Menos movimiento de lo normal en " + ", ".join(n for n, _ in low) + "."

    dated = sorted((r.target_date, r.period.split(" · ")[0]) for _, r in ok if r.period)
    periods = list(dict.fromkeys(p for _, p in dated))
    periods = periods[:1] + [p[0].lower() + p[1:] for p in periods[1:]]
    when = (" y ".join(periods) if periods else "Próximo día") + " (el día siguiente al último dato de cada servicio)"

    changes = []
    for n, r in ok:
        if r.level == "Sin datos suficientes":
            changes.append(f"{n}: datos insuficientes para anticipar la demanda.")
        else:
            changes.append(f"{n}: {(r.change_text or r.level).lower()} frente a lo habitual"
                           f"{'' if r.reliable else ' · solo orientativo'}.")
    changes += [f"{n}: pronóstico no disponible en este momento." for n in down]

    resources = list(dict.fromkeys(x for _, r in high for x in r.resources))
    review: list[str] = []
    for _, r in sorted(high, key=lambda nr: not nr[1].reliable):
        review += [x for x in r.review if x not in review]
    if not review:
        review = ["No se identifican necesidades adicionales para el próximo día: mantener la operación normal."]
    if nodata:
        review.append(f"{', '.join(nodata)}: no usar el pronóstico para planear (datos insuficientes).")
    return AdminSummary(what, when, changes, resources, review[:4], tone)
