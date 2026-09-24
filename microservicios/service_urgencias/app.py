"""
app.py — Microservicio de Urgencias (Flask). Puerto por defecto 5001 (URGENCIAS_URL o PORT).

    GET  /health        estado del servicio y del modelo
    GET  /kpis          indicadores operativos de urgencias
    POST /predict       {"fecha": "YYYY-MM-DD" (opcional), "nivel_triage": 1-5 (opcional)}
    GET  /reporte.xlsx  reporte de tres hojas

Ejecución directa: python microservicios/service_urgencias/app.py
"""
from pathlib import Path
import sys
import os

# Agregar la raíz del repositorio a sys.path
ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
# Fallback a variable de entorno si config.DB_PATH no estuviera disponible:
DB_PATH = os.getenv("DB_PATH", str(getattr(config, "DB_PATH", ROOT_DIR / "hospital.db")))

# model.py y reporte.py de ESTA carpeta tienen prioridad sobre cualquier módulo homónimo de la raíz
SERVICE_DIR = str(Path(__file__).resolve().parent)
if sys.path[0] != SERVICE_DIR:
    sys.path.insert(0, SERVICE_DIR)

import io  # noqa: E402
import logging  # noqa: E402

from flask import Flask, request, send_file  # noqa: E402

from microservicios import comun  # noqa: E402
from model import PUERTO_POR_DEFECTO, SERVICIO, VAR_URL, ModeloUrgencias  # noqa: E402
from reporte import generar_reporte  # noqa: E402

logging.basicConfig(level=logging.INFO, format=f"%(asctime)s | {SERVICIO} | %(levelname)s | %(message)s")

dominio = ModeloUrgencias(DB_PATH)
app = Flask(__name__)
comun.registrar_manejadores(app)


@app.get("/health")
def health():
    try:
        fecha_ref = dominio.contexto().fecha_referencia.date().isoformat()
    except Exception as exc:  # base ausente o ilegible: el servicio responde, pero degradado
        return comun.respuesta_json({"servicio": SERVICIO, "estado": "sin_datos", "modelo_entrenado": False,
                                     "fecha_referencia": None, "detalle": str(exc)}, 503)
    respuesta = {"servicio": SERVICIO, "estado": "ok", "modelo_entrenado": dominio.modelo_entrenado(),
                 "fecha_referencia": fecha_ref}
    if dominio.error_entrenamiento:
        respuesta["detalle"] = dominio.error_entrenamiento
    return comun.respuesta_json(respuesta)


@app.get("/kpis")
def kpis():
    return comun.respuesta_json(dominio.kpis())


@app.post("/predict")
def predict():
    return comun.respuesta_json(dominio.predecir(comun.leer_json(request)))


@app.get("/reporte.xlsx")
def reporte():
    contenido = generar_reporte(dominio)
    fecha = dominio.contexto().fecha_referencia
    return send_file(io.BytesIO(contenido), as_attachment=True,
                     download_name=f"reporte_{SERVICIO}_{fecha:%Y-%m-%d}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


if __name__ == "__main__":
    dominio.iniciar_entrenamiento()
    host = os.getenv("HOST", "127.0.0.1")  # solo local por defecto: datos clínicos agregados
    port = comun.resolver_puerto(VAR_URL, PUERTO_POR_DEFECTO)
    # Sin reloader: el reloader crea un segundo proceso que puede dejar el puerto ocupado.
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
