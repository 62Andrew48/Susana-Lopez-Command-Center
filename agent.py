"""
agent.py — Agente conversacional NL2SQL (Controlador en el patrón MVC).

Flujo de una pregunta
---------------------
    pregunta ──► Router ──► (a) LLM genera SQL ──► SQLGuard ──► Ejecutor solo-lectura ──► respuesta
                        └─► (b) Plan B: intención por regex ──► SQL validado ──► respuesta

Capas de seguridad (defensa en profundidad)
    1. SQLGuard: solo SELECT/WITH, una sentencia, lista negra de palabras, sin tablas del sistema.
    2. Conexión SQLite en modo solo-lectura (mode=ro).
    3. Authorizer de SQLite: el motor niega cualquier operación que no sea lectura.
    4. Límite de filas y tiempo máximo por consulta.
    5. Privacidad: las columnas identificadoras nunca salen en la respuesta.
"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

import pandas as pd
import requests

import config
import database as db

log = logging.getLogger("agent")

# ---------------------------------------------------------------------------
# Esquema que se entrega al LLM (DDL exacto + semántica de negocio)
# ---------------------------------------------------------------------------
SCHEMA_DDL = """
-- Paciente anonimizado (sin nombre ni fecha de nacimiento)
CREATE TABLE pacientes (id_paciente INTEGER PRIMARY KEY, tipo_documento TEXT, sexo TEXT /*Masculino|Femenino*/,
  edad INTEGER, grupo_etario TEXT /*'<1 año','1-5','6-17','18-29','30-59','60+'*/, asegurador TEXT,
  regimen TEXT /*Subsidiado|Contributivo|Vinculado|Particular|Otro*/, departamento TEXT, municipio TEXT, zona TEXT /*Urbana|Rural*/);

-- Un ingreso = un episodio de atención completo. Fechas TEXT 'YYYY-MM-DD HH:MM:SS'
CREATE TABLE ingresos (oid_ingreso INTEGER PRIMARY KEY, consecutivo_ingreso INTEGER, id_paciente INTEGER REFERENCES pacientes,
  clase_ingreso TEXT /*Ambulatorio|Hospitalario*/, via_ingreso TEXT /*Urgencias|Remitido|Cirugia Ambulatorias|Hospitalizacion*/,
  tipo_riesgo TEXT, fecha_ingreso TEXT, fecha_hospitalizacion TEXT, oid_triage INTEGER,
  codigo_cama TEXT, nombre_cama TEXT, grupo_cama TEXT, subgrupo_cama TEXT,
  servicio TEXT /*'UCI','Cuidado Intermedio','Cuidado Básico Neonatal','Hospitalización','Pediatría','Urgencias','Recuperación','Gineco-obstetricia','Sala de partos'*/,
  codigo_diagnostico TEXT /*CIE-10*/, nombre_diagnostico TEXT, capitulo_cie10 TEXT /*ej. 'Respiratorio','Digestivo'*/,
  fecha_atencion TEXT /*primera atención médica*/, tiempo_espera_min REAL /*fecha_atencion - fecha_ingreso*/,
  espera_valida INTEGER /*1 si 0<=espera<=24h; filtrar SIEMPRE espera_valida=1 al promediar*/,
  fecha_triage TEXT, nivel_triage INTEGER /*1 (más urgente) a 5*/, clasificacion_triage TEXT,
  espera_triage_atencion_min REAL, turno TEXT /*Mañana 7-13h|Tarde 13-19h|Noche 19-7h*/,
  fecha_inicio_estancia TEXT, fecha_fin_estimada TEXT /*última prestación registrada*/, estancia_horas REAL);

CREATE TABLE atenciones (oid_ingreso INTEGER, fecha_atencion TEXT);
CREATE TABLE triage (oid_triage INTEGER, fecha_triage TEXT, id_paciente INTEGER, codigo_triage TEXT, clasificacion_triage TEXT,
  nivel_triage INTEGER, tension_arterial TEXT, frecuencia_cardiaca REAL, frecuencia_respiratoria REAL, temperatura REAL);

-- Procedimientos/servicios prestados (CUPS)
CREATE TABLE servicios (oid_s INTEGER, oid_ingreso INTEGER, codigo_servicio TEXT, nombre_servicio TEXT, cantidad REAL,
  fecha_prestacion TEXT, codigo_area_servicio TEXT, area_servicio TEXT, especialidad TEXT);

-- Dispensación de medicamentos e insumos (codigo ATC-like: J01=antibióticos)
CREATE TABLE medicamentos_insumos (oid_mi INTEGER, oid_ingreso INTEGER, codigo TEXT, nombre TEXT, cantidad REAL,
  fecha_prestacion TEXT, area_servicio TEXT, especialidad TEXT, tipo_item TEXT /*Medicamento|Insumo / dispositivo*/);

-- Inventario por ítem (stock_simulado=1 => existencias simuladas para la demo)
CREATE TABLE inventario_farmacia (codigo TEXT, nombre TEXT, tipo_item TEXT, consumo_total REAL, dispensaciones INTEGER,
  dias_con_movimiento INTEGER, ultima_dispensacion TEXT, consumo_30d REAL, consumo_diario_promedio REAL,
  categoria_rotacion TEXT /*Alta|Media|Baja|Sin movimiento 30 días*/, stock_actual REAL, stock_simulado INTEGER,
  dias_inventario REAL /*stock_actual / consumo_diario_promedio*/);

-- Censo diario de camas por subgrupo (fecha TEXT 'YYYY-MM-DD'). USAR ESTA TABLA PARA OCUPACIÓN.
CREATE TABLE ocupacion_diaria (fecha TEXT, servicio TEXT, grupo_cama TEXT, subgrupo_cama TEXT, capacidad INTEGER,
  camas_virtuales INTEGER, camas_ocupadas INTEGER, pacientes INTEGER, porcentaje_ocupacion REAL);
CREATE TABLE camas (codigo_cama TEXT, nombre_cama TEXT, grupo_cama TEXT, subgrupo_cama TEXT, servicio TEXT, es_virtual INTEGER);

-- Cirugías: una fila por programación
CREATE TABLE cirugias (consecutivo_programacion TEXT, id_paciente INTEGER, oid_ingreso INTEGER, procedimientos INTEGER,
  fecha_cirugia TEXT, area_quirofano TEXT, en_periodo INTEGER,
  estado TEXT /*Realizada|Programada sin evidencia de ejecución|Ingreso fuera del periodo o sin ingreso*/,
  fecha_ingreso TEXT, servicio TEXT);
CREATE TABLE programacion_cirugia (consecutivo_programacion TEXT, id_paciente INTEGER, oid_ingreso INTEGER, codigo_servicio TEXT);
CREATE TABLE metadatos (clave TEXT, valor TEXT);
""".strip()

# Columnas que identifican personas o registros individuales: nunca se muestran.
IDENTIFIER_COLUMNS = {"id_paciente", "oid_ingreso", "consecutivo_ingreso", "oid_triage", "oid_s", "oid_mi",
                      "consecutivo_programacion", "nombre_paciente", "fecha_nacimiento", "idpaciente2",
                      "motivo_consulta", "tension_arterial"}


# ---------------------------------------------------------------------------
# Modelos de respuesta
# ---------------------------------------------------------------------------
@dataclass
class Alert:
    severity: str          # "crítica" | "alta" | "media" | "info"
    category: str          # Ocupación | Farmacia | Urgencias | Demanda | Cirugías
    title: str
    detail: str
    action: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class AgentResponse:
    question: str
    answer: str
    sql: str | None = None
    data: pd.DataFrame | None = None
    engine: str = "reglas"                  # "llm" | "reglas" | "reglas (respaldo)"
    chart: dict | None = None               # {"type": "bar"|"line"|"pie", "x": col, "y": col, "color": col}
    recommendations: list[Alert] = field(default_factory=list)
    error: str | None = None
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "question": self.question, "answer": self.answer, "sql": self.sql, "engine": self.engine,
            "chart": self.chart, "error": self.error, "elapsed_ms": self.elapsed_ms,
            "columns": list(self.data.columns) if self.data is not None else [],
            "rows": (json.loads(self.data.to_json(orient="records", force_ascii=False))
                     if self.data is not None else []),
            "recommendations": [r.to_dict() for r in self.recommendations],
        }


class UnsafeQueryError(ValueError):
    """La consulta no superó la validación de seguridad."""


# ---------------------------------------------------------------------------
# 1. Sanitizador de SQL
# ---------------------------------------------------------------------------
class SQLGuard:
    FORBIDDEN = re.compile(
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|UPSERT|MERGE|TRUNCATE|ATTACH|DETACH|PRAGMA|"
        r"VACUUM|REINDEX|ANALYZE|GRANT|REVOKE|EXEC|EXECUTE|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE|"
        r"LOAD_EXTENSION|READFILE|WRITEFILE|EDIT|FTS3_TOKENIZER)\b",
        re.IGNORECASE)
    SYSTEM_TABLES = re.compile(r"\bsqlite_\w+", re.IGNORECASE)

    @staticmethod
    def strip_fences(text: str) -> str:
        match = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
        return (match.group(1) if match else text).strip()

    @classmethod
    def sanitize(cls, sql: str, row_limit: int | None = None) -> str:
        row_limit = row_limit or config.SQL_ROW_LIMIT
        if not sql or not sql.strip():
            raise UnsafeQueryError("La consulta está vacía.")
        sql = cls.strip_fences(sql)
        sql = re.sub(r"--[^\n]*", " ", sql)                    # comentarios de línea
        sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)  # comentarios de bloque
        sql = sql.strip().rstrip(";").strip()
        # Se analiza el texto sin literales para no bloquear, p. ej., WHERE nombre LIKE '%DELETE%'
        without_literals = re.sub(r"'(?:[^']|'')*'", "''", sql)
        if ";" in without_literals:
            raise UnsafeQueryError("Solo se permite una sentencia por consulta.")
        if not re.match(r"^\s*(SELECT|WITH)\b", without_literals, re.IGNORECASE):
            raise UnsafeQueryError("Solo se permiten consultas SELECT.")
        bad = cls.FORBIDDEN.search(without_literals)
        if bad:
            raise UnsafeQueryError(f"Palabra reservada no permitida: {bad.group(0).upper()}")
        if cls.SYSTEM_TABLES.search(without_literals):
            raise UnsafeQueryError("No se permite consultar tablas internas del motor.")
        if re.search(r"\bnombre_paciente\b|\bfecha_nacimiento\b|\bmotivo_consulta\b", without_literals, re.I):
            raise UnsafeQueryError("La consulta solicita datos personales identificables.")
        # Tope de filas: se envuelve la consulta (funciona también con CTE)
        return f"SELECT * FROM (\n{sql}\n) LIMIT {int(row_limit)}"


# ---------------------------------------------------------------------------
# 2. Ejecutor solo-lectura
# ---------------------------------------------------------------------------
_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
if hasattr(sqlite3, "SQLITE_RECURSIVE"):
    _ALLOWED_ACTIONS.add(sqlite3.SQLITE_RECURSIVE)


def _authorizer(action, arg1, arg2, dbname, source):  # noqa: ARG001 - firma exigida por sqlite3
    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


class QueryExecutor:
    def __init__(self, db_path=None):
        self.conn = db.get_connection(read_only=True, db_path=db_path)
        self.conn.set_authorizer(_authorizer)

    def run(self, sql: str) -> tuple[str, pd.DataFrame]:
        safe_sql = SQLGuard.sanitize(sql)
        deadline = time.monotonic() + config.SQL_TIMEOUT_SECONDS
        self.conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        try:
            df = pd.read_sql_query(safe_sql, self.conn)
        finally:
            self.conn.set_progress_handler(None, 0)
        return safe_sql, anonymize(df)


def anonymize(df: pd.DataFrame) -> pd.DataFrame:
    """Elimina columnas identificadoras del resultado (aunque el SQL las haya pedido)."""
    drop = [c for c in df.columns if c.lower() in IDENTIFIER_COLUMNS]
    return df.drop(columns=drop)


# ---------------------------------------------------------------------------
# 3. Clientes LLM (patrón Factory)
# ---------------------------------------------------------------------------
class LLMClient:
    name = "base"

    def complete(self, system: str, messages: list[dict]) -> str:  # pragma: no cover - interfaz
        raise NotImplementedError


class OpenAIClient(LLMClient):
    name = "openai"

    def complete(self, system, messages):
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
            json={"model": config.OPENAI_MODEL, "temperature": 0,
                  "messages": [{"role": "system", "content": system}, *messages]},
            timeout=config.LLM_TIMEOUT_SECONDS)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


class AnthropicClient(LLMClient):
    name = "anthropic"

    def complete(self, system, messages):
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
            json={"model": config.ANTHROPIC_MODEL, "max_tokens": 1024, "temperature": 0,
                  "system": system, "messages": messages},
            timeout=config.LLM_TIMEOUT_SECONDS)
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"])


class OllamaClient(LLMClient):
    """Modelo local (p. ej. SQLCoder): los datos nunca salen del hospital."""
    name = "ollama"

    def complete(self, system, messages):
        r = requests.post(
            f"{config.OLLAMA_URL}/api/chat",
            json={"model": config.OLLAMA_MODEL, "stream": False, "options": {"temperature": 0},
                  "messages": [{"role": "system", "content": system}, *messages]},
            timeout=config.LLM_TIMEOUT_SECONDS * 3)
        r.raise_for_status()
        return r.json()["message"]["content"]


def create_llm_client(provider: str | None = None) -> LLMClient | None:
    provider = (provider or config.LLM_PROVIDER).lower()
    if provider == "openai" and config.OPENAI_API_KEY:
        return OpenAIClient()
    if provider == "anthropic" and config.ANTHROPIC_API_KEY:
        return AnthropicClient()
    if provider == "ollama":
        return OllamaClient()
    return None


# ---------------------------------------------------------------------------
# 4. Utilidades de lenguaje
# ---------------------------------------------------------------------------
def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def fmt_num(value, decimals: int = 0) -> str:
    """Formato colombiano: 1.234,5"""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "N/D"
    s = f"{value:,.{decimals}f}"
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def fmt_minutes(minutes) -> str:
    if minutes is None or (isinstance(minutes, float) and math.isnan(minutes)):
        return "N/D"
    h, m = divmod(int(round(minutes)), 60)
    return f"{h} h {m} min" if h else f"{m} min"


def parse_period(question: str, ref: date, default: str = "mes") -> tuple[date, date, str]:
    """Extrae el periodo de la pregunta. Devuelve (inicio, fin, etiqueta)."""
    q = normalize(question)
    m = re.search(r"ultimos?\s+(\d{1,3})\s+dias", q)
    if m:
        n = int(m.group(1))
        return ref - timedelta(days=n - 1), ref, f"últimos {n} días"
    if "ayer" in q:
        d = ref - timedelta(days=1)
        return d, d, "ayer"
    if re.search(r"\bhoy\b|\bactual|\bahora\b|en este momento", q):
        return ref, ref, "hoy"
    if re.search(r"(ultima|esta|la) semana|semanal|7 dias", q):
        return ref - timedelta(days=6), ref, "última semana"
    if re.search(r"ultimo mes|30 dias", q):
        return ref - timedelta(days=29), ref, "últimos 30 días"
    if re.search(r"este mes|mes actual|del mes|en el mes", q):
        return ref.replace(day=1), ref, "mes en curso"
    if re.search(r"historic|todo el periodo|desde mayo|total", q):
        return date(2000, 1, 1), ref, "todo el periodo"
    if default == "hoy":
        return ref, ref, "hoy"
    if default == "semana":
        return ref - timedelta(days=6), ref, "última semana"
    return ref.replace(day=1), ref, "mes en curso"


SERVICE_KEYWORDS = [
    (r"\buci\b|intensiv", "UCI"), (r"intermedi|ucin\b", "Cuidado Intermedio"),
    (r"basico|neonat", "Cuidado Básico Neonatal"), (r"pediatr", "Pediatría"),
    (r"hospitaliz", "Hospitalización"), (r"urgenc", "Urgencias"),
    (r"gineco|obstetr|materna", "Gineco-obstetricia"), (r"recuperac", "Recuperación"),
    (r"parto", "Sala de partos"),
]


def detect_service(question: str) -> str | None:
    q = normalize(question)
    for pattern, service in SERVICE_KEYWORDS:
        if re.search(pattern, q):
            return service
    return None


def lit(d: date, end: bool = False) -> str:
    return f"'{d:%Y-%m-%d} {'23:59:59' if end else '00:00:00'}'"


# ---------------------------------------------------------------------------
# 5. Motor de recomendaciones
# ---------------------------------------------------------------------------
ANTIMICROBIAL_BY_CHAPTER = {
    "Respiratorio": ("J01", "antibióticos"), "Infecciosas y parasitarias": ("J01", "antibióticos"),
}


class RecommendationEngine:
    SEVERITY_ORDER = {"crítica": 0, "alta": 1, "media": 2, "info": 3}

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.ref = db.get_reference_date(conn)

    # --- Ocupación -------------------------------------------------------------
    @staticmethod
    def staff_pool(subgroup: str) -> str:
        """Población atendida: el personal solo se reasigna entre unidades compatibles."""
        name = normalize(subgroup)
        if "neonat" in name:
            return "neonatal"
        if "pediatr" in name:
            return "pediátrica"
        if "gineco" in name or "parto" in name:
            return "materna"
        if "intensiv" in name or "intermedio" in name:
            return "crítica adultos"
        return "adultos general"

    def occupancy_alerts(self, service: str | None = None) -> list[Alert]:
        occ = db.kpi_bed_occupancy(self.conn, self.ref, by="subgrupo_cama")
        physical = occ[occ["servicio"] != "Urgencias"].copy()
        physical["pool"] = physical["subgrupo_cama"].map(self.staff_pool)
        alerts = []
        for _, r in physical.iterrows():
            if service and r["servicio"] != service:
                continue
            pct = r["porcentaje_ocupacion"]
            if pct < config.OCCUPANCY_WARNING_PCT:
                continue
            critical = pct >= config.OCCUPANCY_CRITICAL_PCT
            free = int(r["capacidad"] - r["camas_ocupadas"])
            beds_to_open = max(math.ceil(r["camas_ocupadas"] - r["capacidad"] * config.OCCUPANCY_WARNING_PCT / 100), 0)
            donors = physical[(physical["pool"] == r["pool"]) & (physical["subgrupo_cama"] != r["subgrupo_cama"])
                              & (physical["porcentaje_ocupacion"] < config.OCCUPANCY_LOW_PCT)
                              ].sort_values("porcentaje_ocupacion").head(2)
            donor_txt = (", ".join(f"{d.subgrupo_cama.title()} ({fmt_num(d.porcentaje_ocupacion, 1)} %)"
                                   for d in donors.itertuples())
                         if not donors.empty else "el grupo de enfermería flotante (no hay unidades "
                                                  f"de población {r['pool']} con holgura)")
            alerts.append(Alert(
                severity="crítica" if critical else "alta", category="Ocupación",
                title=f"{r['subgrupo_cama'].title()} al {fmt_num(pct, 1)} % de ocupación",
                detail=f"{int(r['camas_ocupadas'])} de {int(r['capacidad'])} camas ocupadas; quedan {free} libres. "
                       f"Para volver al {fmt_num(config.OCCUPANCY_WARNING_PCT)} % se necesitan "
                       f"{beds_to_open} camas adicionales o egresos.",
                action=(f"Habilitar {beds_to_open} camas de expansión, priorizar altas antes de las 11:00 y "
                        f"reasignar personal desde {donor_txt}."
                        if critical else
                        f"Vigilar egresos del día y preparar reasignación de personal desde {donor_txt}.")))
        return alerts

    # --- Farmacia ---------------------------------------------------------------
    def stock_alerts(self, top: int = 5) -> list[Alert]:
        crit = db.kpi_critical_stock(self.conn, limit=top)
        total = len(db.kpi_critical_stock(self.conn, limit=10_000))
        if crit.empty:
            return []
        sim = " (stock simulado para la demo)" if crit["stock_simulado"].max() == 1 else ""
        lines = []
        for r in crit.itertuples():
            order_qty = max(math.ceil(r.consumo_diario_promedio * 15 - r.stock_actual), 0)
            lines.append(f"{r.nombre[:55]}: {fmt_num(r.dias_inventario, 1)} días → pedir {fmt_num(order_qty)} und.")
        return [Alert(
            severity="crítica", category="Farmacia",
            title=f"{total} ítems con menos de {fmt_num(config.STOCK_CRITICAL_DAYS)} días de inventario{sim}",
            detail="Prioridad por impacto: " + "; ".join(lines),
            action="Generar órdenes de compra para cubrir 15 días y activar préstamo entre IPS "
                   "para los ítems con menos de 2 días.")]

    # --- Urgencias --------------------------------------------------------------
    def wait_alerts(self) -> list[Alert]:
        start, end = db.last_n_days(self.ref, 7)
        waits = db.kpi_wait_times(self.conn, start, end)
        alerts = []
        t2 = waits["por_triage"].query("triage == 'Triage 2'")
        if not t2.empty and t2["espera_promedio_min"].iat[0] > config.WAIT_TARGET_TRIAGE2_MIN:
            alerts.append(Alert(
                severity="alta", category="Urgencias",
                title=f"Triage 2 espera {fmt_minutes(t2['espera_promedio_min'].iat[0])} en promedio",
                detail=f"La meta de referencia es ≤ {fmt_num(config.WAIT_TARGET_TRIAGE2_MIN)} min "
                       "(Resolución 5596 de 2015). Medido en los últimos 7 días.",
                action="Asignar un médico de respuesta rápida exclusivo para Triage 2 en los turnos críticos."))
        by_shift = waits["por_turno_triage"]
        if not by_shift.empty:
            shift = (by_shift.assign(peso=by_shift["atenciones"] * by_shift["espera_promedio_min"])
                     .groupby("turno")[["peso", "atenciones"]].sum())
            shift["prom"] = shift["peso"] / shift["atenciones"]
            worst = shift["prom"].idxmax()
            share3 = by_shift.query("turno == @worst and triage == 'Triage 3'")["atenciones"].sum() / max(
                shift.loc[worst, "atenciones"], 1) * 100
            alerts.append(Alert(
                severity="media", category="Urgencias",
                title=f"Causa raíz: el turno {worst.lower()} concentra la mayor espera "
                      f"({fmt_minutes(shift.loc[worst, 'prom'])})",
                detail=f"{fmt_num(share3, 0)} % de las atenciones de ese turno son Triage 3; "
                       f"total analizado: {fmt_num(waits['atenciones'])} atenciones.",
                action=f"Reforzar el turno {worst.lower()} con un consultorio adicional de Triage 3 "
                       "o redistribuir horas médicas desde el turno de menor espera."))
        return alerts

    # --- Picos de demanda (alerta predictiva simple) ----------------------------
    def demand_alerts(self) -> list[Alert]:
        trend = db.kpi_demand_trend(self.conn, self.ref)
        spikes = trend[(trend["variacion_pct"] >= config.DEMAND_SPIKE_PCT) & (trend["ultimos_7d"] >= 10)]
        alerts = []
        for r in spikes.itertuples():
            action = "Anticipar recurso humano y camas para la próxima semana."
            atc = ANTIMICROBIAL_BY_CHAPTER.get(r.categoria)
            if atc:
                items = pd.read_sql_query(
                    "SELECT nombre, dias_inventario FROM inventario_farmacia WHERE codigo LIKE ? "
                    "AND consumo_30d > 0 ORDER BY consumo_diario_promedio DESC LIMIT 3", self.conn,
                    params=(f"{atc[0]}%",))
                if not items.empty:
                    action = (f"Aumentar el stock de {atc[1]}: " + "; ".join(
                        f"{i.nombre[:40]} ({fmt_num(i.dias_inventario, 1)} días)" for i in items.itertuples()) + ".")
            alerts.append(Alert(
                severity="alta" if r.variacion_pct >= 2 * config.DEMAND_SPIKE_PCT else "media",
                category="Demanda",
                title=f"Ingresos por causa {r.categoria.lower()} +{fmt_num(r.variacion_pct, 1)} %",
                detail=f"{r.ultimos_7d} ingresos en los últimos 7 días vs. "
                       f"{fmt_num(r.promedio_semanal_base, 1)} de promedio semanal en las 4 semanas previas.",
                action=action))
        via = db.kpi_demand_trend(self.conn, self.ref, by="via_ingreso").query("categoria == 'Urgencias'")
        if not via.empty and via["variacion_pct"].iat[0] >= config.DEMAND_SPIKE_PCT / 2:
            alerts.append(Alert(
                severity="media", category="Demanda",
                title=f"Urgencias recibe +{fmt_num(via['variacion_pct'].iat[0], 1)} % de ingresos",
                detail=f"{via['ultimos_7d'].iat[0]} ingresos por urgencias en 7 días.",
                action="Extender el horario del consultorio de Triage 3 y revisar la disponibilidad de camas "
                       "de observación."))
        return alerts

    # --- Cirugías ---------------------------------------------------------------
    def surgery_alerts(self) -> list[Alert]:
        start, end = self.ref - timedelta(days=29), self.ref
        s = db.kpi_surgeries(self.conn, start, end)
        alerts = []
        if s["cumplimiento_pct"] is not None and s["cumplimiento_pct"] < config.SURGERY_COMPLIANCE_MIN_PCT:
            alerts.append(Alert(
                severity="alta", category="Cirugías",
                title=f"Cumplimiento quirúrgico {fmt_num(s['cumplimiento_pct'], 1)} %",
                detail=f"{s['realizadas']} de {s['programadas']} programaciones con evidencia de ejecución (30 días).",
                action="Auditar cancelaciones y confirmar pacientes 48 h antes de la cirugía."))
        by_day = pd.read_sql_query("""
            SELECT CAST(strftime('%w', fecha_cirugia) AS INTEGER) AS dia, COUNT(*) AS cirugias
            FROM cirugias WHERE estado = 'Realizada' AND fecha_cirugia >= ? GROUP BY 1""",
                                   self.conn, params=(f"{self.ref - timedelta(days=55):%Y-%m-%d}",))
        weekdays = by_day[by_day["dia"].between(1, 5)]
        if len(weekdays) >= 2:
            names = ["domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado"]
            lo, hi = weekdays.loc[weekdays["cirugias"].idxmin()], weekdays.loc[weekdays["cirugias"].idxmax()]
            if hi["cirugias"] > lo["cirugias"] * 1.25:
                alerts.append(Alert(
                    severity="info", category="Cirugías",
                    title="Carga quirúrgica desbalanceada entre días hábiles",
                    detail=f"{names[int(hi['dia'])].capitalize()}: {int(hi['cirugias'])} cirugías vs. "
                           f"{names[int(lo['dia'])]}: {int(lo['cirugias'])} (últimas 8 semanas).",
                    action=f"Trasladar procedimientos electivos al {names[int(lo['dia'])]} para nivelar el uso "
                           "de quirófanos y reducir horas extra."))
        return alerts

    def generate_all(self) -> list[Alert]:
        alerts: list[Alert] = []
        for fn in (self.occupancy_alerts, self.stock_alerts, self.wait_alerts, self.demand_alerts,
                   self.surgery_alerts):
            try:
                alerts.extend(fn())
            except Exception as exc:  # una regla rota no debe tumbar el módulo
                log.exception("Regla %s falló: %s", fn.__name__, exc)
        return sorted(alerts, key=lambda a: self.SEVERITY_ORDER.get(a.severity, 9))


# ---------------------------------------------------------------------------
# 6. Plan B: intenciones con SQL validado
# ---------------------------------------------------------------------------
@dataclass
class Intent:
    name: str
    pattern: re.Pattern
    handler: Callable[["HospitalAgent", str], AgentResponse]
    priority: int = 50

    def matches(self, q: str) -> bool:
        return bool(self.pattern.search(q))


RAW_SQL = re.compile(r"^\s*(select|with|drop|delete|update|insert|alter|create|pragma|attach|replace)\b", re.I)
# Órdenes imperativas al inicio del mensaje ("borra…", "elimina…"); no bloquea preguntas analíticas
# como "¿qué medicamentos conviene eliminar del inventario?".
DESTRUCTIVE_REQUEST = re.compile(r"^\W*(por favor\s+)?(borra|borrar|elimina|eliminar|suprime|modifica|actualiza|"
                                 r"inserta|agrega|cambia|vacia|drop|delete|truncate)\b")


class HospitalAgent:
    """Fachada del agente. Uso: HospitalAgent().ask('¿Cuántas camas de UCI están ocupadas hoy?')"""

    def __init__(self, provider: str | None = None, mode: str | None = None, db_path=None):
        self.executor = QueryExecutor(db_path)
        self.conn = self.executor.conn
        self.ref = db.get_reference_date(self.conn)
        self.llm = create_llm_client(provider)
        # llm_first: LLM y, si falla, Plan B | hybrid: Plan B si reconoce la intención, si no LLM | rules
        self.mode = (mode or ("llm_first" if self.llm else "rules")).lower()
        self.recommender = RecommendationEngine(self.conn)
        self.intents = sorted(self._build_intents(), key=lambda i: i.priority)

    # ------------------------------------------------------------------ API pública
    def ask(self, question: str) -> AgentResponse:
        t0 = time.time()
        question = (question or "").strip()[:500]
        if not question:
            return AgentResponse(question, "Escribe una pregunta sobre la operación del hospital.")
        intent = self.match_intent(question)
        try:
            if RAW_SQL.match(question):          # SQL escrito por el usuario: mismo guardián
                safe_sql, df = self.executor.run(question)
                resp = AgentResponse(question, f"Consulta ejecutada: {len(df)} fila(s).", safe_sql, df,
                                     engine="sql directo", chart=auto_chart(df))
            elif DESTRUCTIVE_REQUEST.search(normalize(question)):
                raise UnsafeQueryError("el asistente es de solo lectura; no puede borrar, modificar ni "
                                       "insertar información del HIS.")
            elif self.mode == "rules" or not self.llm:
                resp = self._run_intent(intent, question) if intent else self._help(question)
            elif self.mode == "hybrid" and intent:
                resp = self._run_intent(intent, question)
            else:
                try:
                    resp = self._ask_llm(question)
                except UnsafeQueryError:
                    raise  # un intento de SQL peligroso se reporta, no se enmascara con el Plan B
                except Exception as exc:
                    log.warning("LLM falló (%s); se usa el Plan B", exc)
                    resp = self._run_intent(intent, question) if intent else self._help(question)
                    resp.engine = "reglas (respaldo)"
                    resp.error = f"El LLM no respondió correctamente: {exc}"
        except UnsafeQueryError as exc:
            resp = AgentResponse(question, f"Consulta bloqueada por seguridad: {exc}", engine="seguridad",
                                 error=str(exc))
        resp.elapsed_ms = int((time.time() - t0) * 1000)
        return resp

    def match_intent(self, question: str) -> Intent | None:
        q = normalize(question)
        return next((i for i in self.intents if i.matches(q)), None)

    def alerts(self) -> list[Alert]:
        return self.recommender.generate_all()

    def close(self) -> None:
        """Libera el archivo de la base (necesario en Windows antes de reconstruirla)."""
        self.conn.close()

    # ------------------------------------------------------------------ NL2SQL con LLM
    def _system_prompt(self) -> str:
        r = self.ref
        return f"""Eres un analista de datos del Hospital Susana López de Valencia (Popayán). Conviertes preguntas
en español a UNA consulta SQLite de solo lectura.

ESQUEMA:
{SCHEMA_DDL}

REGLAS:
- La fecha de referencia ("hoy") es {r:%Y-%m-%d}. Los datos son históricos: NUNCA uses date('now').
- "Última semana" = '{r - timedelta(days=6):%Y-%m-%d}' a '{r:%Y-%m-%d}'. "Este mes" = '{r:%Y-%m}-01' a '{r:%Y-%m-%d}'.
- Fechas en ingresos/servicios/medicamentos son TEXT 'YYYY-MM-DD HH:MM:SS'; usa date(col) o BETWEEN con '23:59:59'.
- Ocupación de camas: usa ocupacion_diaria (fecha exacta) y excluye Urgencias de la ocupación global.
- Tiempos de espera: AVG(tiempo_espera_min) con espera_valida = 1.
- Nunca selecciones id_paciente, oid_ingreso ni datos personales; responde con agregados.
- Solo SELECT o WITH. Una sola sentencia. Máximo 50 filas salvo que pidan series por día.
- Usa alias de columna descriptivos en español (snake_case).
- Si la pregunta no se puede responder con estos datos, responde exactamente: NO_SQL: <motivo breve>.
- Responde SOLO con el SQL dentro de un bloque ```sql```.

EJEMPLOS:
P: ¿Cuántas camas de UCI están ocupadas hoy?
```sql
SELECT subgrupo_cama, camas_ocupadas, capacidad, porcentaje_ocupacion FROM ocupacion_diaria
WHERE fecha = '{r:%Y-%m-%d}' AND servicio = 'UCI' ORDER BY camas_ocupadas DESC
```
P: ¿Cuáles son los medicamentos con menos de 5 días de inventario?
```sql
SELECT nombre, stock_actual, consumo_diario_promedio, dias_inventario FROM inventario_farmacia
WHERE tipo_item = 'Medicamento' AND dias_inventario < 5 ORDER BY dias_inventario ASC LIMIT 30
```
P: ¿Cuál es el tiempo de espera promedio en urgencias en la última semana?
```sql
SELECT nivel_triage, COUNT(*) AS atenciones, ROUND(AVG(tiempo_espera_min),1) AS espera_promedio_min
FROM ingresos WHERE via_ingreso = 'Urgencias' AND espera_valida = 1
AND fecha_ingreso BETWEEN '{r - timedelta(days=6):%Y-%m-%d} 00:00:00' AND '{r:%Y-%m-%d} 23:59:59'
GROUP BY nivel_triage ORDER BY nivel_triage
```
P: ¿Qué servicio tiene más pacientes ingresados este mes?
```sql
SELECT servicio, COUNT(DISTINCT id_paciente) AS pacientes FROM ingresos
WHERE fecha_ingreso BETWEEN '{r:%Y-%m}-01 00:00:00' AND '{r:%Y-%m-%d} 23:59:59'
GROUP BY servicio ORDER BY pacientes DESC
```
P: ¿Qué especialidades ordenaron más servicios en agosto?
```sql
SELECT especialidad, COUNT(*) AS servicios FROM servicios
WHERE fecha_prestacion BETWEEN '2026-08-01 00:00:00' AND '2026-08-31 23:59:59'
GROUP BY especialidad ORDER BY servicios DESC LIMIT 10
```"""

    def _ask_llm(self, question: str) -> AgentResponse:
        system = self._system_prompt()
        messages = [{"role": "user", "content": question}]
        raw = self.llm.complete(system, messages)
        if raw.strip().upper().startswith("NO_SQL"):
            return AgentResponse(question, "No puedo responder eso con los datos disponibles: "
                                 + raw.split(":", 1)[-1].strip(), engine="llm")
        try:
            safe_sql, df = self.executor.run(raw)
        except UnsafeQueryError:
            raise
        except Exception as exc:  # autocorrección: un reintento con el error del motor
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": f"La consulta falló con: {exc}. Corrígela."}]
            raw = self.llm.complete(system, messages)
            safe_sql, df = self.executor.run(raw)
        answer = self._narrate(question, safe_sql, df)
        return AgentResponse(question, answer, safe_sql, df, engine="llm", chart=auto_chart(df),
                             recommendations=self._contextual_recommendations(question))

    def _narrate(self, question: str, sql: str, df: pd.DataFrame) -> str:
        if df.empty:
            return "La consulta no devolvió registros para ese criterio."
        if config.LLM_NARRATIVE:
            try:
                preview = df.head(25).to_csv(index=False)
                return self.llm.complete(
                    "Eres un asesor de gestión hospitalaria. Responde en español, en 2 a 4 frases, con las cifras "
                    "clave del resultado. No inventes datos que no estén en la tabla. No menciones SQL.",
                    [{"role": "user", "content": f"Pregunta: {question}\nResultado (CSV):\n{preview}"}]).strip()
            except Exception as exc:
                log.warning("Narrativa LLM falló: %s", exc)
        first = df.iloc[0].to_dict()
        detail = ", ".join(f"{k}: {v}" for k, v in first.items())
        return f"La consulta devolvió {len(df)} fila(s). Primer resultado → {detail}."

    def _contextual_recommendations(self, question: str) -> list[Alert]:
        q = normalize(question)
        try:
            if re.search(r"cama|ocupa|uci", q):
                return self.recommender.occupancy_alerts(detect_service(question))
            if re.search(r"inventario|stock|medicament|insumo|rotacion", q):
                return self.recommender.stock_alerts(3)
            if re.search(r"espera|urgenc|triage", q):
                return self.recommender.wait_alerts()
            if re.search(r"cirug|quirofan", q):
                return self.recommender.surgery_alerts()
        except Exception as exc:
            log.warning("Recomendaciones contextuales fallaron: %s", exc)
        return []

    # ------------------------------------------------------------------ Plan B
    def _run_intent(self, intent: Intent, question: str) -> AgentResponse:
        resp = intent.handler(self, question)
        resp.engine = resp.engine or "reglas"
        return resp

    def _query(self, sql: str) -> tuple[str, pd.DataFrame]:
        return self.executor.run(sql)

    def _build_intents(self) -> list[Intent]:
        rx = lambda p: re.compile(p)  # noqa: E731
        return [
            Intent("alertas", rx(r"alerta|recomend|sugerenc|que (debo|deberia|hago)|reasign|accion"),
                   HospitalAgent._h_alerts, 5),
            Intent("stock", rx(r"inventario|stock|desabastec|agot|existencia|dias de (inventario|cobertura)"),
                   HospitalAgent._h_stock, 10),
            Intent("rotacion", rx(r"rotacion|mas (consumid|usad|dispensad)|menos (consumid|usad)|"
                                  r"(mayor|menor) consumo|medicament.*(mas|menos)"), HospitalAgent._h_rotation, 15),
            Intent("espera", rx(r"espera|demora|tarda|tiempo.*atencion"), HospitalAgent._h_wait, 20),
            Intent("cirugias", rx(r"cirug|quirofan"), HospitalAgent._h_surgery, 25),
            Intent("ocupacion", rx(r"ocupa|camas?\b.*(libre|disponib)|disponib.*camas?|censo"),
                   HospitalAgent._h_occupancy, 30),
            Intent("servicio_top", rx(r"(servicio|subgrupo|area|unidad).*(mas|mayor).*(paciente|ingres|demanda)|"
                                      r"(mas|mayor).*(paciente|ingres).*(servicio|subgrupo|area)"),
                   HospitalAgent._h_service_top, 35),
            Intent("especialidad", rx(r"especialidad"), HospitalAgent._h_specialty, 40),
            Intent("diagnosticos", rx(r"diagnostic|enfermedad|patologia|causa.*ingreso"),
                   HospitalAgent._h_diagnoses, 45),
            Intent("tendencia", rx(r"tendencia|pico|aument|predic|proyecc|crec"), HospitalAgent._h_trend, 42),
            Intent("demografia", rx(r"genero|sexo|regimen|eps|asegurador|edad|zona|municipio"),
                   HospitalAgent._h_demographics, 55),
            Intent("ingresos", rx(r"cuant[oa]s? (pacientes|ingresos|admisiones)|ingresos por dia"),
                   HospitalAgent._h_admissions, 60),
        ]

    # --- Handlers (cada uno ejecuta SQL real y visible) --------------------------
    def _h_occupancy(self, question: str) -> AgentResponse:
        start, end, label = parse_period(question, self.ref, default="hoy")
        service = detect_service(question)
        where = f"AND servicio = '{service}'" if service else "AND servicio <> 'Urgencias'"
        if start == end:
            sql, df = self._query(f"""
SELECT servicio, subgrupo_cama, camas_ocupadas, capacidad, porcentaje_ocupacion
FROM ocupacion_diaria WHERE fecha = '{end:%Y-%m-%d}' {where}
ORDER BY porcentaje_ocupacion DESC""")
            occ, cap = df["camas_ocupadas"].sum(), df["capacidad"].sum()
            pct = occ / cap * 100 if cap else 0
            scope = f"de {service}" if service else "físicas (sin Urgencias)"
            answer = (f"**{label.capitalize()} ({end:%d/%m/%Y}) hay {fmt_num(occ)} camas {scope} ocupadas de "
                      f"{fmt_num(cap)} disponibles ({fmt_num(pct, 1)} % de ocupación).**")
            if len(df) > 1:
                top = df.iloc[0]
                answer += f" La unidad más cargada es {top.subgrupo_cama.title()} con {fmt_num(top.porcentaje_ocupacion, 1)} %."
            chart = {"type": "bar", "x": "subgrupo_cama", "y": "porcentaje_ocupacion"}
        else:
            sql, df = self._query(f"""
SELECT fecha, servicio, SUM(camas_ocupadas) AS camas_ocupadas, SUM(capacidad) AS capacidad,
       ROUND(100.0 * SUM(camas_ocupadas) / SUM(capacidad), 1) AS porcentaje_ocupacion
FROM ocupacion_diaria WHERE fecha BETWEEN '{start:%Y-%m-%d}' AND '{end:%Y-%m-%d}' {where}
GROUP BY fecha, servicio ORDER BY fecha""")
            answer = (f"Ocupación promedio {('de ' + service) if service else 'por servicio'} en el periodo "
                      f"{label}: {fmt_num(df['porcentaje_ocupacion'].mean(), 1)} %.")
            chart = {"type": "line", "x": "fecha", "y": "porcentaje_ocupacion", "color": "servicio"}
        answer += ("\n\n_Nota: el HIS registra la última cama del episodio; la foto del día es fiable, "
                   "la serie histórica subestima las unidades de paso como UCI._")
        return AgentResponse(question, answer, sql, df, chart=chart,
                             recommendations=self.recommender.occupancy_alerts(service))

    def _h_stock(self, question: str) -> AgentResponse:
        q = normalize(question)
        m = re.search(r"(menos de|menor a|<)\s*(\d{1,3})\s*dias", q)
        days = int(m.group(2)) if m else int(config.STOCK_CRITICAL_DAYS)
        type_filter = "AND tipo_item = 'Medicamento'" if re.search(r"medicament", q) and "insumo" not in q else ""
        sql, df = self._query(f"""
SELECT nombre, tipo_item, stock_actual, consumo_diario_promedio, dias_inventario, categoria_rotacion
FROM inventario_farmacia
WHERE dias_inventario IS NOT NULL AND dias_inventario < {days} {type_filter}
ORDER BY dias_inventario ASC, consumo_diario_promedio DESC""")
        sim = self.conn.execute("SELECT valor FROM metadatos WHERE clave='stock_simulado'").fetchone()[0] == "1"
        answer = f"**{len(df)} ítems tienen menos de {days} días de inventario.**"
        if not df.empty:
            worst = df.iloc[0]
            answer += (f" El más crítico es {worst.nombre.title()[:60]} con {fmt_num(worst.dias_inventario, 1)} "
                       f"días (consume {fmt_num(worst.consumo_diario_promedio, 1)} und/día).")
        if "rotacion" in q:
            low = self.conn.execute("SELECT COUNT(*) FROM inventario_farmacia WHERE categoria_rotacion IN "
                                    "('Baja','Sin movimiento 30 días')").fetchone()[0]
            answer += f" Además, {fmt_num(low)} ítems tienen rotación baja o nula en 30 días (candidatos a no reponer)."
        if sim:
            answer += ("\n\n_El extracto del HIS no incluye existencias: el stock es simulado. El consumo diario "
                       "sí es real. Con el archivo Inventario.txt de farmacia el cálculo pasa a ser real._")
        return AgentResponse(question, answer, sql, df.head(100),
                             chart={"type": "bar", "x": "nombre", "y": "dias_inventario"},
                             recommendations=self.recommender.stock_alerts(3))

    def _h_rotation(self, question: str) -> AgentResponse:
        q = normalize(question)
        start, end, label = parse_period(question, self.ref, default="mes")
        lowest = bool(re.search(r"menos|menor|baja", q))
        item = "AND tipo_item = 'Medicamento'" if "insumo" not in q else "AND tipo_item <> 'Medicamento'"
        sql, df = self._query(f"""
SELECT nombre, tipo_item, SUM(cantidad) AS unidades, COUNT(*) AS dispensaciones
FROM medicamentos_insumos
WHERE fecha_prestacion BETWEEN {lit(start)} AND {lit(end, True)} {item}
GROUP BY codigo, nombre, tipo_item ORDER BY unidades {'ASC' if lowest else 'DESC'} LIMIT 15""")
        kind = "menor" if lowest else "mayor"
        answer = f"Ítems de **{kind} rotación** en el periodo {label}:"
        if not df.empty:
            answer += f" encabeza {df.iloc[0].nombre.title()[:60]} con {fmt_num(df.iloc[0].unidades)} unidades."
        return AgentResponse(question, answer, sql, df, chart={"type": "bar", "x": "nombre", "y": "unidades"})

    def _h_wait(self, question: str) -> AgentResponse:
        start, end, label = parse_period(question, self.ref, default="semana")
        sql, df = self._query(f"""
SELECT COALESCE('Triage ' || nivel_triage, 'Sin triage') AS triage,
       COUNT(*) AS atenciones, ROUND(AVG(tiempo_espera_min), 1) AS espera_promedio_min
FROM ingresos
WHERE via_ingreso = 'Urgencias' AND espera_valida = 1
  AND fecha_ingreso BETWEEN {lit(start)} AND {lit(end, True)}
GROUP BY 1 ORDER BY 1""")
        total = df["atenciones"].sum()
        avg = (df["atenciones"] * df["espera_promedio_min"]).sum() / total if total else None
        answer = (f"**En urgencias, el tiempo promedio entre el ingreso y la primera atención médica fue de "
                  f"{fmt_minutes(avg)}** ({label}: {start:%d/%m} al {end:%d/%m}, {fmt_num(total)} atenciones).")
        if not df.empty:
            worst = df.sort_values("espera_promedio_min", ascending=False).iloc[0]
            answer += f" La mayor espera la tiene {worst.triage} con {fmt_minutes(worst.espera_promedio_min)}."
        return AgentResponse(question, answer, sql, df,
                             chart={"type": "bar", "x": "triage", "y": "espera_promedio_min"},
                             recommendations=self.recommender.wait_alerts())

    def _h_service_top(self, question: str) -> AgentResponse:
        start, end, label = parse_period(question, self.ref, default="mes")
        level = "subgrupo_cama" if re.search(r"subgrupo|unidad|area", normalize(question)) else "servicio"
        sql, df = self._query(f"""
SELECT {level}, COUNT(DISTINCT id_paciente) AS pacientes, COUNT(*) AS ingresos
FROM ingresos WHERE fecha_ingreso BETWEEN {lit(start)} AND {lit(end, True)}
GROUP BY {level} ORDER BY pacientes DESC""")
        answer = "No hay ingresos en ese periodo."
        if not df.empty:
            top = df.iloc[0]
            share = top.pacientes / df["pacientes"].sum() * 100
            answer = (f"**{str(top[level]).title()} es el {('subgrupo' if level != 'servicio' else 'servicio')} con más "
                      f"pacientes ingresados ({label}): {fmt_num(top.pacientes)} pacientes** "
                      f"({fmt_num(share, 1)} % del total).")
            if len(df) > 1:
                answer += f" Le sigue {str(df.iloc[1][level]).title()} con {fmt_num(df.iloc[1].pacientes)}."
            answer += "\n\n_Clasificación según la última cama asignada al episodio._"
        return AgentResponse(question, answer, sql, df, chart={"type": "bar", "x": level, "y": "pacientes"})

    def _h_specialty(self, question: str) -> AgentResponse:
        start, end, label = parse_period(question, self.ref, default="mes")
        sql, df = self._query(f"""
SELECT especialidad, COUNT(*) AS servicios, COUNT(DISTINCT oid_ingreso) AS ingresos_atendidos
FROM servicios WHERE fecha_prestacion BETWEEN {lit(start)} AND {lit(end, True)}
GROUP BY especialidad ORDER BY servicios DESC LIMIT 12""")
        answer = (f"La especialidad más solicitada ({label}) es **{df.iloc[0].especialidad.title()}** con "
                  f"{fmt_num(df.iloc[0].servicios)} servicios." if not df.empty else "Sin datos en el periodo.")
        return AgentResponse(question, answer, sql, df, chart={"type": "bar", "x": "especialidad", "y": "servicios"})

    def _h_diagnoses(self, question: str) -> AgentResponse:
        start, end, label = parse_period(question, self.ref, default="mes")
        sql, df = self._query(f"""
SELECT codigo_diagnostico, nombre_diagnostico, COUNT(*) AS ingresos
FROM ingresos WHERE codigo_diagnostico IS NOT NULL
  AND fecha_ingreso BETWEEN {lit(start)} AND {lit(end, True)}
GROUP BY 1, 2 ORDER BY ingresos DESC LIMIT 10""")
        answer = (f"El diagnóstico más frecuente ({label}) es **{df.iloc[0].nombre_diagnostico.capitalize()}** "
                  f"({df.iloc[0].codigo_diagnostico}) con {fmt_num(df.iloc[0].ingresos)} ingresos."
                  if not df.empty else "Sin datos en el periodo.")
        return AgentResponse(question, answer, sql, df,
                             chart={"type": "bar", "x": "codigo_diagnostico", "y": "ingresos"})

    def _h_surgery(self, question: str) -> AgentResponse:
        start, end, label = parse_period(question, self.ref, default="mes")
        sql, df = self._query(f"""
SELECT estado, COUNT(*) AS programaciones
FROM cirugias WHERE en_periodo = 1 AND fecha_ingreso BETWEEN {lit(start)} AND {lit(end, True)}
GROUP BY estado ORDER BY programaciones DESC""")
        total = df["programaciones"].sum()
        done = df.loc[df["estado"] == "Realizada", "programaciones"].sum()
        answer = (f"**Uso de quirófanos ({label}): {fmt_num(done)} de {fmt_num(total)} cirugías programadas "
                  f"se realizaron ({fmt_num(done / total * 100 if total else 0, 1)} %).**"
                  "\n\n_Se consideran solo programaciones cuyo ingreso está dentro del extracto._")
        return AgentResponse(question, answer, sql, df, chart={"type": "pie", "x": "estado", "y": "programaciones"},
                             recommendations=self.recommender.surgery_alerts())

    def _h_trend(self, question: str) -> AgentResponse:
        r = self.ref
        sql, df = self._query(f"""
SELECT capitulo_cie10,
       SUM(CASE WHEN fecha_ingreso >= '{r - timedelta(days=6):%Y-%m-%d}' THEN 1 ELSE 0 END) AS ultimos_7d,
       ROUND(SUM(CASE WHEN fecha_ingreso < '{r - timedelta(days=6):%Y-%m-%d}' THEN 1 ELSE 0 END) / 4.0, 1)
         AS promedio_semanal_previo,
       ROUND(100.0 * (SUM(CASE WHEN fecha_ingreso >= '{r - timedelta(days=6):%Y-%m-%d}' THEN 1 ELSE 0 END)
         / (SUM(CASE WHEN fecha_ingreso < '{r - timedelta(days=6):%Y-%m-%d}' THEN 1 ELSE 0 END) / 4.0) - 1), 1)
         AS variacion_pct
FROM ingresos
WHERE capitulo_cie10 IS NOT NULL
  AND fecha_ingreso BETWEEN '{r - timedelta(days=34):%Y-%m-%d} 00:00:00' AND '{r:%Y-%m-%d} 23:59:59'
GROUP BY capitulo_cie10 HAVING promedio_semanal_previo >= 5
ORDER BY variacion_pct DESC""")
        up = df[df["variacion_pct"] >= config.DEMAND_SPIKE_PCT]
        answer = ("Comparando los últimos 7 días con el promedio de las 4 semanas previas, "
                  + ("aumentan: " + ", ".join(f"**{x.capitulo_cie10}** (+{fmt_num(x.variacion_pct, 1)} %)"
                                               for x in up.itertuples()) + "." if not up.empty
                     else "ningún grupo de diagnósticos supera el umbral de alza."))
        return AgentResponse(question, answer, sql, df,
                             chart={"type": "bar", "x": "capitulo_cie10", "y": "variacion_pct"},
                             recommendations=self.recommender.demand_alerts())

    def _h_demographics(self, question: str) -> AgentResponse:
        q = normalize(question)
        start, end, label = parse_period(question, self.ref, default="mes")
        col = ("regimen" if "regimen" in q else "asegurador" if re.search(r"eps|asegurador", q)
               else "grupo_etario" if "edad" in q else "zona" if "zona" in q
               else "municipio" if "municipio" in q else "sexo")
        sql, df = self._query(f"""
SELECT p.{col}, COUNT(DISTINCT i.id_paciente) AS pacientes
FROM ingresos i JOIN pacientes p ON p.id_paciente = i.id_paciente
WHERE i.fecha_ingreso BETWEEN {lit(start)} AND {lit(end, True)}
GROUP BY p.{col} ORDER BY pacientes DESC LIMIT 15""")
        answer = f"Distribución de pacientes por {col.replace('_', ' ')} ({label})."
        if not df.empty:
            answer += f" Predomina {df.iloc[0][col]} con {fmt_num(df.iloc[0].pacientes)} pacientes."
        return AgentResponse(question, answer, sql, df,
                             chart={"type": "pie" if len(df) <= 6 else "bar", "x": col, "y": "pacientes"})

    def _h_admissions(self, question: str) -> AgentResponse:
        start, end, label = parse_period(question, self.ref, default="mes")
        sql, df = self._query(f"""
SELECT date(fecha_ingreso) AS fecha, COUNT(*) AS ingresos, COUNT(DISTINCT id_paciente) AS pacientes
FROM ingresos WHERE fecha_ingreso BETWEEN {lit(start)} AND {lit(end, True)}
GROUP BY 1 ORDER BY 1""")
        answer = (f"**{fmt_num(df['ingresos'].sum())} ingresos** en el periodo {label} "
                  f"(promedio {fmt_num(df['ingresos'].mean(), 1)} por día).")
        return AgentResponse(question, answer, sql, df, chart={"type": "line", "x": "fecha", "y": "ingresos"})

    def _h_alerts(self, question: str) -> AgentResponse:
        alerts = self.alerts()
        df = pd.DataFrame([a.to_dict() for a in alerts])
        crit = sum(a.severity == "crítica" for a in alerts)
        answer = (f"Hay **{len(alerts)} alertas activas** ({crit} críticas). Estas son las acciones "
                  "recomendadas en orden de prioridad:")
        return AgentResponse(question, answer, None, df, recommendations=alerts)

    def _help(self, question: str) -> AgentResponse:
        return AgentResponse(
            question,
            "No identifiqué la pregunta en el modo sin LLM. Prueba con: ocupación de camas (por servicio o UCI), "
            "inventario o rotación de medicamentos, tiempos de espera en urgencias, servicio con más ingresos, "
            "diagnósticos, especialidades, cirugías, tendencias de demanda o alertas.", engine="reglas")


# ---------------------------------------------------------------------------
# 7. Selección automática de gráfico para resultados del LLM
# ---------------------------------------------------------------------------
def auto_chart(df: pd.DataFrame | None) -> dict | None:
    if df is None or df.empty or len(df.columns) < 2:
        return None
    numeric = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    text = [c for c in df.columns if c not in numeric]
    if not numeric or not text:
        return None
    x, y = text[0], numeric[-1]
    if re.search(r"fecha|dia|mes|semana", x, re.I):
        return {"type": "line", "x": x, "y": y, "color": text[1] if len(text) > 1 else None}
    if len(df) <= 40:
        return {"type": "bar", "x": x, "y": y}
    return None


KEY_QUESTIONS = [
    "¿Cuántas camas de UCI están ocupadas hoy?",
    "¿Cuáles son los medicamentos con menos de 5 días de inventario o con menor rotación?",
    "¿Cuál es el tiempo de espera promedio en urgencias en la última semana?",
    "¿Qué servicio tiene más pacientes ingresados este mes?",
]

if __name__ == "__main__":  # Demo por consola: python agent.py
    agent = HospitalAgent()
    print(f"Motor: {agent.mode} | LLM: {agent.llm.name if agent.llm else 'ninguno'} | hoy = {agent.ref}")
    for q in KEY_QUESTIONS:
        r = agent.ask(q)
        print(f"\n❓ {q}\n🤖 [{r.engine}, {r.elapsed_ms} ms] {r.answer}")
        if r.data is not None:
            print(r.data.head(5).to_string(index=False))
