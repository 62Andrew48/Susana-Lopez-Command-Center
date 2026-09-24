"""
reporte.py — Quirófanos: reporte .xlsx con tres hojas (sin identificadores).

  1. Historico clasificado · cirugías realizadas por día y área, con clasificación del día
  2. Consumo o uso        · uso promedio por área y día de la semana, y estado de la programación
  3. Prediccion           · validación, pronóstico a 7 días total y por área
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
    d = ctx.datos.cirugias.copy()
    total = comun.reindexar_diario(d.groupby("fecha")["cirugias"].sum(), ctx.inicio_datos, ctx.fecha_referencia)
    clasificacion = comun.clasificar_nivel(total)
    d["dia_semana"] = d["fecha"].dt.dayofweek.map(lambda i: comun.DIAS_SEMANA[i])
    d["festivo"] = comun.es_festivo(d["fecha"])
    d["cirugias_dia_total"] = d["fecha"].map(total)
    d["clasificacion_dia"] = d["fecha"].map(clasificacion)
    return (d[["fecha", "dia_semana", "festivo", "area_quirofano", "cirugias", "cirugias_dia_total", "clasificacion_dia"]]
            .sort_values(["fecha", "area_quirofano"], ascending=[False, True], ignore_index=True))


def _uso(dominio) -> list[comun.Bloque]:
    ctx = dominio.contexto()
    d = ctx.datos.cirugias
    filas = []
    for area in dominio.areas():
        serie = comun.reindexar_diario(d[d["area_quirofano"] == area].groupby("fecha")["cirugias"].sum(),
                                       ctx.inicio_modelo, ctx.fin_modelo)
        promedio = serie.groupby(serie.index.dayofweek).mean().round(2)
        filas.append({"area_quirofano": area, **{comun.DIAS_SEMANA[i]: promedio.get(i, 0.0) for i in range(7)},
                      "promedio_diario": round(serie.mean(), 2), "total_periodo": int(serie.sum())})
    estados = ctx.datos.estados.copy()
    estados["participacion_pct"] = (estados["programaciones"] / estados["programaciones"].sum() * 100).round(1)
    return [
        ("Cirugías promedio por área y día de la semana (días completos del periodo modelado)", pd.DataFrame(filas)),
        ("Estado de las programaciones con ingreso dentro del extracto", estados),
        ("Nota sobre la fuente", pd.DataFrame({"nota": [dominio.nota_fuente]})),
    ]


def _prediccion(dominio) -> list[comun.Bloque]:
    ctx = dominio.contexto()
    modelo = dominio.modelo({})
    horizonte = ctx.fin_modelo + pd.Timedelta(days=7)
    por_area = []
    for area in dominio.areas():
        try:
            m = dominio.modelo({"area_quirofano": area})
            tabla = m.predecir_hasta(horizonte).assign(area_quirofano=area,
                                                       supera_linea_base="Sí" if m.supera_baseline else "NO")
            por_area.append(tabla)
        except (comun.DatosInsuficientes, comun.NoEncontrado):
            continue
    por_area_df = pd.concat(por_area, ignore_index=True) if por_area else pd.DataFrame()
    notas = ctx.advertencias + modelo.advertencias + ["La suma por áreas puede diferir del total: cada serie "
                                                      "se modela por separado."]
    return [
        ("Validación del modelo (partición temporal estricta)", dominio.tabla_metricas(modelo, dominio.describir_objetivo({}))),
        ("Pronóstico de cirugías realizadas · próximos 7 días (todas las áreas)", modelo.predecir_hasta(horizonte)),
        ("Pronóstico por área quirúrgica · próximos 7 días", por_area_df),
        ("Validación día a día: real vs. modelo vs. línea base", modelo.backtest),
        ("Advertencias", pd.DataFrame({"nota": notas})),
    ]


def generar_reporte(dominio) -> bytes:
    return comun.construir_excel({
        "Historico clasificado": [("Cirugías realizadas por día y área quirúrgica", _historico(dominio))],
        "Consumo o uso": _uso(dominio),
        "Prediccion": _prediccion(dominio),
    })
