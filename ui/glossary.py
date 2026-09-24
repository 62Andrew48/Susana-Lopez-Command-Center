"""
ui/glossary.py — Términos del sector salud en lenguaje sencillo (fuente: "Glosario de términos del sector
salud · Guía de apoyo para el Diccionario de Datos HIS", 22/09/2026, entregado con el reto).

  * tip(): muestra un término con su explicación al pasar el cursor (subrayado punteado).
  * answer(): responde en el chat preguntas como "¿qué es triage?" o "¿qué significa CIE-10?" sin tocar la base.

Las definiciones son resúmenes del glosario del reto; las de "cama de expansión" y "estancia" explican
decisiones de este proyecto y así se indica.
"""
from __future__ import annotations

import html
import re
import unicodedata

TERMS: dict[str, tuple[str, str]] = {  # clave normalizada -> (nombre, explicación)
    "triage": ("Triage", "Clasificación de los pacientes de urgencias según la gravedad, para decidir quién se "
               "atiende primero (no por orden de llegada). Escala de 5 niveles: Triage I es el más urgente y "
               "Triage V el menos urgente."),
    "triage i": ("Triage I", "El nivel más urgente: atención inmediata."),
    "triage ii": ("Triage II", "Urgencia alta. La meta de referencia es atenderlo en máximo 30 minutos "
                  "(Resolución 5596 de 2015)."),
    "his": ("HIS", "Sistema de Información Hospitalaria: el software que administra admisiones, historia clínica, "
            "servicios y facturación. Los datos del reto son un extracto de un HIS."),
    "ips": ("IPS", "Institución Prestadora de Servicios de Salud: la clínica u hospital que atiende al paciente."),
    "eps": ("EPS", "Entidad Promotora de Salud: la aseguradora que administra la afiliación del paciente."),
    "arl": ("ARL", "Administradora de Riesgos Laborales: cubre accidentes de trabajo y enfermedades laborales."),
    "rips": ("RIPS", "Registro Individual de Prestación de Servicios de Salud: el reporte oficial de cada servicio "
             "prestado que las IPS envían al sistema de salud."),
    "cups": ("CUPS", "Clasificación Única de Procedimientos en Salud: los códigos oficiales de procedimientos."),
    "cie-10": ("CIE-10", "Clasificación Internacional de Enfermedades: los códigos de diagnóstico."),
    "regimen": ("Régimen", "Cómo está afiliada la persona al sistema de salud: contributivo (cotiza), subsidiado "
                "(lo subsidia el Estado), vinculado, particular u otro."),
    "ingreso": ("Ingreso", "Todo el episodio de atención de un paciente, desde que llega hasta el alta; no es una "
                "sola visita al médico."),
    "clase de ingreso": ("Clase de ingreso", "Ambulatorio (sin hospitalización) u hospitalario (con internación)."),
    "via de ingreso": ("Vía de ingreso", "Por dónde entró el paciente: urgencias, hospitalización programada, "
                       "remitido desde otra institución o cirugía ambulatoria."),
    "especialidad": ("Especialidad", "La rama de la medicina del profesional que prestó el servicio."),
    "area de servicio": ("Área de servicio", "La unidad del hospital donde se prestó el servicio (laboratorio, "
                         "farmacia, hospitalización...)."),
    "programacion quirurgica": ("Programación quirúrgica", "Agendar una cirugía antes de hacerla. Una cirugía "
                                "programada puede no ejecutarse, y una ejecutada puede no haberse programado."),
    "cama de expansion": ("Cama de expansión", "Cama que el HIS registra como “virtual”: capacidad adicional que "
                          "se habilita cuando las camas físicas no alcanzan (definición de este proyecto)."),
    "estancia": ("Estancia", "Tiempo que el paciente lleva internado. El extracto no trae fecha de alta: se estima "
                 "hasta su último servicio o medicamento registrado (supuesto de este proyecto)."),
}
ALIASES = {"triaje": "triage", "cie10": "cie-10", "cie 10": "cie-10", "cama virtual": "cama de expansion",
           "camas virtuales": "cama de expansion", "camas de expansion": "cama de expansion",
           "triage 1": "triage i", "triage 2": "triage ii", "t1": "triage i", "t2": "triage ii",
           "admision": "ingreso", "regimen de afiliacion": "regimen"}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text).lower())
    return "".join(c for c in text if not unicodedata.combining(c)).strip()


def lookup(term: str) -> tuple[str, str] | None:
    key = _norm(term)
    key = ALIASES.get(key, key)
    return TERMS.get(key)


def tip(term: str, label: str | None = None) -> str:
    """HTML: el término con su explicación al pasar el cursor."""
    found = lookup(term)
    shown = html.escape(label or term)
    if not found:
        return shown
    return (f'<span title="{html.escape(found[1])}" style="border-bottom:1px dotted currentColor;'
            f'cursor:help">{shown}</span>')


_QUESTION = re.compile(r"^\s*(que|q)\s+(es|son|significa|quiere decir)\s+(el |la |los |las |un |una )?(.+?)\s*\??$")


def answer(question: str) -> str | None:
    """'¿Qué es triage II?' -> explicación del glosario; None si no es una pregunta de definición conocida."""
    m = _QUESTION.match(_norm(question).replace("¿", "").replace("?", ""))
    if not m:
        return None
    found = lookup(m.group(4))
    if not found:
        return None
    name, text = found
    return f"**{name}:** {text}"
