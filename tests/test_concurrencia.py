"""Varios usuarios preguntándole al agente a la vez (el agente es compartido por toda la app de Streamlit).

Antes, el authorizer y el tiempo máximo del SQL ejecutaban Python dentro de SQLite sobre una conexión que usaban
todos los hilos: con 10 usuarios simultáneos el proceso se congelaba. Esta prueba falla si vuelve a pasar."""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import HospitalAgent  # noqa: E402

QUESTIONS = ["¿Cuántas camas de UCI están ocupadas hoy?",
             "¿Cuáles son los medicamentos con menos de 5 días de inventario?",
             "¿Cuál es el tiempo de espera promedio en urgencias en la última semana?",
             "¿Qué servicio tiene más pacientes ingresados este mes?"]


def test_diez_usuarios_a_la_vez_no_congelan_el_agente():
    agent = HospitalAgent(provider="none", mode="rules")
    results, errors = [], []

    def user(k):
        for i in range(5):
            try:
                results.append(agent.ask(QUESTIONS[(k + i) % 4]).engine)
                agent.alerts()
            except Exception as exc:  # pragma: no cover - solo si algo falla
                errors.append(repr(exc))

    threads = [threading.Thread(target=user, args=(k,), daemon=True) for k in range(10)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "El agente se congeló con usuarios simultáneos"
    assert not errors and len(results) == 50 and set(results) == {"reglas"}
    assert time.time() - t0 < 60


def test_el_sql_de_la_ia_sigue_bloqueado_y_la_conexion_es_de_solo_lectura():
    agent = HospitalAgent(provider="none", mode="rules")
    assert agent.ask("DROP TABLE ingresos").engine == "seguridad"
    try:
        agent.conn.execute("DELETE FROM camas")
        raise AssertionError("la conexión del agente permitió escribir")
    except Exception as exc:
        assert "readonly" in str(exc)


# --- Procedimientos por nombre (Plan B) -------------------------------------------------------------
def test_procedimiento_por_nombre_con_grafica_y_sin_inventar():
    agent = HospitalAgent(provider="none", mode="rules")
    r = agent.ask("pacientes atendidos por apendicectomía")
    assert r.data is not None and r.chart["x"] == "mes" and "pacientes" in r.answer
    assert "no registra" not in r.answer
    c = agent.ask("grafica de pacientes con cesaria")                   # error de escritura común
    assert "entendí «cesarea»" in c.answer and c.data is not None
    v = agent.ask("generame una grafica de los pacientes atendidos por basectomia")
    assert "no registra ningún procedimiento" in v.answer and v.data is None   # no existe en el extracto
    assert agent.ask("¿Cuántas camas de UCI están ocupadas hoy?").answer.startswith("**Hoy")


def test_cuantas_personas_atendidas_en_urgencias_sin_ia():
    agent = HospitalAgent(provider="none", mode="rules")
    r = agent.ask("cuantas personas son atendidas en urgencias")
    assert "personas llegaron por urgencias" in r.answer and r.data is not None
    assert "personas" in agent.ask("cuánta gente ingresó a pediatría esta semana").answer
