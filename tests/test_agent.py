"""Pruebas del agente. Ejecutar: python -m pytest -q   (requiere haber corrido database.py)"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent as ag  # noqa: E402
import database as db  # noqa: E402


@pytest.fixture(scope="module")
def rules_agent():
    return ag.HospitalAgent(mode="rules")


# --- Sanitizador ---------------------------------------------------------------
@pytest.mark.parametrize("sql", [
    "DROP TABLE ingresos",
    "SELECT 1; DROP TABLE ingresos",
    "DELETE FROM pacientes",
    "UPDATE inventario_farmacia SET stock_actual = 0",
    "INSERT INTO camas VALUES (1)",
    "PRAGMA table_info(ingresos)",
    "ATTACH DATABASE 'x.db' AS x",
    "SELECT * FROM sqlite_master",
    "WITH x AS (SELECT 1) DELETE FROM camas",
    "SELECT 1 /* */ ; VACUUM",
    "SELECT nombre_paciente FROM pacientes",
])
def test_guard_blocks_dangerous_sql(sql):
    with pytest.raises(ag.UnsafeQueryError):
        ag.SQLGuard.sanitize(sql)


def test_guard_allows_keywords_inside_literals():
    out = ag.SQLGuard.sanitize("SELECT nombre FROM inventario_farmacia WHERE nombre LIKE '%DELETE%'")
    assert out.startswith("SELECT * FROM (")


def test_guard_strips_markdown_and_adds_limit():
    out = ag.SQLGuard.sanitize("```sql\nSELECT servicio FROM ingresos;\n```", row_limit=10)
    assert out.endswith("LIMIT 10") and ";" not in out


def test_readonly_authorizer_blocks_writes_even_if_guard_is_bypassed():
    ex = ag.QueryExecutor()
    with pytest.raises(sqlite3.DatabaseError):
        ex.conn.execute("DELETE FROM camas")


def test_identifiers_never_returned():
    ex = ag.QueryExecutor()
    _, df = ex.run("SELECT id_paciente, oid_ingreso, servicio FROM ingresos LIMIT 5")
    assert list(df.columns) == ["servicio"]


def test_cte_queries_work():
    ex = ag.QueryExecutor()
    _, df = ex.run("WITH t AS (SELECT servicio, COUNT(*) n FROM ingresos GROUP BY 1) SELECT * FROM t ORDER BY n DESC")
    assert len(df) > 0


# --- Plan B: las 4 preguntas del reto -------------------------------------------
@pytest.mark.parametrize("question,intent", [
    (ag.KEY_QUESTIONS[0], "ocupacion"),
    (ag.KEY_QUESTIONS[1], "stock"),
    (ag.KEY_QUESTIONS[2], "espera"),
    (ag.KEY_QUESTIONS[3], "servicio_top"),
    ("¿Qué medicamentos tienen mayor rotación?", "rotacion"),
    ("¿Cuántas cirugías se realizaron vs programadas?", "cirugias"),
    ("Dame las alertas y recomendaciones", "alertas"),
    ("¿Qué especialidades son más solicitadas?", "especialidad"),
])
def test_intent_routing(rules_agent, question, intent):
    assert rules_agent.match_intent(question).name == intent


@pytest.mark.parametrize("question", ag.KEY_QUESTIONS)
def test_key_questions_return_data(rules_agent, question):
    resp = rules_agent.ask(question)
    assert resp.error is None
    assert resp.sql and resp.sql.startswith("SELECT * FROM (")
    assert resp.data is not None and not resp.data.empty
    assert not (set(c.lower() for c in resp.data.columns) & ag.IDENTIFIER_COLUMNS)


def test_period_parser():
    from datetime import date
    ref = date(2026, 9, 21)
    assert ag.parse_period("última semana", ref)[:2] == (date(2026, 9, 15), ref)
    assert ag.parse_period("este mes", ref)[:2] == (date(2026, 9, 1), ref)
    assert ag.parse_period("últimos 3 días", ref)[:2] == (date(2026, 9, 19), ref)
    assert ag.parse_period("hoy", ref)[:2] == (ref, ref)


def test_alert_engine_produces_prioritized_alerts(rules_agent):
    alerts = rules_agent.alerts()
    assert alerts, "Debe existir al menos una alerta con los datos del reto"
    order = [ag.RecommendationEngine.SEVERITY_ORDER[a.severity] for a in alerts]
    assert order == sorted(order)


# --- Flujo LLM con cliente simulado ----------------------------------------------
class FakeLLM(ag.LLMClient):
    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)

    def complete(self, system, messages):
        return self.replies.pop(0)


def test_llm_path_with_self_correction():
    a = ag.HospitalAgent(mode="llm_first")
    a.llm = FakeLLM([
        "```sql\nSELECT servicio, COUNT(*) AS n FROM ingresoz GROUP BY 1\n```",   # error: tabla inexistente
        "```sql\nSELECT servicio, COUNT(*) AS n FROM ingresos GROUP BY 1\n```",   # corrección
        "Urgencias concentra la mayor cantidad de ingresos.",                       # narrativa
    ])
    resp = a.ask("¿cuántos ingresos hay por servicio?")
    assert resp.engine == "llm" and not resp.data.empty and "Urgencias" in resp.answer


def test_llm_malicious_sql_is_blocked():
    a = ag.HospitalAgent(mode="llm_first")
    a.llm = FakeLLM(["```sql\nDROP TABLE ingresos\n```"])
    resp = a.ask("borra todo")
    assert resp.engine == "seguridad"
    assert db.get_connection(read_only=True).execute("SELECT COUNT(*) FROM ingresos").fetchone()[0] > 0


def test_llm_failure_falls_back_to_rules():
    class Broken(ag.LLMClient):
        def complete(self, system, messages):
            raise TimeoutError("sin red")
    a = ag.HospitalAgent(mode="llm_first")
    a.llm = Broken()
    resp = a.ask(ag.KEY_QUESTIONS[0])
    assert resp.engine == "reglas (respaldo)" and not resp.data.empty


@pytest.mark.parametrize("text", ["DROP TABLE ingresos", "borra la tabla de ingresos",
                                  "elimina los registros de pacientes", "UPDATE camas SET es_virtual=1"])
def test_destructive_requests_are_blocked_in_any_mode(rules_agent, text):
    assert rules_agent.ask(text).engine == "seguridad"


def test_raw_select_is_allowed(rules_agent):
    resp = rules_agent.ask("SELECT servicio, COUNT(*) AS n FROM ingresos GROUP BY servicio")
    assert resp.engine == "sql directo" and len(resp.data) > 0


def test_analytical_question_with_destructive_word_is_not_blocked(rules_agent):
    resp = rules_agent.ask("¿Qué medicamentos conviene eliminar del inventario por baja rotación?")
    assert resp.engine != "seguridad"
