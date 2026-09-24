"""
month_report.py — Reporte "en lo que va del mes": desde el día 1 del mes hasta la fecha de corte de los datos.

En vez de "se atendieron 1.000 pacientes en el mes" dice "del 1 al 21 de septiembre (21 días) se han atendido 2.391
personas", y lo compara con los MISMOS días del mes anterior (1 al 21 de agosto), que es la única comparación justa
con un mes a medias. Al pasar al mes siguiente el reporte arranca de nuevo el día 1.

Todo sale de hospital.db (extracto del HIS). La proyección al cierre del mes es una regla de tres con el ritmo
diario y se rotula como tal. Sin datos de pacientes: solo conteos. No depende de Streamlit.
"""
from __future__ import annotations

import calendar
import sqlite3
from datetime import date, timedelta

import database as db
from agent import fmt_num

MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre"]


def period(ref: date) -> tuple[date, date, date, date]:
    """(inicio, fin) del mes en curso hasta `ref` y los mismos días del mes anterior."""
    start = ref.replace(day=1)
    prev_end_of_month = start - timedelta(days=1)
    prev_start = prev_end_of_month.replace(day=1)
    prev_end = prev_start.replace(day=min(ref.day, prev_end_of_month.day))
    return start, ref, prev_start, prev_end


def _counts(conn: sqlite3.Connection, start: date, end: date) -> dict:
    lo, hi = f"{start:%Y-%m-%d} 00:00:00", f"{end:%Y-%m-%d} 23:59:59"
    ing = conn.execute("""
        SELECT COUNT(*), COUNT(DISTINCT id_paciente), SUM(clase_ingreso = 'Hospitalario'),
               SUM(via_ingreso = 'Urgencias') FROM ingresos WHERE fecha_ingreso BETWEEN ? AND ?""", (lo, hi)).fetchone()
    triage = conn.execute("SELECT COUNT(*) FROM triage WHERE fecha_triage BETWEEN ? AND ?", (lo, hi)).fetchone()[0]
    surg = conn.execute("SELECT COUNT(*) FROM cirugias WHERE estado = 'Realizada' AND fecha_cirugia BETWEEN ? AND ?",
                        (lo, hi)).fetchone()[0]
    meds = conn.execute("SELECT SUM(cantidad) FROM medicamentos_insumos WHERE fecha_prestacion BETWEEN ? AND ?",
                        (lo, hi)).fetchone()[0]
    occ = conn.execute("""
        SELECT AVG(p) FROM (SELECT fecha, 100.0 * SUM(camas_ocupadas) / SUM(capacidad) AS p FROM ocupacion_diaria
         WHERE fecha BETWEEN ? AND ? AND servicio <> 'Urgencias' AND capacidad > 0 GROUP BY fecha)""",
                       (start.isoformat(), end.isoformat())).fetchone()[0]
    wait = db.kpi_wait_times(conn, start, end)["promedio_min"]
    return {"personas": int(ing[1] or 0), "ingresos": int(ing[0] or 0), "hospitalizados": int(ing[2] or 0),
            "urgencias": int(ing[3] or 0), "triage": int(triage or 0), "cirugias": int(surg or 0),
            "dispensado": int(meds or 0), "ocupacion": round(occ, 1) if occ is not None else None,
            "espera": wait}


METRICS = [  # clave, indicador, unidad, ¿más es mejor? (None = neutro)
    ("personas", "Personas atendidas (distintas)", "", None),
    ("ingresos", "Ingresos registrados", "", None),
    ("urgencias", "Llegaron por urgencias", "", None),
    ("hospitalizados", "Ingresos hospitalarios", "", None),
    ("triage", "Clasificaciones de triage", "", None),
    ("cirugias", "Cirugías realizadas", "", True),
    ("ocupacion", "Ocupación promedio de camas (sin urgencias)", " %", None),
    ("espera", "Espera promedio en urgencias", " min", False),
    ("dispensado", "Unidades de medicamentos e insumos entregadas", "", None),
]


def _var(now, before) -> float | None:
    if now is None or not before:
        return None
    return round((now - before) / before * 100, 1)


def month_to_date(conn: sqlite3.Connection, ref: date) -> dict:
    start, end, p_start, p_end = period(ref)
    cur, prev = _counts(conn, start, end), _counts(conn, p_start, p_end)
    days = (end - start).days + 1
    month_days = calendar.monthrange(ref.year, ref.month)[1]
    rows = []
    for key, label, unit, better in METRICS:
        v, b = cur[key], prev[key]
        change = _var(v, b)
        points = key == "ocupacion" and v is not None and b is not None
        if change is None:
            reading = "Sin comparación"
        elif abs(change) < 5:
            reading = "Similar al mes anterior"
        else:
            reading = f"{'Más' if change > 0 else 'Menos'} que el mes anterior"
        decimals = 1 if key in ("ocupacion", "espera") else 0
        rows.append({"clave": key, "indicador": label,
                     "este_mes": f"{fmt_num(v, decimals)}{unit}" if v is not None else "N/D",
                     "mes_anterior": f"{fmt_num(b, decimals)}{unit}" if b is not None else "N/D",
                     "variacion": (f"{'+' if v > b else ''}{fmt_num(v - b, 1)} puntos" if points else
                                   f"{'+' if (change or 0) > 0 else ''}{fmt_num(change, 1)} %" if change is not None
                                   else "—"), "lectura": reading, "valor": v, "anterior": b, "cambio": change})
    mname, pname = MONTHS[ref.month - 1], MONTHS[p_start.month - 1]
    change = _var(cur["personas"], prev["personas"])
    trend = ("" if change is None else
             f", {fmt_num(abs(change), 1)} % {'más' if change > 0 else 'menos'} que del 1 al {p_end.day} de {pname} "
             f"({fmt_num(prev['personas'])})")
    projected = round(cur["ingresos"] / days * month_days) if days else None
    headline = (f"Del 1 al {end.day} de {mname} ({days} días) se han atendido {fmt_num(cur['personas'])} personas "
                f"en {fmt_num(cur['ingresos'])} ingresos{trend}.")
    extra = (f"Se han realizado {fmt_num(cur['cirugias'])} cirugías y la ocupación promedio de camas va en "
             f"{fmt_num(cur['ocupacion'], 1)} %. "
             + (f"A este ritmo ({fmt_num(cur['ingresos'] / days, 0)} ingresos por día) el mes cerraría con unos "
                f"{fmt_num(projected)} ingresos (proyección simple, no es un pronóstico)." if projected else ""))
    return {"inicio": start.isoformat(), "fin": end.isoformat(), "dias": days, "dias_mes": month_days,
            "mes": mname, "mes_anterior": pname, "inicio_anterior": p_start.isoformat(),
            "fin_anterior": p_end.isoformat(), "titular": headline, "detalle": extra, "proyeccion_ingresos": projected,
            "filas": rows}


def table(mtd: dict):
    import pandas as pd
    return pd.DataFrame([{"Indicador": r["indicador"], f"1-{mtd['dias']} {mtd['mes'][:3]}": r["este_mes"],
                          f"Mismos días de {mtd['mes_anterior']}": r["mes_anterior"], "Cambio": r["variacion"]}
                         for r in mtd["filas"]])
