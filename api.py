"""
api.py — Backend REST (FastAPI) con los endpoints mínimos del reto.

    uvicorn api:app --reload --port 8000     ->  documentación automática en http://localhost:8000/docs

El dashboard de Streamlit usa los mismos servicios en proceso (sin HTTP) para simplificar la demo;
esta API permite integrar el agente con otros sistemas del hospital (HIS, intranet, bots).
"""
from __future__ import annotations

from contextlib import closing
from datetime import date

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import database as db
from agent import HospitalAgent

db.ensure_database()
agent = HospitalAgent()

app = FastAPI(title="HSLV · Agente IA de gestión hospitalaria", version="1.0.0",
              description="NL2SQL + KPIs hospitalarios + alertas (Hackatón FUP 2026)")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=500,
                          examples=["¿Cuántas camas de UCI están ocupadas hoy?"])


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "reference_date": agent.ref.isoformat(), "engine": agent.mode,
            "llm": agent.llm.name if agent.llm else None}


@app.post("/api/query")
def query(req: QueryRequest) -> dict:
    """Pregunta en lenguaje natural -> SQL validado -> resultado JSON."""
    resp = agent.ask(req.question)
    if resp.engine == "seguridad":
        raise HTTPException(status_code=400, detail=resp.answer)
    return resp.to_dict()


@app.get("/api/kpis")
def kpis(start: date | None = None, end: date | None = None) -> dict:
    """KPIs precalculados para el dashboard (por defecto: mes en curso)."""
    with closing(db.get_connection(read_only=True)) as conn:
        return db.compute_kpis(conn, start, end)


@app.get("/api/alerts")
def alerts() -> list[dict]:
    """Alertas proactivas priorizadas con acción recomendada."""
    return [a.to_dict() for a in agent.alerts()]
