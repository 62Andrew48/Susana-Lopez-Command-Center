"""
reporte.py — Urgencias: reporte .xlsx con tres hojas (datos agregados, sin identificadores).

  1. Historico clasificado · ingresos diarios por triage y clasificación del día (Baja/Normal/Alta/Pico)
  2. Consumo o uso        · uso por turno y triage, por capítulo CIE-10 y perfil por día de la semana
  3. Prediccion           · métricas de validación, pronóstico a 7 días y comparación real vs. modelo vs. base
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

import pandas as pd  # noqa: E402

from microservicios import comun  # noqa: E402


def _historico(dominio) -> pd.DataFrame:
    ctx = dominio.contexto()
    d = ctx.datos
    total = comun.reindexar_diario(d.groupby("fecha")["ingresos"].sum(), ctx.inicio_datos, ctx.fecha_referencia)
    clasificacion = comun.clasificar_nivel(total)
    tabla = dominio.resumen_espera(d, ["fecha", "nivel_triage"])
    tabla["triage"] = tabla["nivel_triage"].map(lambda n: f"Triage {int(n)}" if n else "Sin triage")
    tabla["dia_semana"] = tabla["fecha"].dt.dayofweek.map(lambda i: comun.DIAS_SEMANA[i])
    tabla["festivo"] = comun.es_festivo(tabla["fecha"])
    tabla["ingresos_dia_total"] = tabla["fecha"].map(total)
    tabla["clasificacion_dia"] = tabla["fecha"].map(clasificacion)
    return (tabla[["fecha", "dia_semana", "festivo", "triage", "ingresos", "espera_promedio_min",
                   "pct_atendidos_en_30_min", "ingresos_dia_total", "clasificacion_dia"]]
            .sort_values(["fecha", "triage"], ascending=[False, True], ignore_index=True))


def _uso(dominio) -> list[comun.Bloque]:
    ctx = dominio.contexto()
    d = ctx.datos
    v = dominio.ventanas()
    turno_triage = dominio.resumen_espera(d, ["turno", "nivel_triage"])
    turno_triage.insert(1, "triage", turno_triage.pop("nivel_triage").map(lambda n: f"Triage {int(n)}" if n else "Sin triage"))

    ultimos = d[d["fecha"].between(*v["ultimos_28d"])]
    previos = d[d["fecha"].between(ctx.fecha_referencia - pd.Timedelta(days=55), v["ultimos_28d"][0] - pd.Timedelta(days=1))]
    cap = (ultimos.groupby("capitulo_cie10")["ingresos"].sum().rename("ingresos_ultimos_28d").to_frame()
           .join(previos.groupby("capitulo_cie10")["ingresos"].sum().rename("ingresos_28d_previos"), how="outer")
           .fillna(0).reset_index())
    cap["variacion_pct"] = [comun.variacion_pct(a, b) for a, b in zip(cap["ingresos_ultimos_28d"], cap["ingresos_28d_previos"])]
    cap = cap.sort_values("ingresos_ultimos_28d", ascending=False, ignore_index=True)

    diario = comun.reindexar_diario(d.groupby("fecha")["ingresos"].sum(), ctx.inicio_datos, ctx.fin_modelo)
    perfil = (diario.groupby(diario.index.dayofweek).agg(["mean", "median", "max"]).round(1)
              .rename(columns={"mean": "promedio_ingresos", "median": "mediana_ingresos", "max": "maximo_ingresos"}))
    perfil.insert(0, "dia_semana", [comun.DIAS_SEMANA[i] for i in perfil.index])
    return [
        ("Uso de urgencias por turno y triage (todo el periodo)", turno_triage),
        ("Ingresos por capítulo CIE-10: últimos 28 días vs. 28 días previos", cap),
        ("Perfil de demanda por día de la semana (días completos)", perfil.reset_index(drop=True)),
    ]


def _prediccion(dominio) -> list[comun.Bloque]:
    modelo = dominio.modelo({})
    ctx = dominio.contexto()
    pronostico = modelo.predecir_hasta(ctx.fin_modelo + pd.Timedelta(days=7))
    notas = pd.DataFrame({"nota": ctx.advertencias + modelo.advertencias or ["Sin advertencias."]})
    return [
        ("Validación del modelo (partición temporal estricta)", dominio.tabla_metricas(modelo, dominio.describir_objetivo({}))),
        ("Pronóstico de ingresos por urgencias · próximos 7 días", pronostico),
        ("Validación día a día: real vs. modelo vs. línea base", modelo.backtest),
        ("Importancia de variables del modelo", modelo.importancias),
        ("Advertencias", notas),
    ]


def generar_reporte(dominio) -> bytes:
    return comun.construir_excel({
        "Historico clasificado": [("Ingresos diarios por urgencias clasificados por triage y nivel de demanda",
                                   _historico(dominio))],
        "Consumo o uso": _uso(dominio),
        "Prediccion": _prediccion(dominio),
    })
