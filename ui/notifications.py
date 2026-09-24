"""
ui/notifications.py — Centro de notificaciones (campana con contador) filtrado por rol.

`collect()` es lógica pura (sin Streamlit, probada en tests/test_operacion.py): arma las notificaciones del
usuario a partir de las bases clínica y analítica y de la hora del reloj clínico. `render_bell()` las dibuja.

Cada notificación tiene un id estable: marcarla como leída la oculta del contador hasta que la situación
cambie (p. ej. una nueva fórmula por vencer genera un id nuevo). Nada se inventa: todo sale de datos del HIS,
del módulo clínico o del motor de recomendaciones.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

import config
import database as db

FMT = "%Y-%m-%d %H:%M:%S"
ORDER = {"crítica": 0, "alta": 1, "media": 2, "info": 3}
DOT = {"crítica": "#DC2626", "alta": "#EA580C", "media": "#CA8A04", "info": "#2E3378"}


@dataclass
class Notification:
    id: str
    severity: str        # crítica | alta | media | info
    title: str
    detail: str
    slug: str | None     # página a la que lleva
    link_label: str = "Ver"


def _n(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _dt(ts: str) -> datetime:
    return datetime.strptime(ts, FMT)


# ---------------------------------------------------------------------------
# Fuentes por rol
# ---------------------------------------------------------------------------
def _pharmacy_live(clin: sqlite3.Connection) -> Notification | None:
    """Farmacia en vivo desde el libro de inventario de clinico.db: cambia al recibir pedidos o entregar fórmulas."""
    red = clin.execute("SELECT COUNT(*) FROM v_semaforo_stock WHERE semaforo = 'ROJO'").fetchone()[0]
    if not red:
        return None
    return Notification(f"farmacia:{red}", "crítica", _n(red, "ítem se agota en menos de 5 días",
                                                           "ítems se agotan en menos de 5 días"),
                        "Revisar la orden de compra sugerida y registrar la llegada de los pedidos.", "alertas",
                        "Ver qué pedir")


def _hospital(alerts, categories: set[str], slug_for: dict[str, str], clin=None) -> list[Notification]:
    """Alertas críticas y altas del motor de recomendaciones (mismo texto que en Alertas y acciones).
    Farmacia se toma del inventario vivo (clinico.db) para que refleje pedidos y entregas."""
    out = []
    for a in alerts:
        if a.severity in ("crítica", "alta") and a.category in categories:
            if a.category == "Farmacia" and clin is not None:
                continue
            out.append(Notification(f"alerta:{a.category}:{a.title}", a.severity, a.title, a.action,
                                    slug_for.get(a.category, "alertas"),
                                    "Ver camas" if a.category == "Ocupación" else "Ver acción"))
    if "Farmacia" in categories and clin is not None:
        live = _pharmacy_live(clin)
        if live:
            out.insert(0, live)
    return out


def _triage(analytics: sqlite3.Connection, now: str) -> list[Notification]:
    q = db.triage_queue(analytics, now)
    if q.empty:
        return []
    out = []
    t1 = q[q["nivel_triage"] == 1]
    if not t1.empty:
        out.append(Notification(f"triage1:{now[:13]}:{len(t1)}", "crítica",
                                _n(len(t1), "paciente Triage I esperando atención", "pacientes Triage I esperando atención"),
                                "Triage I requiere atención inmediata (Res. 5596 de 2015).", "hoy", "Ver cola"))
    late = q[(q["nivel_triage"] == 2) & (q["fuera_de_meta"] == 1)]
    if not late.empty:
        out.append(Notification(f"triage2:{now[:13]}:{len(late)}", "alta",
                                _n(len(late), "paciente Triage II fuera de meta", "pacientes Triage II fuera de meta"),
                                f"Esperan más de {config.WAIT_TARGET_TRIAGE2_MIN:.0f} min; el mayor lleva "
                                f"{int(late['espera_min'].max())} min.", "hoy", "Ver cola"))
    return out


def _doctor(clin: sqlite3.Connection, user_id: int) -> list[Notification]:
    out = []
    for r in clin.execute("""
            SELECT c.id, c.id_paciente, c.fecha_hora, f.nombre AS producto
              FROM citas c LEFT JOIN prescripciones p ON p.id = c.prescripcion_origen_id
              LEFT JOIN productos_farmacia f ON f.codigo = p.codigo_producto
             WHERE c.motivo = 'REEVALUACION_FORMULA' AND c.estado = 'PROGRAMADA'"""):
        out.append(Notification(f"reeval:{r['id']}", "alta", f"Paciente {r['id_paciente']} pidió reevaluación",
                                f"Fórmula caducada de {str(r['producto'] or '').capitalize()[:45]} · cita "
                                f"{_dt(r['fecha_hora']):%d/%m %H:%M}", "clinico", "Atender"))
    for r in clin.execute("""
            SELECT p.id, p.id_paciente, f.nombre AS producto, f.critico_continuidad
              FROM prescripciones p JOIN productos_farmacia f ON f.codigo = p.codigo_producto
             WHERE p.medico_id = ? AND p.estado = 'CADUCADA' AND p.requiere_reevaluacion = 1""", (user_id,)):
        crit = bool(r["critico_continuidad"])
        out.append(Notification(
            f"caducada:{r['id']}", "crítica" if crit else "media",
            f"Fórmula no reclamada · paciente {r['id_paciente']}",
            f"{str(r['producto']).capitalize()[:45]}" + (" · continuidad crítica: búsqueda activa del paciente"
                                                          if crit else " · volvió a stock"), "clinico", "Ver historia"))
    return out


def _nurse(clin: sqlite3.Connection, now: str) -> list[Notification]:
    out, t = [], _dt(now)
    rows = clin.execute("""
        SELECT p.id, p.id_paciente, p.ambito, p.fecha_limite_reclamo, f.nombre AS producto
          FROM prescripciones p JOIN productos_farmacia f ON f.codigo = p.codigo_producto
         WHERE p.estado IN ('VIGENTE','PARCIAL')""").fetchall()
    expired = [r for r in rows if r["ambito"] == "AMBULATORIA" and _dt(r["fecha_limite_reclamo"]) < t]
    if expired:
        out.append(Notification(f"vencidas:{','.join(str(r['id']) for r in expired)}", "alta",
                                _n(len(expired), "fórmula vencida sin procesar", "fórmulas vencidas sin procesar"),
                                "Ejecutar el retorno a stock (Simular / job de caducidad).", "clinico", "Dispensación"))
    for r in rows:
        if r["ambito"] != "AMBULATORIA":
            continue
        left = (_dt(r["fecha_limite_reclamo"]) - t).total_seconds() / 3600
        if 0 <= left < 24:
            out.append(Notification(f"porvencer:{r['id']}", "alta", f"Fórmula por vencer · paciente {r['id_paciente']}",
                                    f"{str(r['producto']).capitalize()[:45]} · quedan {left:.0f} h para reclamar",
                                    "clinico", "Entregar"))
    hosp = [r for r in rows if r["ambito"] == "HOSPITALARIA"]
    if hosp:
        out.append(Notification(f"hosp:{','.join(str(r['id']) for r in hosp)}", "info",
                                _n(len(hosp), "orden hospitalaria con dosis pendientes", "órdenes hospitalarias con dosis pendientes"),
                                "Administración y dispensación en piso.", "clinico", "Ver órdenes"))
    return out


def _admin_audit(clin: sqlite3.Connection) -> list[Notification]:
    rows = clin.execute("SELECT id, fecha, id_paciente FROM auditoria_accesos WHERE acceso_emergencia = 1 "
                        "ORDER BY id DESC").fetchall()
    if not rows:
        return []
    return [Notification(f"breakglass:{rows[0]['id']}", "alta", _n(len(rows), "acceso de emergencia registrado", "accesos de emergencia registrados"),
                         f"Último: paciente {rows[0]['id_paciente']} · {_dt(rows[0]['fecha']):%d/%m %H:%M}. "
                         "Revisar la justificación.", "datos", "Bitácora")]


def _patient(clin: sqlite3.Connection, id_paciente: int, now: str) -> list[Notification]:
    out, t = [], _dt(now)
    for r in clin.execute("""
            SELECT p.id, p.estado, p.ambito, p.fecha_limite_reclamo, p.requiere_reevaluacion, f.nombre AS producto
              FROM prescripciones p JOIN productos_farmacia f ON f.codigo = p.codigo_producto
             WHERE p.id_paciente = ?""", (id_paciente,)):
        name = str(r["producto"]).capitalize()[:45]
        if r["estado"] in ("VIGENTE", "PARCIAL") and r["ambito"] == "AMBULATORIA":
            left = (_dt(r["fecha_limite_reclamo"]) - t).total_seconds() / 3600
            if left >= 0:  # apartado para el paciente hasta la fecha límite (30 días)
                out.append(Notification(f"pac_vence:{r['id']}",
                                        "alta" if left < 24 else "media" if left < 72 else "info",
                                        "Reclama tu medicamento", f"{name} · está apartado para ti hasta el "
                                        f"{_dt(r['fecha_limite_reclamo']):%d/%m a las %H:%M}", "portal", "Ver fórmula"))
        elif r["estado"] == "PENDIENTE_STOCK":
            eta = clin.execute("SELECT MIN(fecha_estimada_llegada) FROM pedidos_compra WHERE estado = 'EN_CAMINO' "
                               "AND codigo_producto = (SELECT codigo_producto FROM prescripciones WHERE id = ?)",
                               (r["id"],)).fetchone()[0]
            out.append(Notification(f"pac_espera:{r['id']}:{eta}", "info", "Tu medicamento está en camino",
                                    f"{name} · hoy no hay existencias"
                                    + (f"; llegada estimada {_dt(eta + ' 00:00:00'):%d/%m}" if eta else "")
                                    + ". Al llegar queda apartado para ti 30 días.", "portal", "Ver fórmula"))
        elif r["estado"] == "CADUCADA" and r["requiere_reevaluacion"]:
            has_appt = clin.execute("SELECT 1 FROM citas WHERE prescripcion_origen_id = ? AND estado = 'PROGRAMADA'",
                                    (r["id"],)).fetchone()
            if not has_appt:
                out.append(Notification(f"pac_caduca:{r['id']}", "alta", "Tu fórmula venció",
                                        f"{name} · solicita una cita de reevaluación para renovarla", "portal",
                                        "Pedir cita"))
    for c in clin.execute("SELECT id, fecha_hora, especialidad FROM citas WHERE id_paciente = ? "
                          "AND estado = 'PROGRAMADA'", (id_paciente,)):
        days = (_dt(c["fecha_hora"]) - t).total_seconds() / 86400
        if 0 <= days <= 7:
            out.append(Notification(f"pac_cita:{c['id']}", "info", "Tienes una cita próxima",
                                    f"{str(c['especialidad']).title()} · {_dt(c['fecha_hora']):%d/%m a las %H:%M}",
                                    "portal", "Ver cita"))
    return out


def _backorders(clin: sqlite3.Connection) -> list[Notification]:
    rows = clin.execute("""
        SELECT p.codigo_producto, f.nombre, COUNT(*) AS n,
               (SELECT COUNT(*) FROM pedidos_compra o WHERE o.codigo_producto = p.codigo_producto
                   AND o.estado = 'EN_CAMINO') AS pedidos
          FROM prescripciones p JOIN productos_farmacia f ON f.codigo = p.codigo_producto
         WHERE p.estado = 'PENDIENTE_STOCK' GROUP BY p.codigo_producto""").fetchall()
    out = []
    for r in rows:
        no_order = not r["pedidos"]
        out.append(Notification(f"espera:{r['codigo_producto']}:{r['n']}:{r['pedidos']}", "alta" if no_order else "media",
                                _n(r["n"], "paciente espera", "pacientes esperan") + f" {str(r['nombre']).capitalize()[:40]}",
                                "Sin pedido registrado: pídelo para darle fecha al paciente." if no_order
                                else "Pedido en camino: se apartará al registrar la llegada.", "inventario",
                                "Pedidos"))
    return out


def _surgery(clin: sqlite3.Connection, now: str) -> list[Notification]:
    out = []
    urgent = clin.execute("SELECT COUNT(*) FROM cirugias_solicitudes WHERE estado = 'EN_ESPERA' AND prioridad = 'URGENTE'"
                          ).fetchone()[0]
    if urgent:
        out.append(Notification(f"qx_urg:{urgent}", "crítica", _n(urgent, "cirugía urgente sin programar",
                                                                    "cirugías urgentes sin programar"),
                                "Deben operarse en máximo 24 horas.", "quirofanos", "Programar"))
    today = clin.execute("SELECT COUNT(*) FROM cirugias_solicitudes WHERE estado = 'PROGRAMADA' AND fecha_programada = ?",
                         (now[:10],)).fetchone()[0]
    if today:
        out.append(Notification(f"qx_hoy:{now[:10]}:{today}", "info", _n(today, "cirugía programada hoy",
                                                                          "cirugías programadas hoy"),
                                "Márcalas como realizadas al terminar.", "quirofanos", "Ver"))
    return out


def _surgery_urgent(clin: sqlite3.Connection, now: str, creado_por: int | None = None) -> list[Notification]:
    """Cirugías urgentes ya programadas, con su hora (hoy y mañana)."""
    extra, args = ("AND s.creado_por = ?", [creado_por]) if creado_por else ("", [])
    rows = clin.execute(f"""
        SELECT s.id, s.fecha_programada, s.hora_programada, s.area_quirofano,
               COALESCE(NULLIF(trim(pc.nombres || ' ' || pc.apellidos), ''), 'Paciente ' || s.id_paciente) AS paciente
          FROM cirugias_solicitudes s LEFT JOIN pacientes_clinicos pc ON pc.id_paciente = s.id_paciente
         WHERE s.estado = 'PROGRAMADA' AND s.prioridad = 'URGENTE' AND s.fecha_programada BETWEEN ? AND date(?, '+1 day')
               {extra} ORDER BY s.fecha_programada, s.hora_programada""", [now[:10], now[:10], *args]).fetchall()
    out = []
    for r in rows:
        day = "hoy" if r["fecha_programada"] == now[:10] else "mañana"
        out.append(Notification(f"qx_urg_prog:{r['id']}:{r['fecha_programada']}:{r['hora_programada']}", "crítica",
                                f"Cirugía urgente {day} a las {r['hora_programada'] or 'hora por definir'}",
                                f"{r['paciente']} · {r['area_quirofano'].split(' - ')[-1].title()}", "quirofanos",
                                "Ver"))
    return out


def _proposal(clin: sqlite3.Connection, for_coordinator: bool) -> list[Notification]:
    if for_coordinator:
        row = clin.execute("SELECT p.id, p.creada_en, (SELECT COUNT(*) FROM cirugias_propuesta_items i "
                           "WHERE i.propuesta_id = p.id) AS n FROM cirugias_propuestas p WHERE p.estado = 'PENDIENTE' "
                           "ORDER BY p.id DESC LIMIT 1").fetchone()
        if row is None:
            return []
        return [Notification(f"qx_prop:{row['id']}", "alta", "Gerencia propuso un calendario quirúrgico",
                             f"{_n(row['n'], 'cirugía', 'cirugías')} · acéptalo o recházalo con un motivo",
                             "quirofanos", "Revisar")]
    row = clin.execute("SELECT id, estado, aplicadas, motivo FROM cirugias_propuestas WHERE estado IN "
                       "('ACEPTADA','RECHAZADA') ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return []
    detail = (f"{row['aplicadas']} cirugías programadas" if row["estado"] == "ACEPTADA"
              else f"Motivo: {str(row['motivo'])[:80]}")
    return [Notification(f"qx_prop_resp:{row['id']}:{row['estado']}", "info" if row["estado"] == "ACEPTADA" else "media",
                         f"Coordinación {'aceptó' if row['estado'] == 'ACEPTADA' else 'rechazó'} tu propuesta "
                         f"#{row['id']}", detail, "quirofanos", "Ver")]


def _registrations(clin: sqlite3.Connection) -> list[Notification]:
    n = clin.execute("SELECT COUNT(*) FROM solicitudes_registro WHERE estado = 'PENDIENTE'").fetchone()[0]
    if not n:
        return []
    return [Notification(f"registro:{n}", "media", _n(n, "persona pide registrarse", "personas piden registrarse"),
                         "Cítalas para que vayan al hospital con su documento.", "personal", "Atender")]


def _new_messages(clin: sqlite3.Connection, lado: str, id_paciente: int | None = None) -> list[Notification]:
    """Mensajes sin leer en las solicitudes de cita (lado = quien los recibe)."""
    other = "FACTURACION" if lado == "PACIENTE" else "PACIENTE"
    extra, args = ("AND s.id_paciente = ?", [id_paciente]) if id_paciente else ("", [])
    row = clin.execute(f"SELECT COUNT(*), MAX(m.id) FROM solicitudes_cita_mensajes m JOIN solicitudes_cita s "
                       f"ON s.id = m.solicitud_id WHERE m.lado = ? AND m.leido = 0 {extra}", [other, *args]).fetchone()
    if not row[0]:
        return []
    if lado == "PACIENTE":
        return [Notification(f"msg_pac:{row[1]}", "alta", "Facturación te escribió",
                             _n(row[0], "mensaje nuevo", "mensajes nuevos") + " sobre tu solicitud de cita", "portal",
                             "Leer")]
    return [Notification(f"msg_fac:{row[1]}", "alta", _n(row[0], "mensaje nuevo de pacientes", "mensajes nuevos de pacientes"),
                         "En las solicitudes de cita.", "atencion", "Leer")]


def _billing(clin: sqlite3.Connection, now: str) -> list[Notification]:
    rows = clin.execute("SELECT id, creada_en FROM solicitudes_cita WHERE estado = 'PENDIENTE' ORDER BY creada_en"
                        ).fetchall()
    if not rows:
        return _new_messages(clin, "FACTURACION")
    oldest_h = (_dt(now) - _dt(rows[0]["creada_en"])).total_seconds() / 3600
    return [Notification(f"solcita:{rows[-1]['id']}:{len(rows)}", "alta" if oldest_h >= 24 else "media",
                         _n(len(rows), "solicitud de cita por atender", "solicitudes de cita por atender"),
                         f"La más antigua espera hace {int(oldest_h)} h. Agenda y avisa por WhatsApp.", "atencion",
                         "Atender")] + _new_messages(clin, "FACTURACION")


def _patient_requests(clin: sqlite3.Connection, id_paciente: int) -> list[Notification]:
    out = []
    for r in clin.execute("SELECT s.id, s.estado, s.respuesta, c.fecha_hora FROM solicitudes_cita s "
                          "LEFT JOIN citas c ON c.id = s.cita_id WHERE s.id_paciente = ? "
                          "AND s.estado IN ('AGENDADA','CERRADA') ORDER BY s.id DESC LIMIT 3", (id_paciente,)):
        if r["estado"] == "AGENDADA":
            out.append(Notification(f"pac_sol:{r['id']}:A", "alta", "Te agendaron la cita que pediste",
                                    f"{_dt(r['fecha_hora']):%d/%m a las %H:%M}", "portal", "Ver cita"))
        else:
            out.append(Notification(f"pac_sol:{r['id']}:C", "alta", "Facturación respondió tu solicitud",
                                    str(r["respuesta"])[:120], "portal", "Ver"))
    return out


def collect(role: str, user: dict, clin: sqlite3.Connection, analytics: sqlite3.Connection, now: str,
            alerts: list) -> list[Notification]:
    """Notificaciones del usuario, ordenadas por severidad. Cada rol ve solo lo que puede atender."""
    to_beds = {"Ocupación": "camas", "Farmacia": "alertas"}
    if role == "ADMIN":
        items = (_hospital(alerts, {"Ocupación", "Farmacia", "Urgencias", "Demanda", "Cirugías"}, to_beds, clin)
                 + _triage(analytics, now) + _admin_audit(clin) + _backorders(clin) + _registrations(clin)
                 + _proposal(clin, for_coordinator=False) + _surgery_urgent(clin, now))
    elif role == "DOCTOR":
        items = (_doctor(clin, user["id"]) + _triage(analytics, now) + _hospital(alerts, {"Ocupación"}, to_beds)
                 + _surgery_urgent(clin, now, creado_por=user["id"]))
    elif role == "ENFERMERIA":
        items = _nurse(clin, now) + _triage(analytics, now) + _hospital(alerts, {"Ocupación", "Farmacia"}, to_beds, clin)
    elif role == "QUIROFANOS":
        items = _surgery(clin, now) + _surgery_urgent(clin, now) + _proposal(clin, for_coordinator=True)
    elif role == "FACTURACION":
        items = _billing(clin, now)
    elif role == "PACIENTE":
        items = (_patient(clin, user["id_paciente"], now) + _patient_requests(clin, user["id_paciente"])
                 + _new_messages(clin, "PACIENTE", user["id_paciente"]))
    else:
        items = []
    return sorted(items, key=lambda n: ORDER.get(n.severity, 9))


# ---------------------------------------------------------------------------
# Interfaz
# ---------------------------------------------------------------------------
def render_bell(container) -> None:
    """Campana con contador de no leídas. Las críticas nuevas además avisan con un toast una sola vez."""
    import streamlit as st

    from ui import context as ctx
    from ui.theme import esc

    user = ctx.current_user()
    alerts = ctx.alerts() if user["rol"] != "PACIENTE" else []
    items = collect(user["rol"], user, ctx.get_clin(), ctx.get_conn(), ctx.clock(), alerts)
    read = st.session_state.setdefault("notif_read", {}).setdefault(user["id"], set())
    unread = [n for n in items if n.id not in read]
    toasted = st.session_state.setdefault("notif_toasted", set())
    for n in [n for n in unread if n.severity == "crítica" and (user["id"], n.id) not in toasted][:2]:
        st.toast(n.title, icon=":material/warning:")
        toasted.add((user["id"], n.id))

    label = f":material/notifications: {len(unread)}" if unread else ":material/notifications:"
    with container.popover(label, width="stretch", help="Notificaciones"):
        head, action = st.columns([1.6, 1], vertical_alignment="center")
        head.markdown(f"**Notificaciones** · {len(unread)} sin leer")
        if unread and action.button("Marcar leídas", key="notif_read_all", width="stretch"):
            read.update(n.id for n in unread)
            st.rerun()
        if not items:
            st.caption("No tienes notificaciones. Todo al día.")
        for i, n in enumerate(items[:12]):
            faded = "opacity:.55;" if n.id in read else ""
            st.markdown(f'<div style="{faded}border-top:1px solid #E5E7EB;padding:0.45rem 0 0.2rem;">'
                        f'<span style="color:{DOT.get(n.severity, "#9CA3AF")}">●</span> <b>{esc(n.title)}</b><br>'
                        f'<span class="muted">{esc(n.detail)}</span></div>', unsafe_allow_html=True)
            if n.slug and n.slug in ctx.PAGES:
                st.page_link(ctx.PAGES[n.slug], label=f"{n.link_label} →")
        if len(items) > 12:
            st.caption(f"Y {len(items) - 12} más en Alertas y acciones.")
