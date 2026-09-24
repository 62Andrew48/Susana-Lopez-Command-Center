"""Cliente de microservicios: enrutamiento, respaldo y circuit breaker (sin levantar los servicios)."""
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ml_services as ml  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    ml.reset_breakers()
    yield
    ml.reset_breakers()


@pytest.mark.parametrize("question, code", [
    ("¿Cuántos ingresos a urgencias se esperan mañana?", "urgencias"),
    ("pronóstico de cirugías para los próximos días", "quirofanos"),
    ("¿Qué demanda de medicamentos se espera en farmacia?", "farmacia"),
    ("predicción de consultas ambulatorias", "consultas"),
    ("¿Cuántas camas de UCI están ocupadas hoy?", None),
    ("¿Cuál fue la espera en urgencias la última semana?", None),
])
def test_route_only_forecast_questions(question, code):
    svc = ml.route(question)
    assert (svc.code if svc else None) == code


def test_describe_warns_when_model_does_not_beat_baseline():
    pred = {"prediccion": 112.1, "intervalo": [62.6, 161.6], "objetivo": "Ingresos diarios por urgencias",
            "fecha_objetivo": "2026-09-22", "metricas": {"mae": 14.85, "mae_baseline": 14.61, "supera_baseline": False}}
    text = ml.describe(ml.BY_CODE["urgencias"], pred)
    assert "112" in text and "no supera" in text


def test_breaker_opens_after_failure_and_skips_calls(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise requests.ConnectionError("apagado")

    monkeypatch.setattr(ml.requests, "request", boom)
    with pytest.raises(ml.ServiceUnavailable):
        ml.predict("quirofanos")
    with pytest.raises(ml.ServiceUnavailable):
        ml.predict("quirofanos")  # circuito abierto: no vuelve a llamar
    assert len(calls) == 1 and ml.is_down("quirofanos") and not ml.is_down("urgencias")


def test_assistant_falls_back_to_agent_when_service_is_down(monkeypatch):
    from agent import AgentResponse
    from ui.assistant_scope import SCOPES, answer
    monkeypatch.setattr(ml.requests, "request", lambda *a, **k: (_ for _ in ()).throw(requests.Timeout()))

    class Agent:
        def ask(self, q):
            return AgentResponse(q, "Respuesta del histórico", engine="reglas")

    resp = answer("¿Cuántos ingresos a urgencias se esperan mañana?", SCOPES["completo"], agent=Agent(),
                  forecasts=True)
    assert "no está disponible" in resp.answer and "Respuesta del histórico" in resp.answer


def test_forecasts_disabled_without_permission(monkeypatch):
    from agent import AgentResponse
    from ui.assistant_scope import SCOPES, answer
    monkeypatch.setattr(ml.requests, "request", lambda *a, **k: pytest.fail("no debe llamar al microservicio"))

    class Agent:
        def ask(self, q):
            return AgentResponse(q, "ok", engine="reglas")

    assert answer("¿Cuántos ingresos a urgencias se esperan mañana?", SCOPES["completo"], agent=Agent()).answer == "ok"
