"""
ml_services.py — Cliente de los microservicios predictivos (microservicios/service_*), con tolerancia a fallos.

  * Tiempo máximo por llamada (por defecto 2 s): una caída o lentitud de un servicio nunca bloquea la app.
  * Circuit breaker simple: si un servicio falla, se deja de llamar durante COOLDOWN_S segundos y se responde
    "no disponible" al instante; pasado ese tiempo se vuelve a intentar.
  * Aislamiento: cada servicio se consulta por separado; si Quirófanos cae, Urgencias y Farmacia siguen.

Las URL salen del .env (URGENCIAS_URL, QUIROFANOS_URL, FARMACIA_URL, CONSULTAS_URL) con los mismos valores
por defecto que microservicios/run_services.py. No depende de Streamlit.
"""
from __future__ import annotations

import os
import re
import time
import unicodedata
from dataclasses import dataclass, field

import requests

TIMEOUT_S = float(os.getenv("ML_TIMEOUT_SECONDS", "2"))
COOLDOWN_S = 30.0


@dataclass(frozen=True)
class Service:
    code: str
    name: str
    icon: str
    env: str
    port: int
    keywords: tuple[str, ...] = field(default_factory=tuple)

    @property
    def url(self) -> str:
        value = (os.getenv(self.env) or "").strip()
        if value.isdigit():
            return f"http://127.0.0.1:{value}"
        return (value if "://" in value else f"http://{value}") if value else f"http://127.0.0.1:{self.port}"


SERVICES = [
    Service("urgencias", "Urgencias", "🚑", "URGENCIAS_URL", 5001, ("urgencia", "triage", "emergencia")),
    Service("quirofanos", "Quirófanos", "🩻", "QUIROFANOS_URL", 5002, ("cirug", "quirofan")),
    Service("farmacia", "Farmacia", "💊", "FARMACIA_URL", 5003, ("farmac", "medicament", "insumo", "dispensac")),
    Service("consultas", "Consulta externa", "🩺", "CONSULTAS_URL", 5004, ("consulta", "ambulatori", "medicina general")),
]
BY_CODE = {s.code: s for s in SERVICES}
_open_until: dict[str, float] = {}


class ServiceUnavailable(RuntimeError):
    pass


def _call(service: Service, method: str, path: str, *, json: dict | None = None, timeout: float | None = None,
          raw: bool = False):
    now = time.monotonic()
    if _open_until.get(service.code, 0) > now:
        raise ServiceUnavailable(f"{service.name}: servicio no disponible (reintento en "
                                 f"{int(_open_until[service.code] - now)} s)")
    try:
        resp = requests.request(method, service.url + path, json=json, timeout=timeout or TIMEOUT_S)
        if resp.status_code >= 500:
            raise ServiceUnavailable(f"{service.name}: respondió {resp.status_code}")
        resp.raise_for_status()
    except ServiceUnavailable:
        _open_until[service.code] = time.monotonic() + COOLDOWN_S
        raise
    except requests.RequestException as exc:
        _open_until[service.code] = time.monotonic() + COOLDOWN_S
        raise ServiceUnavailable(f"{service.name}: no responde ({type(exc).__name__})") from exc
    _open_until.pop(service.code, None)
    return resp.content if raw else resp.json()


def health(code: str) -> dict:
    return _call(BY_CODE[code], "GET", "/health")


def kpis(code: str) -> dict:
    return _call(BY_CODE[code], "GET", "/kpis")


def predict(code: str, payload: dict | None = None) -> dict:
    return _call(BY_CODE[code], "POST", "/predict", json=payload or {})


def report(code: str) -> bytes:
    return _call(BY_CODE[code], "GET", "/reporte.xlsx", timeout=max(TIMEOUT_S, 15), raw=True)


def is_down(code: str) -> bool:
    """True si el servicio falló hace menos de COOLDOWN_S segundos."""
    return _open_until.get(code, 0) > time.monotonic()


def reset_breakers() -> None:
    _open_until.clear()


# ---------------------------------------------------------------------------
# Despacho desde el asistente: preguntas de pronóstico -> microservicio
# ---------------------------------------------------------------------------
_FORECAST = re.compile(r"pronostic|predic|proyecc|se espera|esperad[oa]s?|manana|proxim[oa]s? dias?|cuant[oa]s? .*(habra|tendremos|vendran|llegaran)")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text).lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def route(question: str) -> Service | None:
    """Servicio al que va una pregunta de pronóstico ('¿cuántos ingresos a urgencias se esperan mañana?')."""
    q = _norm(question)
    if not _FORECAST.search(q):
        return None
    return next((s for s in SERVICES if any(k in q for k in s.keywords)), None)


def _err(v) -> str:
    return "—" if v is None else f"{v:.1f}".replace(".", ",")


def describe(service: Service, pred: dict) -> str:
    """Respuesta en lenguaje natural a partir del contrato /predict."""
    m = pred.get("metricas", {})
    lo, hi = (pred.get("intervalo") or [None, None])[:2]
    value = pred.get("prediccion")
    fmt = (lambda v: f"{v:,.0f}".replace(",", ".")) if (value or 0) >= 20 else (lambda v: f"{v:.1f}".replace(".", ","))
    text = (f"**{service.name}** · {pred.get('objetivo', 'pronóstico')} para el **{pred.get('fecha_objetivo')}**: "
            f"**{fmt(value)}** (rango probable {fmt(lo)} a {fmt(hi)}).")
    if m.get("supera_baseline"):
        text += (f"\n\nEl modelo mejora a la referencia simple: error medio {_err(m.get('mae'))} frente a "
                 f"{_err(m.get('mae_baseline'))} de repetir la semana anterior.")
    else:
        text += (f"\n\n⚠️ En la validación el modelo **no supera** a repetir la semana anterior (error {_err(m.get('mae'))} "
                 f"frente a {_err(m.get('mae_baseline'))}): úsalo como orientación, no para decidir.")
    return text
