"""
config.py — Configuración central del MVP (Hackatón FUP · Hospital Susana López de Valencia).

Todas las credenciales y parámetros sensibles se leen desde el archivo `.env`
(nunca se versiona). Los valores por defecto permiten ejecutar la demo sin API key:
en ese caso el agente trabaja 100 % con el Plan B (reglas + SQL validado).
"""
from __future__ import annotations

import os
from pathlib import Path

try:  # python-dotenv es opcional en tiempo de ejecución
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

BASE_DIR = Path(__file__).resolve().parent
if load_dotenv:
    load_dotenv(BASE_DIR / ".env")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


# --- Rutas -----------------------------------------------------------------
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "Datos"))
DB_PATH = Path(os.getenv("DB_PATH", BASE_DIR / "hospital.db"))

# --- Fecha de referencia ("hoy") -------------------------------------------
# Los datos son un extracto histórico. Si no se define, "hoy" = última fecha de
# ingreso presente en los datos. Formato: YYYY-MM-DD
REFERENCE_DATE = os.getenv("REFERENCE_DATE", "").strip() or None

# --- Proveedor LLM (Factory) -------------------------------------------------
# Valores: openai | anthropic | ollama | none
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "none").strip().lower()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "sqlcoder")
LLM_TIMEOUT_SECONDS = _env_float("LLM_TIMEOUT_SECONDS", 30)
# Si es "true", el LLM redacta la respuesta en lenguaje natural a partir del resultado.
LLM_NARRATIVE = os.getenv("LLM_NARRATIVE", "true").lower() == "true"

# --- Contacto -----------------------------------------------------------------
# WhatsApp de facturación/admisiones (ej. 573001234567). Vacío = el portal no muestra el botón de WhatsApp.
HOSPITAL_WHATSAPP = os.getenv("HOSPITAL_WHATSAPP", "").strip()

# --- Seguridad de consultas --------------------------------------------------
SQL_ROW_LIMIT = int(_env_float("SQL_ROW_LIMIT", 500))
SQL_TIMEOUT_SECONDS = _env_float("SQL_TIMEOUT_SECONDS", 10)

# --- Umbrales de negocio (alertas y recomendaciones) --------------------------
OCCUPANCY_WARNING_PCT = _env_float("OCCUPANCY_WARNING_PCT", 85)   # ocupación alta
OCCUPANCY_CRITICAL_PCT = _env_float("OCCUPANCY_CRITICAL_PCT", 95)  # sobreocupación
OCCUPANCY_LOW_PCT = _env_float("OCCUPANCY_LOW_PCT", 60)            # servicio con holgura
STOCK_CRITICAL_DAYS = _env_float("STOCK_CRITICAL_DAYS", 5)         # días de inventario
DEMAND_SPIKE_PCT = _env_float("DEMAND_SPIKE_PCT", 20)              # alza vs. línea base
WAIT_TARGET_TRIAGE2_MIN = _env_float("WAIT_TARGET_TRIAGE2_MIN", 30)  # Res. 5596/2015
SURGERY_COMPLIANCE_MIN_PCT = _env_float("SURGERY_COMPLIANCE_MIN_PCT", 90)
# Esperas fuera de este rango se consideran errores de registro y no se promedian.
WAIT_MAX_VALID_MIN = _env_float("WAIT_MAX_VALID_MIN", 1440)
