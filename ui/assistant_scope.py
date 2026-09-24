"""
ui/assistant_scope.py — Qué puede responder el asistente según el rol (lógica pura, sin Streamlit).

El alcance se decide por PERMISOS del RBAC, no por el nombre del rol:

  * completo   (agente.consultar: Admin, Médico)  -> glosario, ubicar camas y agente NL2SQL sobre la base
                                                     analítica anonimizada (LLM o Plan B). Con
                                                     tablero.gerencial.ver (Admin) además enruta los
                                                     pronósticos a los microservicios predictivos.
  * operativo  (camas.ver / farmacia.alertas.ver, sin agente.consultar: Enfermería)
                                                  -> glosario, ubicar camas y SOLO las preguntas operativas del
                                                     Plan B (camas, inventario, rotación, espera, alertas). Sin LLM,
                                                     sin SQL libre y sin mostrar el SQL.
  * paciente   (portal.propio)                    -> glosario y SUS fórmulas y citas. Nunca toca la base del
                                                     hospital ni datos de otros pacientes.

Cualquier otra pregunta recibe una negativa amable que explica qué sí puede consultar.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from agent import DESTRUCTIVE_REQUEST, RAW_SQL, AgentResponse, normalize
from ui import glossary

FMT = "%Y-%m-%d %H:%M:%S"
NURSE_INTENTS = {"ocupacion", "stock", "rotacion", "espera", "alertas"}


@dataclass(frozen=True)
class Scope:
    code: str             # completo | operativo | paciente | ninguno
    subtitle: str         # lo que se muestra bajo el nombre del asistente
    can_see_sql: bool
    suggestions: tuple[str, ...]


SCOPES = {
    "completo": Scope("completo", "Datos del hospital en lenguaje natural", True, (
        "¿Cuántas camas de UCI están ocupadas hoy?",
        "¿Cuáles son los medicamentos con menos de 5 días de inventario o con menor rotación?",
        "¿Cuál es el tiempo de espera promedio en urgencias en la última semana?",
        "¿Qué servicio tiene más pacientes ingresados este mes?",
        "¿Dónde hay camas libres para adultos?")),
    "operativo": Scope("operativo", "Camas, farmacia y urgencias de tu turno", False, (
        "¿Dónde hay camas libres para adultos?",
        "¿Qué medicamentos tienen menos de 5 días de inventario?",
        "¿Cuál es el tiempo de espera en urgencias esta semana?",
        "¿Qué es triage 2?")),
    "paciente": Scope("paciente", "Tus fórmulas y tus citas", False, (
        "¿Qué medicamentos tengo por reclamar?",
        "¿Cuándo es mi próxima cita?",
        "Quiero una cita: tengo tos y fiebre desde hace 3 días",
        "¿Qué es una EPS?")),
    "ninguno": Scope("ninguno", "", False, ()),
}


def scope_for(permissions: set[str]) -> Scope:
    if "agente.consultar" in permissions:
        return SCOPES["completo"]
    if permissions & {"camas.ver", "farmacia.alertas.ver"}:
        return SCOPES["operativo"]
    if "portal.propio" in permissions:
        return SCOPES["paciente"]
    return SCOPES["ninguno"]


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text).lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _limit(question: str, scope: Scope) -> AgentResponse:
    can = {"operativo": "camas libres, inventario y rotación de medicamentos, tiempos de espera en urgencias, "
                        "alertas del hospital y significado de términos (por ejemplo, “¿qué es triage?”)",
           "paciente": "tus medicamentos por reclamar, tus citas, pedir una cita contándome qué te pasa y el "
                       "significado de términos de salud"}.get(
        scope.code, "nada por ahora")
    return AgentResponse(question, f"Eso no está disponible para tu usuario. Puedo ayudarte con: {can}.",
                         engine="fuera de alcance")


# ---------------------------------------------------------------------------
# Paciente: solo sus propios datos
# ---------------------------------------------------------------------------
_RX = re.compile(r"medicament|formula|receta|reclam|remedio|pastilla|farmacia|droga")
_APPT = re.compile(r"\bcita|consulta|control|reevaluac|medico")
_WANT_APPT = re.compile(r"(quiero|necesito|pedir|pido|solicit|agend|sacar|separar|programar|me pueden dar|dar)\w*\s.{0,40}"
                        r"\b(cita|consulta|medico|doctor)|\b(me duele|dolor|fiebre|tos\b|gripa|mareo|vomit|diarrea|"
                        r"me siento mal|sintoma|me enferme|brote|alergia)")


NO_ADVICE = ("No soy un profesional de la salud, así que no puedo recetarte, recomendarte medicamentos ni decirte qué "
             "tienes. Eso lo decide un médico que revise tu historia clínica, tus alergias y los medicamentos que ya "
             "tomas. Mientras tanto, evita automedicarte.")
_ADVICE = re.compile(r"recomiend|recet|que (me )?(puedo )?tom(o|ar)\b|puedo tomar|que (medicamento|remedio|pastilla|droga)s?"
                     r"\b(?! tengo)|pastillas? para|remedio para|jarabe para|dosis|diagnost|que tengo\b|sera (grave|normal)|"
                     r"es grave|es normal que|debo preocup|antibiotic|automedic|me (puedo|debo) (tomar|aplicar|inyectar|"
                     r"poner)|sirve para|mezclar|combinar|dejar de tomar|suspend|que significa (mi|el|este) (resultado|examen)|"
                     r"mis examenes (salieron|dicen)|me (hace|hara) dano|cura para|como (se )?cura|tratamiento para")
_DOSE = re.compile(r"cada cuant|como (me )?(lo |la )?tomo|cuant[oa]s? (me )?tomo|a que hora (me )?(lo |la )?tomo|"
                   r"(dosis|indicaciones) de (mi|la|el)")
_OWN_RX = re.compile(r"tengo por reclamar|por reclamar|pendientes?|mis (formulas|medicamentos)|mi formula|reclam")


def _forward_or_request(question: str, clin, id_paciente: int, now: str, tipo: str, pref: str) -> str:
    """Si hay una solicitud abierta, el mensaje llega a esa conversación con facturación; si no, se crea una."""
    import requests_service as rq
    pending = clin.execute("SELECT id FROM solicitudes_cita WHERE id_paciente = ? AND estado = 'PENDIENTE'",
                           (id_paciente,)).fetchone()
    if pending:
        rq.send_message(clin, pending[0], lado="PACIENTE", autor_id=None, texto=question, now=now,
                        id_paciente=id_paciente)
        return ("Ya tienes una solicitud de cita abierta: le pasé este mensaje a facturación para que lo tengan en "
                "cuenta al asignarte el profesional. Sigue la conversación en “Mis fórmulas y citas” → Mis citas.")
    phone = clin.execute("SELECT telefono FROM pacientes_clinicos WHERE id_paciente = ?", (id_paciente,)).fetchone()
    rq.create_request(clin, id_paciente=id_paciente, tipo=tipo, sintomas=question, preferencia=pref,
                      telefono=phone[0] if phone else None, canal="ASISTENTE", now=now)
    return (f"Le envié tu solicitud a facturación ({rq.TYPES[tipo].lower()}, {rq.PREFERENCES[pref].lower()}) con lo "
            "que me contaste. Te asignan el profesional adecuado y te escriben por WhatsApp o lo ves en “Mis fórmulas "
            "y citas” → Mis citas.")


def _kind(q: str) -> tuple[str, str]:
    tipo = "CONTROL" if "control" in q else "RESULTADOS" if "resultado" in q or "examen" in q else \
        "ESPECIALISTA" if "especialista" in q else "MEDICINA_GENERAL"
    pref = "MANANA" if "manana" in q else "TARDE" if "tarde" in q else "CUALQUIERA"
    return tipo, pref


def _request_appointment(question: str, clin: sqlite3.Connection, id_paciente: int, now: str,
                         advice: bool = False) -> AgentResponse:
    """El paciente pide una cita (o un consejo médico que el bot no puede dar): la solicitud llega a facturación."""
    import requests_service as rq
    q = _norm(question)
    lead = f"**{rq.EMERGENCY_TEXT}**\n\n" if rq.is_emergency(question) else ""
    if advice:
        lead += NO_ADVICE + "\n\n"
    tipo, pref = _kind(q)
    try:
        text = _forward_or_request(question, clin, id_paciente, now, tipo, pref)
    except sqlite3.IntegrityError as exc:
        text = f"{exc}. Puedes escribirle a facturación en “Mis fórmulas y citas” → Mis citas."
    return AgentResponse(question, lead + text, engine="orientación" if advice else "mis datos")


def _own_dose(question: str, clin: sqlite3.Connection, id_paciente: int) -> AgentResponse | None:
    """«¿Cada cuánto me tomo la claritromicina?»: se responde SOLO con lo que el médico dejó en SU fórmula."""
    q = _norm(question)
    rows = clin.execute("""
        SELECT f.nombre AS producto, p.dosis, p.frecuencia_horas, p.duracion_dias, u.nombre_mostrado AS medico
          FROM prescripciones p JOIN productos_farmacia f ON f.codigo = p.codigo_producto
          LEFT JOIN usuarios u ON u.id = p.medico_id
         WHERE p.id_paciente = ? AND p.estado IN ('VIGENTE','PARCIAL','PENDIENTE_STOCK')""", (id_paciente,)).fetchall()
    for r in rows:
        name = _norm(str(r["producto"]).split()[0])
        if len(name) >= 4 and name in q:
            return AgentResponse(question, f"Según la fórmula que te dejó {r['medico'] or 'tu médico'}: "
                                           f"**{str(r['producto']).capitalize()}**, {r['dosis']} cada "
                                           f"{r['frecuencia_horas']} horas durante {r['duracion_dias']} días. No "
                                           "cambies la dosis por tu cuenta; si tienes dudas o te cae mal, pide una "
                                           "cita y te lo revisan.", engine="mis datos")
    return None


def _patient_answer(question: str, clin: sqlite3.Connection, id_paciente: int, now: str) -> AgentResponse | None:
    q = _norm(question)
    t = datetime.strptime(now, FMT)
    if _DOSE.search(q):
        own = _own_dose(question, clin, id_paciente)
        if own is not None:
            return own
    if (_ADVICE.search(q) or _DOSE.search(q)) and not _OWN_RX.search(q):
        return _request_appointment(question, clin, id_paciente, now, advice=True)
    if _WANT_APPT.search(q) and not re.search(r"cuando|a que hora|tengo (alguna |una )?cita|mis citas|proxima", q):
        return _request_appointment(question, clin, id_paciente, now)
    if _RX.search(q):
        rows = clin.execute("""
            SELECT f.nombre AS producto, p.estado, p.ambito, p.fecha_limite_reclamo, p.requiere_reevaluacion,
                   p.dosis_prescritas - p.dosis_entregadas AS pendientes
              FROM prescripciones p JOIN productos_farmacia f ON f.codigo = p.codigo_producto
             WHERE p.id_paciente = ?""", (id_paciente,)).fetchall()
        pending = [r for r in rows if r["estado"] in ("VIGENTE", "PARCIAL") and r["ambito"] == "AMBULATORIA"]
        expired = [r for r in rows if r["estado"] == "CADUCADA" and r["requiere_reevaluacion"]]
        lines = [f"- **{str(r['producto']).capitalize()}**: {r['pendientes']} dosis por reclamar antes del "
                 f"{datetime.strptime(r['fecha_limite_reclamo'], FMT):%d/%m a las %H:%M}" for r in pending]
        lines += [f"- **{str(r['producto']).capitalize()}**: la fórmula venció; pide una cita de reevaluación "
                  "desde “Mis fórmulas y citas”." for r in expired]
        text = ("Esto es lo que tienes pendiente:\n" + "\n".join(lines)) if lines else \
            "No tienes medicamentos pendientes por reclamar."
        return AgentResponse(question, text, engine="mis datos")
    if _APPT.search(q):
        rows = clin.execute("SELECT fecha_hora, especialidad, motivo FROM citas WHERE id_paciente = ? "
                            "AND estado = 'PROGRAMADA' ORDER BY fecha_hora", (id_paciente,)).fetchall()
        upcoming = [r for r in rows if datetime.strptime(r["fecha_hora"], FMT) >= t]
        if not upcoming:
            return AgentResponse(question, "No tienes citas programadas.", engine="mis datos")
        c = upcoming[0]
        when = datetime.strptime(c["fecha_hora"], FMT)
        extra = f" Además tienes {len(upcoming) - 1} más." if len(upcoming) > 1 else ""
        return AgentResponse(question, f"Tu próxima cita es el **{when:%d/%m a las %H:%M}** en "
                                       f"**{str(c['especialidad']).title()}**.{extra}", engine="mis datos")
    return None


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------
def _kpis_or_none(code: str) -> dict | None:
    try:
        return ml.kpis(code)
    except ml.ServiceUnavailable:
        return None


def _forecast(question: str, agent) -> AgentResponse | None:
    """Pronósticos -> microservicio del dominio. Si el servicio no responde, cae al agente y lo avisa."""
    import ml_services as ml
    service = ml.route(question)
    if service is None:
        return None
    try:
        return AgentResponse(question, ml.describe(service, ml.predict(service.code), _kpis_or_none(service.code)), engine="modelo predictivo")
    except ml.ServiceUnavailable as exc:
        resp = agent.ask(question)
        resp.answer = (f"_El pronóstico de {service.name} no está disponible en este momento; "
                       f"respondo con el histórico._\n\n{resp.answer}")
        resp.error = str(exc)
        return resp


_MONTH = re.compile(r"\b(reporte|informe|resumen|balance)\b.{0,30}\b(mes|mensual)\b|\bmensual\b|en lo que va (del|de este) "
                    r"mes|lo que (va|llevamos) (del|de este) mes|acumulado del mes|como (vamos|va el hospital|va el mes)|"
                    r"(cuantos|cuantas) (pacientes|personas) (se han atendido|hemos atendido|se atendieron|han venido|"
                    r"van|llevamos)")


def _month_report(question: str, agent) -> AgentResponse:
    """Reporte desde el día 1 del mes hasta la fecha de corte, con el Excel para descargar."""
    import month_report as mr
    import reports
    mtd = mr.month_to_date(agent.conn, agent.ref)
    resp = AgentResponse(question, f"{mtd['titular']}\n\n{mtd['detalle']}\n\nAbajo el detalle frente a los mismos "
                                   f"días de {mtd['mes_anterior']}. El reporte arranca de nuevo el día 1 de cada mes.",
                         data=mr.table(mtd), engine="reporte del mes")
    resp.files = [(f"reporte_{mtd['mes']}_al_{mtd['fin']}.xlsx", reports.month_to_date_xlsx(mtd),
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")]
    return resp


def answer(question: str, scope: Scope, *, agent=None, bed_answer=None, clin=None, id_paciente=None,
           now: str | None = None, forecasts: bool = False) -> AgentResponse:
    """Responde dentro del alcance del rol. `bed_answer` ubica camas libres; `forecasts` habilita los
    microservicios predictivos (solo gerencia)."""
    question = (question or "").strip()[:500]
    if not question:
        return AgentResponse(question, "Escribe tu pregunta.")
    meaning = glossary.answer(question)
    if meaning:
        return AgentResponse(question, meaning, engine="glosario")

    if scope.code == "paciente":
        mine = _patient_answer(question, clin, id_paciente, now) if clin is not None else None
        return mine or _limit(question, scope)

    if scope.code == "ninguno":
        return _limit(question, scope)

    if scope.code == "operativo" and (RAW_SQL.match(question) or DESTRUCTIVE_REQUEST.search(normalize(question))):
        return AgentResponse(question, "Consulta bloqueada: el asistente es de solo lectura.", engine="seguridad")

    if scope.code == "completo" and agent is not None and _MONTH.search(normalize(question)):
        return _month_report(question, agent)

    beds = bed_answer(question) if bed_answer else None
    if beds is not None:
        return beds

    if scope.code == "completo":
        predicted = _forecast(question, agent) if forecasts else None
        return predicted or agent.ask(question)

    # operativo: solo intenciones verificadas del Plan B, sin LLM ni SQL libre
    intent = agent.match_intent(question)
    if intent is None or intent.name not in NURSE_INTENTS:
        return _limit(question, scope)
    resp = intent.handler(agent, question)
    resp.engine = "reglas"
    resp.sql = None           # la trazabilidad técnica es para gerencia y médicos
    return resp
