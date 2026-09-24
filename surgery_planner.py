"""
surgery_planner.py — Programación de cirugías según la disponibilidad de quirófanos.

Qué traen los datos (y qué no): la tabla `cirugias` tiene el día y el área de cada cirugía realizada, pero el HIS
no trae salas, horas de inicio ni duración. Por eso la capacidad se mide en **cirugías por día y por área**:

  * Carga habitual   = promedio de cirugías realizadas ese día de la semana en esa área (últimas 16 semanas).
  * Capacidad probada = percentil 90 del mismo dato: lo que el área ya demostró poder hacer de forma sostenida.
  * Cupos libres      = capacidad probada − carga habitual (redondeado hacia abajo).

La lista de espera sale de las programaciones del HIS **sin evidencia de ejecución** (40 en el extracto) y de las
solicitudes nuevas que registran los médicos. El área se deduce del código CUPS del procedimiento principal
(capítulos de la clasificación: 76-84 sistema musculoesquelético → ortopedia, 42-54 digestivo → cirugía general…).

El optimizador asigna primero lo urgente (en 1 día), luego lo prioritario (en 7 días) y luego lo electivo por
antigüedad, al primer día con cupo libre en su área. Si lo urgente no cabe, lo marca como sobrecupo (hay que
habilitar un turno quirúrgico adicional o correr una electiva). No depende de Streamlit.

Hora: el HIS no trae horas de inicio, así que la hora es una SUGERENCIA que el coordinador puede cambiar: la jornada
quirúrgica de referencia (JORNADA, 07:00-19:00, ajustable) se reparte en turnos según la capacidad probada del área
ese día (6 cirugías/día → una cada 2 horas). Dos cirugías de la misma área no quedan a la misma hora.

Gerencia puede PROPONER el calendario sugerido; coordinación lo acepta (se programa) o lo rechaza con motivo.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime, timedelta

import pandas as pd

FMT = "%Y-%m-%d %H:%M:%S"
PRIORITIES = {"URGENTE": 1, "PRIORITARIA": 7}          # días máximos para operar; ELECTIVA = por antigüedad
PRIORITY_LABEL = {"URGENTE": "Urgente (≤ 24 h)", "PRIORITARIA": "Prioritaria (≤ 7 días)", "ELECTIVA": "Electiva"}
AREAS = {
    "QUIROFANOS - CIRUGIA GENERAL": "Cirugía general",
    "QUIROFANOS - TRAUMATOLOGIA Y ORTOPEDIA": "Ortopedia y traumatología",
    "QUIROFANOS - CIRUGIA GINECOBSTETRICA": "Ginecobstetricia",
    "QUIROFANOS - UROLOGIA": "Urología",
    "QUIROFANOS - CIRUGIA PLASTICA": "Cirugía plástica",
    "QUIROFANOS PEDIATRIA UMI - CIRUGIA PEDIATRICA": "Cirugía pediátrica",
    "QUIROFANOS - OTRAS ESPECIALIDADES": "Otras especialidades",
}
DOW = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
JORNADA = (7, 19)          # jornada quirúrgica de referencia (horas); ajustable por el hospital


def area_from_cups(codes: str | None, servicio: str | None = None) -> str:
    """Área quirúrgica a partir del capítulo CUPS del procedimiento principal."""
    first = str(codes or "").split(",")[0].strip()
    try:
        chapter = int(first[:2])
    except ValueError:
        return "QUIROFANOS - CIRUGIA GENERAL"
    if 76 <= chapter <= 84:
        area = "QUIROFANOS - TRAUMATOLOGIA Y ORTOPEDIA"
    elif 65 <= chapter <= 75:
        area = "QUIROFANOS - CIRUGIA GINECOBSTETRICA"
    elif 55 <= chapter <= 64:
        area = "QUIROFANOS - UROLOGIA"
    elif chapter == 85:
        area = "QUIROFANOS - CIRUGIA PLASTICA"
    elif chapter in (1, 2, 3, 4, 5) or 8 <= chapter <= 29:
        area = "QUIROFANOS - OTRAS ESPECIALIDADES"
    else:
        area = "QUIROFANOS - CIRUGIA GENERAL"   # 06-07, 30-54, 86 y demás
    if servicio == "Pediatría" and area == "QUIROFANOS - CIRUGIA GENERAL":
        area = "QUIROFANOS PEDIATRIA UMI - CIRUGIA PEDIATRICA"
    return area


# ---------------------------------------------------------------------------
# Capacidad (desde el extracto)
# ---------------------------------------------------------------------------
def capacity_profile(analytics: sqlite3.Connection, ref: date, weeks: int = 16) -> pd.DataFrame:
    """Por área y día de la semana: carga habitual (promedio), capacidad probada (p90) y cupos libres."""
    start = ref - timedelta(days=weeks * 7 - 1)
    df = pd.read_sql_query("""
        SELECT date(fecha_cirugia) AS dia, area_quirofano AS area, COUNT(*) AS n FROM cirugias
         WHERE estado = 'Realizada' AND fecha_cirugia IS NOT NULL AND date(fecha_cirugia) BETWEEN ? AND ?
         GROUP BY 1, 2""", analytics, params=(start.isoformat(), ref.isoformat()))
    if df.empty:
        return pd.DataFrame(columns=["area", "dow", "habitual", "capacidad", "libres"])
    days = pd.date_range(start, ref, freq="D")
    grid = (df.assign(dia=pd.to_datetime(df["dia"])).pivot_table(index="dia", columns="area", values="n", fill_value=0)
            .reindex(days, fill_value=0))
    rows = []
    for area in grid.columns:
        by_dow = grid[area].groupby(grid.index.dayofweek)
        for dow, s in by_dow:
            mean, p90 = float(s.mean()), float(s.quantile(0.9))
            rows.append({"area": area, "dow": int(dow), "habitual": round(mean, 1), "capacidad": round(p90, 1),
                         "libres": max(int(math.floor(p90 - mean)), 0)})
    return pd.DataFrame(rows)


def compliance(analytics: sqlite3.Connection) -> dict:
    row = analytics.execute("""SELECT SUM(estado = 'Realizada'), SUM(estado LIKE 'Programada%') FROM cirugias
                               WHERE en_periodo = 1""").fetchone()
    done, pending = int(row[0] or 0), int(row[1] or 0)
    return {"realizadas": done, "sin_ejecutar": pending,
            "cumplimiento_pct": round(done / (done + pending) * 100, 1) if done + pending else None}


def idle_insight(profile: pd.DataFrame) -> list[str]:
    """Dónde hay más capacidad ociosa (para mover electivas allí)."""
    if profile.empty:
        return []
    out = []
    for area, g in profile[profile["capacidad"] >= 1].groupby("area"):
        best = g.sort_values("libres", ascending=False).head(2)
        best = best[best["libres"] > 0]
        if not best.empty:
            days = " y ".join(f"{DOW[r.dow]} ({r.libres} cupos)" for r in best.itertuples())
            out.append(f"{AREAS.get(area, area)}: más capacidad ociosa el {days}.")
    return out


# ---------------------------------------------------------------------------
# Lista de espera (clinico.db)
# ---------------------------------------------------------------------------
def sync_waitlist(conn: sqlite3.Connection, analytics_db) -> int:
    """Trae al plan las programaciones del HIS sin evidencia de ejecución (idempotente)."""
    from pathlib import Path
    src = sqlite3.connect(f"{Path(analytics_db).resolve().as_uri()}?mode=ro", uri=True)
    rows = src.execute("""
        SELECT c.consecutivo_programacion, c.id_paciente, c.procedimientos, c.fecha_ingreso, c.servicio,
               (SELECT group_concat(codigo_servicio) FROM programacion_cirugia p
                 WHERE p.consecutivo_programacion = c.consecutivo_programacion) AS codigos
          FROM cirugias c WHERE c.estado LIKE 'Programada%'""").fetchall()
    src.close()
    n = 0
    with conn:
        for cons, pid, procs, since, service, codes in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO cirugias_solicitudes(origen, consecutivo_programacion, id_paciente, area_quirofano, "
                "procedimientos, codigos_cups, prioridad, fecha_solicitud, nota) VALUES ('HIS', ?,?,?,?,?, 'ELECTIVA', ?, ?)",
                (cons, pid, area_from_cups(codes, service), procs or 1, codes, since or "2026-09-01 00:00:00",
                 "Programada en el HIS sin evidencia de ejecución"))
            n += cur.rowcount
    return n


def request_surgery(conn: sqlite3.Connection, *, id_paciente: int, area: str, prioridad: str, procedimiento: str,
                    user_id: int, now: str, codigos: str | None = None) -> int:
    if area not in AREAS:
        raise sqlite3.IntegrityError("Área quirúrgica no válida")
    if prioridad not in PRIORITY_LABEL:
        raise sqlite3.IntegrityError("Prioridad no válida")
    if len((procedimiento or "").strip()) < 4:
        raise sqlite3.IntegrityError("Describe el procedimiento")
    with conn:
        rid = conn.execute(
            "INSERT INTO cirugias_solicitudes(origen, id_paciente, area_quirofano, procedimientos, codigos_cups, "
            "prioridad, fecha_solicitud, nota, creado_por) VALUES ('APP', ?, ?, 1, ?, ?, ?, ?, ?)",
            (id_paciente, area, (codigos or "").strip() or None, prioridad, now, procedimiento.strip(), user_id)).lastrowid
        _event(conn, id_paciente, f"Solicitud de cirugía ({PRIORITY_LABEL[prioridad].lower()}): {procedimiento.strip()}",
               user_id, now)
    return rid


def waitlist(conn: sqlite3.Connection, states: tuple[str, ...] = ("EN_ESPERA",)) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(states))
    return conn.execute(f"""
        SELECT s.*, COALESCE(NULLIF(trim(pc.nombres || ' ' || pc.apellidos), ''), 'Paciente ' || s.id_paciente) AS paciente
          FROM cirugias_solicitudes s LEFT JOIN pacientes_clinicos pc ON pc.id_paciente = s.id_paciente
         WHERE s.estado IN ({marks})
         ORDER BY CASE s.prioridad WHEN 'URGENTE' THEN 0 WHEN 'PRIORITARIA' THEN 1 ELSE 2 END, s.fecha_solicitud""",
                        states).fetchall()


# ---------------------------------------------------------------------------
# Horas
# ---------------------------------------------------------------------------
def valid_hour(hour: str | None) -> bool:
    try:
        h, m = str(hour).split(":")
        return len(h) == 2 and len(m) == 2 and 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except ValueError:
        return False


def day_slots(profile: pd.DataFrame, area: str, day: date) -> list[str]:
    """Horas de inicio sugeridas del área ese día: la jornada repartida según su capacidad probada."""
    match = profile[(profile["area"] == area) & (profile["dow"] == day.weekday())] if not profile.empty else profile
    cap = float(match["capacidad"].iat[0]) if not match.empty else 0.0
    n = max(int(math.ceil(cap)), 1)
    minutes = (JORNADA[1] - JORNADA[0]) * 60
    step = max(30, (minutes // n) // 30 * 30)
    return [f"{JORNADA[0] + (i * step) // 60:02d}:{(i * step) % 60:02d}" for i in range(n) if i * step < minutes]


def taken_hours(conn: sqlite3.Connection, day_from: str, day_to: str) -> dict[tuple[str, str], set[str]]:
    out: dict[tuple[str, str], set[str]] = {}
    for a, d, h in conn.execute("SELECT area_quirofano, fecha_programada, hora_programada FROM cirugias_solicitudes "
                                "WHERE estado = 'PROGRAMADA' AND hora_programada IS NOT NULL AND fecha_programada "
                                "BETWEEN ? AND ?", (day_from, day_to)):
        out.setdefault((a, d), set()).add(h)
    return out


def suggest_hour(profile: pd.DataFrame, area: str, day: date, taken: set[str], now: str | None = None) -> str | None:
    """Primera hora sugerida libre del área ese día (si es hoy, posterior a la hora actual)."""
    limit = now[11:16] if now and now[:10] == day.isoformat() else None
    for h in day_slots(profile, area, day):
        if h not in taken and (limit is None or h > limit):
            return h
    return None


# ---------------------------------------------------------------------------
# Optimizador
# ---------------------------------------------------------------------------
def plan(requests: list[dict], profile: pd.DataFrame, start: date, horizon_days: int = 14,
         already: dict[tuple[str, str], int] | None = None, taken: dict[tuple[str, str], set[str]] | None = None,
         now: str | None = None) -> list[dict]:
    """Propuesta: [{id, fecha, hora, area, prioridad, espera_dias, alerta}]. `already` = cupos ya usados por
    cirugías programadas en la app {(area, fecha): n}; `taken` = sus horas {(area, fecha): {"07:00", …}}."""
    free = {(r.area, r.dow): r.libres for r in profile.itertuples()}
    used = dict(already or {})
    hours = {k: set(v) for k, v in (taken or {}).items()}

    def hour_for(area: str, d: date) -> str | None:
        return suggest_hour(profile, area, d, hours.get((area, d.isoformat()), set()), now)
    days = [start + timedelta(days=i) for i in range(horizon_days)]
    order = sorted(requests, key=lambda r: ({"URGENTE": 0, "PRIORITARIA": 1}.get(r["prioridad"], 2),
                                            r["fecha_solicitud"]))
    out = []
    for r in order:
        limit = PRIORITIES.get(r["prioridad"], horizon_days)
        window = days[:limit] if r["prioridad"] in PRIORITIES else days
        chosen, alert = None, None
        for d in window:
            key = (r["area_quirofano"], d.isoformat())
            if used.get(key, 0) < free.get((r["area_quirofano"], d.weekday()), 0) and hour_for(r["area_quirofano"], d):
                chosen = d
                break
        if chosen is None and r["prioridad"] == "URGENTE":
            chosen, alert = days[0], "Sobrecupo: habilitar un turno quirúrgico adicional o correr una electiva"
        elif chosen is None and r["prioridad"] == "PRIORITARIA":
            for d in days[limit:]:
                key = (r["area_quirofano"], d.isoformat())
                if used.get(key, 0) < free.get((r["area_quirofano"], d.weekday()), 0) and \
                        hour_for(r["area_quirofano"], d):
                    chosen, alert = d, "Fuera del plazo de 7 días por falta de cupo"
                    break
        hour = None
        if chosen is not None:
            key = (r["area_quirofano"], chosen.isoformat())
            used[key] = used.get(key, 0) + 1
            hour = hour_for(r["area_quirofano"], chosen)
            if hour is None:  # sobrecupo urgente sin turno libre: se pone al final de la jornada
                hour = f"{JORNADA[1]:02d}:00"
            hours.setdefault(key, set()).add(hour)
        waited = (start - datetime.strptime(r["fecha_solicitud"][:10], "%Y-%m-%d").date()).days
        if chosen is None:
            sporadic = not any(free.get((r["area_quirofano"], d), 0) for d in range(7))
            alert = ("Área con actividad esporádica en los datos: programar a mano con el especialista"
                     if sporadic else f"Sin cupo en los próximos {horizon_days} días")
        out.append({"id": r["id"], "paciente": r.get("paciente"), "area": r["area_quirofano"],
                    "prioridad": r["prioridad"], "fecha": chosen.isoformat() if chosen else None, "hora": hour,
                    "espera_dias": max(waited, 0), "alerta": alert})
    return sorted(out, key=lambda x: (x["fecha"] or "9999", x["hora"] or "99", x["area"]))


def scheduled_counts(conn: sqlite3.Connection, day_from: str, day_to: str) -> dict[tuple[str, str], int]:
    return {(a, d): n for a, d, n in conn.execute(
        "SELECT area_quirofano, fecha_programada, COUNT(*) FROM cirugias_solicitudes WHERE estado = 'PROGRAMADA' "
        "AND fecha_programada BETWEEN ? AND ? GROUP BY 1, 2", (day_from, day_to))}


def confirm(conn: sqlite3.Connection, proposal: list[dict], user_id: int, now: str, coordinator: bool = True) -> int:
    """Aplica la programación sugerida (solo coordinación; respeta la capacidad calculada por plan())."""
    if not coordinator:
        raise sqlite3.IntegrityError("Solo coordinación de quirófanos confirma la programación")
    n = 0
    with conn:
        for p in proposal:
            if not p["fecha"]:
                continue
            cur = conn.execute("UPDATE cirugias_solicitudes SET estado = 'PROGRAMADA', fecha_programada = ?, "
                               "hora_programada = ?, actualizado_por = ?, actualizado_en = ? "
                               "WHERE id = ? AND estado = 'EN_ESPERA'",
                               (p["fecha"], p.get("hora"), user_id, now, p["id"]))
            if cur.rowcount:
                n += 1
                pid = conn.execute("SELECT id_paciente FROM cirugias_solicitudes WHERE id = ?", (p["id"],)).fetchone()[0]
                _event(conn, pid, f"Cirugía programada para el {p['fecha']}"
                       + (f" a las {p['hora']}" if p.get("hora") else "") + f" ({AREAS.get(p['area'], p['area'])})",
                       user_id, now)
    return n


CANCEL_REASONS = ("Paciente no apto (valoración preanestésica)", "Paciente desiste", "Sin cama postoperatoria",
                  "Falta de insumos o equipos", "Solicitud duplicada", "Resuelto sin cirugía", "Otro")
MAX_DAYS_AHEAD = 90


def _get(conn, request_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM cirugias_solicitudes WHERE id = ?", (request_id,)).fetchone()
    if row is None:
        raise sqlite3.IntegrityError("Solicitud inexistente")
    return row


def free_on(conn: sqlite3.Connection, profile: pd.DataFrame, area: str, day: str) -> tuple[int, int]:
    """(cupos libres de capacidad probada ese día, ya programadas en la app ese día) para el área."""
    d = date.fromisoformat(day)
    match = profile[(profile["area"] == area) & (profile["dow"] == d.weekday())]
    free = int(match["libres"].iat[0]) if not match.empty else 0
    used = conn.execute("SELECT COUNT(*) FROM cirugias_solicitudes WHERE estado = 'PROGRAMADA' AND area_quirofano = ? "
                        "AND fecha_programada = ?", (area, day)).fetchone()[0]
    return free, used


def schedule(conn: sqlite3.Connection, request_id: int, day: str, user_id: int, now: str, profile: pd.DataFrame,
             *, coordinator: bool, justification: str = "", hour: str | None = None) -> None:
    """Agendar una cirugía en un día y una hora. Reglas:
    - solo coordinación de quirófanos agenda; la solicitud debe estar en espera;
    - no en el pasado (ni una hora que ya pasó) ni a más de 90 días; el paciente no puede tener otra cirugía
      programada ese día; el área no puede tener otra cirugía a la misma hora;
    - una urgente va en máximo 1 día, salvo justificación;
    - si el área ya no tiene cupo ese día (capacidad probada), es SOBRECUPO: exige justificación y queda registrada."""
    if not coordinator:
        raise sqlite3.IntegrityError("Solo coordinación de quirófanos puede agendar cirugías")
    r = _get(conn, request_id)
    if r["estado"] != "EN_ESPERA":
        raise sqlite3.IntegrityError("Solo se agendan solicitudes en espera")
    today = now[:10]
    if day < today:
        raise sqlite3.IntegrityError("La fecha no puede ser anterior a hoy")
    if (date.fromisoformat(day) - date.fromisoformat(today)).days > MAX_DAYS_AHEAD:
        raise sqlite3.IntegrityError(f"No se agenda a más de {MAX_DAYS_AHEAD} días")
    if conn.execute("SELECT 1 FROM cirugias_solicitudes WHERE id_paciente = ? AND estado = 'PROGRAMADA' "
                    "AND fecha_programada = ? AND id <> ?", (r["id_paciente"], day, request_id)).fetchone():
        raise sqlite3.IntegrityError("El paciente ya tiene otra cirugía programada ese día")
    if not valid_hour(hour):
        raise sqlite3.IntegrityError("Indica la hora de la cirugía (HH:MM)")
    if day == today and hour <= now[11:16]:
        raise sqlite3.IntegrityError("Esa hora ya pasó; elige una hora posterior")
    if conn.execute("SELECT 1 FROM cirugias_solicitudes WHERE area_quirofano = ? AND estado = 'PROGRAMADA' "
                    "AND fecha_programada = ? AND hora_programada = ? AND id <> ?",
                    (r["area_quirofano"], day, hour, request_id)).fetchone():
        raise sqlite3.IntegrityError(f"Ya hay una cirugía de {AREAS.get(r['area_quirofano'])} a las {hour} ese día")
    just = (justification or "").strip()
    reasons = []
    if r["prioridad"] == "URGENTE" and (date.fromisoformat(day) - date.fromisoformat(today)).days > 1:
        reasons.append("una urgente se opera en máximo 1 día")
    free, used = free_on(conn, profile, r["area_quirofano"], day)
    overbook = used >= free
    if overbook:
        reasons.append(f"el área no tiene cupo ese día ({used} programadas de {free} cupos)")
    if reasons and len(just) < 15:
        raise sqlite3.IntegrityError("Requiere justificación (mínimo 15 caracteres): " + "; ".join(reasons))
    with conn:
        conn.execute("UPDATE cirugias_solicitudes SET estado = 'PROGRAMADA', fecha_programada = ?, hora_programada = ?, "
                     "sobrecupo_justificacion = ?, actualizado_por = ?, actualizado_en = ? WHERE id = ?",
                     (day, hour, just if reasons else None, user_id, now, request_id))
        _event(conn, r["id_paciente"], f"Cirugía programada para el {day} a las {hour} "
               f"({AREAS.get(r['area_quirofano'])})"
               + (f" · SOBRECUPO: {just}" if overbook else ""), user_id, now)


def cancel(conn: sqlite3.Connection, request_id: int, user_id: int, now: str, *, category: str, detail: str,
           coordinator: bool) -> None:
    """Quitar una cirugía. Reglas:
    - no se borra: queda CANCELADA con categoría y detalle (auditable);
    - una realizada o ya cancelada no se toca;
    - un médico solo cancela SUS solicitudes que siguen en espera; lo demás lo hace coordinación;
    - una cirugía programada para hoy o mañana solo la cancela coordinación y con detalle (≥ 15 caracteres)."""
    r = _get(conn, request_id)
    if r["estado"] not in ("EN_ESPERA", "PROGRAMADA"):
        raise sqlite3.IntegrityError("Una cirugía realizada o ya cancelada no se puede cancelar")
    if category not in CANCEL_REASONS:
        raise sqlite3.IntegrityError("Elige la causa de la cancelación")
    detail = (detail or "").strip()
    if not coordinator:
        if r["estado"] != "EN_ESPERA" or r["creado_por"] != user_id:
            raise sqlite3.IntegrityError("Solo puedes cancelar tus propias solicitudes que siguen en espera; "
                                         "lo demás lo cancela coordinación de quirófanos")
    soon = r["estado"] == "PROGRAMADA" and r["fecha_programada"] <= (date.fromisoformat(now[:10]) +
                                                                     timedelta(days=1)).isoformat()
    if (soon or category == "Otro") and len(detail) < 15:
        raise sqlite3.IntegrityError("Explica el motivo (mínimo 15 caracteres): la cirugía es para hoy/mañana "
                                     "o la causa es “Otro”")
    with conn:
        conn.execute("UPDATE cirugias_solicitudes SET estado = 'CANCELADA', motivo_categoria = ?, motivo = ?, "
                     "actualizado_por = ?, actualizado_en = ? WHERE id = ?",
                     (category, detail or category, user_id, now, request_id))
        _event(conn, r["id_paciente"], f"Cirugía cancelada: {category}" + (f" · {detail}" if detail else ""),
               user_id, now)


def reprogram(conn: sqlite3.Connection, request_id: int, user_id: int, now: str, motivo: str, *,
              coordinator: bool) -> None:
    """Devuelve una programada a la lista de espera (coordinación, con motivo)."""
    if not coordinator:
        raise sqlite3.IntegrityError("Solo coordinación de quirófanos reprograma")
    r = _get(conn, request_id)
    if r["estado"] != "PROGRAMADA":
        raise sqlite3.IntegrityError("Solo se reprograma una cirugía programada")
    if len((motivo or "").strip()) < 10:
        raise sqlite3.IntegrityError("Indica el motivo (mínimo 10 caracteres)")
    with conn:
        conn.execute("UPDATE cirugias_solicitudes SET estado = 'EN_ESPERA', fecha_programada = NULL, "
                     "hora_programada = NULL, sobrecupo_justificacion = NULL, motivo = ?, actualizado_por = ?, actualizado_en = ? WHERE id = ?",
                     (motivo.strip(), user_id, now, request_id))
        _event(conn, r["id_paciente"], f"Cirugía reprogramada (vuelve a lista de espera): {motivo.strip()}", user_id, now)


def mark_done(conn: sqlite3.Connection, request_id: int, user_id: int, now: str, *, coordinator: bool) -> None:
    if not coordinator:
        raise sqlite3.IntegrityError("Solo coordinación de quirófanos cierra cirugías")
    r = _get(conn, request_id)
    if r["estado"] != "PROGRAMADA":
        raise sqlite3.IntegrityError("Solo se marca como realizada una cirugía programada")
    if r["fecha_programada"] > now[:10]:
        raise sqlite3.IntegrityError("No se puede marcar como realizada una cirugía de una fecha futura")
    with conn:
        conn.execute("UPDATE cirugias_solicitudes SET estado = 'REALIZADA', actualizado_por = ?, actualizado_en = ? "
                     "WHERE id = ?", (user_id, now, request_id))
        _event(conn, r["id_paciente"], "Cirugía realizada", user_id, now)


# ---------------------------------------------------------------------------
# Propuesta de gerencia → coordinación acepta o rechaza
# ---------------------------------------------------------------------------
def propose(conn: sqlite3.Connection, proposal: list[dict], user_id: int, now: str, *, can_propose: bool) -> int:
    """Gerencia envía el calendario sugerido. Una propuesta nueva reemplaza la pendiente anterior."""
    if not can_propose:
        raise sqlite3.IntegrityError("Solo gerencia propone calendarios quirúrgicos")
    items = [p for p in proposal if p.get("fecha") and p.get("hora")]
    if not items:
        raise sqlite3.IntegrityError("La propuesta no tiene cirugías con fecha")
    with conn:
        conn.execute("UPDATE cirugias_propuestas SET estado = 'REEMPLAZADA', respondida_en = ? WHERE estado = 'PENDIENTE'",
                     (now,))
        pid = conn.execute("INSERT INTO cirugias_propuestas(creada_por, creada_en) VALUES (?, ?)",
                           (user_id, now)).lastrowid
        conn.executemany("INSERT INTO cirugias_propuesta_items(propuesta_id, solicitud_id, fecha, hora) VALUES (?,?,?,?)",
                         [(pid, p["id"], p["fecha"], p["hora"]) for p in items])
    return pid


def pending_proposal(conn: sqlite3.Connection) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
    head = conn.execute("SELECT p.*, u.nombre_mostrado AS autor FROM cirugias_propuestas p "
                        "JOIN usuarios u ON u.id = p.creada_por WHERE p.estado = 'PENDIENTE' "
                        "ORDER BY p.id DESC LIMIT 1").fetchone()
    if head is None:
        return None, []
    items = conn.execute("""
        SELECT i.*, s.area_quirofano, s.prioridad, s.estado, s.id_paciente,
               COALESCE(NULLIF(trim(pc.nombres || ' ' || pc.apellidos), ''), 'Paciente ' || s.id_paciente) AS paciente
          FROM cirugias_propuesta_items i JOIN cirugias_solicitudes s ON s.id = i.solicitud_id
          LEFT JOIN pacientes_clinicos pc ON pc.id_paciente = s.id_paciente
         WHERE i.propuesta_id = ? ORDER BY i.fecha, i.hora""", (head["id"],)).fetchall()
    return head, items


def last_answered(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT p.*, u.nombre_mostrado AS quien FROM cirugias_propuestas p "
                        "LEFT JOIN usuarios u ON u.id = p.respondida_por "
                        "WHERE p.estado IN ('ACEPTADA','RECHAZADA') ORDER BY p.id DESC LIMIT 1").fetchone()


def accept_proposal(conn: sqlite3.Connection, proposal_id: int, user_id: int, now: str, profile: pd.DataFrame,
                    *, coordinator: bool) -> tuple[int, list[str]]:
    """Coordinación acepta: programa cada cirugía con las mismas reglas de schedule(). Devuelve (programadas,
    motivos de las que no se pudieron programar: ya no estaban en espera, fecha pasada, choque de hora…)."""
    if not coordinator:
        raise sqlite3.IntegrityError("Solo coordinación de quirófanos acepta el calendario")
    head = conn.execute("SELECT * FROM cirugias_propuestas WHERE id = ?", (proposal_id,)).fetchone()
    if head is None or head["estado"] != "PENDIENTE":
        raise sqlite3.IntegrityError("La propuesta ya no está pendiente")
    items = conn.execute("SELECT * FROM cirugias_propuesta_items WHERE propuesta_id = ? ORDER BY fecha, hora",
                         (proposal_id,)).fetchall()
    done, skipped = 0, []
    for it in items:
        try:
            schedule(conn, it["solicitud_id"], it["fecha"], user_id, now, profile, coordinator=True,
                     hour=it["hora"], justification=f"Calendario propuesto por gerencia (propuesta #{proposal_id})")
            done += 1
        except sqlite3.IntegrityError as exc:
            skipped.append(f"Solicitud {it['solicitud_id']}: {exc}")
    with conn:
        conn.execute("UPDATE cirugias_propuestas SET estado = 'ACEPTADA', respondida_por = ?, respondida_en = ?, "
                     "aplicadas = ? WHERE id = ?", (user_id, now, done, proposal_id))
    return done, skipped


def reject_proposal(conn: sqlite3.Connection, proposal_id: int, user_id: int, now: str, motivo: str, *,
                    coordinator: bool) -> None:
    if not coordinator:
        raise sqlite3.IntegrityError("Solo coordinación de quirófanos responde la propuesta")
    if len((motivo or "").strip()) < 10:
        raise sqlite3.IntegrityError("Explica por qué la rechazas (mínimo 10 caracteres)")
    with conn:
        cur = conn.execute("UPDATE cirugias_propuestas SET estado = 'RECHAZADA', respondida_por = ?, respondida_en = ?, "
                           "motivo = ? WHERE id = ? AND estado = 'PENDIENTE'", (user_id, now, motivo.strip(), proposal_id))
    if not cur.rowcount:
        raise sqlite3.IntegrityError("La propuesta ya no está pendiente")


def _event(conn, id_paciente: int, text: str, by: int | None, now: str) -> None:
    hc = conn.execute("SELECT id FROM historias_clinicas WHERE id_paciente = ?", (id_paciente,)).fetchone()
    if hc:
        conn.execute("INSERT INTO historia_clinica_eventos(historia_id, tipo, descripcion, autor_id, fecha) "
                     "VALUES (?, 'CIRUGIA', ?, ?, ?)", (hc[0], text, by, now))


