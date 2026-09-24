"""
reporte.py — Farmacia: reporte .xlsx con tres hojas (sin identificadores de pacientes ni ingresos).

  1. Historico clasificado · cada ítem con clase ABC (por frecuencia de dispensación) y rotación
  2. Consumo o uso        · demanda diaria por tipo de ítem y top 20 de los últimos 30 días
  3. Prediccion           · validación del modelo general, pronóstico a 7 días y cobertura proyectada
                            de los ítems clase A con más movimiento (stock simulado, marcado)
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

ITEMS_PRONOSTICO = 10


def _historico(dominio) -> pd.DataFrame:
    tabla = dominio.clasificacion_items()
    columnas = ["codigo", "nombre", "tipo_item", "clase_abc", "categoria_rotacion", "dispensaciones_periodo",
                "unidades_periodo", "participacion_pct", "participacion_acumulada_pct", "dias_con_movimiento",
                "frecuencia_dias_pct", "ultima_dispensacion", "consumo_30d", "consumo_diario_promedio"]
    return tabla[columnas]


def _uso(dominio) -> list[comun.Bloque]:
    ctx = dominio.contexto()
    d = ctx.datos.demanda
    diario = (d.groupby(["fecha", "tipo_item"]).agg(dispensaciones=("dispensaciones", "sum"),
                                                    unidades=("unidades", "sum"),
                                                    items_distintos=("codigo", "nunique"))
              .reset_index().sort_values(["fecha", "tipo_item"], ascending=[False, True], ignore_index=True))
    diario.insert(1, "dia_semana", diario["fecha"].dt.dayofweek.map(lambda i: comun.DIAS_SEMANA[i]))
    ult30 = d[d["fecha"] >= ctx.fecha_referencia - pd.Timedelta(days=29)]
    top = (ult30.groupby("codigo", as_index=False)[["dispensaciones", "unidades"]].sum()
           .assign(unidades_por_dia=lambda t: (t["unidades"] / 30).round(2))
           .sort_values("dispensaciones", ascending=False).head(20)
           .merge(ctx.datos.catalogo[["codigo", "nombre", "tipo_item"]], on="codigo", how="left"))
    return [
        ("Demanda diaria real por tipo de ítem", diario),
        ("Top 20 ítems por dispensaciones · últimos 30 días",
         top[["codigo", "nombre", "tipo_item", "dispensaciones", "unidades", "unidades_por_dia"]]),
    ]


def _cobertura_items(dominio) -> pd.DataFrame:
    """Pronóstico de 7 días por ítem clase A y cobertura contra el stock (simulado)."""
    ctx = dominio.contexto()
    clasif = dominio.clasificacion_items()
    candidatos = clasif[(clasif["clase_abc"] == "A") & (clasif["frecuencia_dias_pct"] >= 50)].head(ITEMS_PRONOSTICO)
    filas = []
    for item in candidatos.itertuples():
        base = {"codigo": item.codigo, "nombre": item.nombre, "tipo_item": item.tipo_item}
        try:
            modelo = dominio.modelo({"codigo": item.codigo})
            tabla = modelo.predecir_hasta(ctx.fin_modelo + pd.Timedelta(days=7))
            demanda = tabla["prediccion"].sum()
            stock = item.stock_actual
            filas.append({**base,
                          "demanda_7d_pronosticada": round(demanda, 1),
                          "demanda_7d_min": round(tabla["limite_inferior"].sum(), 1),
                          "demanda_7d_max": round(tabla["limite_superior"].sum(), 1),
                          "stock_actual_simulado": stock,
                          "cobertura_proyectada_dias": round(stock / (demanda / 7), 1) if demanda > 0 and pd.notna(stock) else None,
                          "mae_modelo": round(modelo.metricas["mae"], 2),
                          "mae_linea_base": round(modelo.metricas["mae_baseline"], 2),
                          "supera_linea_base": "Sí" if modelo.supera_baseline else "NO"})
        except (comun.DatosInsuficientes, comun.NoEncontrado) as exc:
            filas.append({**base, "observacion": str(exc)})
    return pd.DataFrame(filas)


def _prediccion(dominio) -> list[comun.Bloque]:
    ctx = dominio.contexto()
    modelo = dominio.modelo({})
    pronostico = modelo.predecir_hasta(ctx.fin_modelo + pd.Timedelta(days=7))
    notas = ctx.advertencias + modelo.advertencias
    if ctx.datos.stock_simulado:
        notas.append("Stock SIMULADO: la cobertura proyectada ilustra el cálculo; se vuelve real al cargar el "
                     "inventario de farmacia. La demanda pronosticada sí se basa en dispensaciones reales.")
    notas.append("Intervalos de 7 días = suma de los límites diarios (criterio conservador).")
    return [
        ("Validación del modelo general (partición temporal estricta)",
         dominio.tabla_metricas(modelo, dominio.describir_objetivo({}))),
        ("Pronóstico de dispensaciones de farmacia · próximos 7 días", pronostico),
        (f"Ítems clase A: demanda pronosticada a 7 días y cobertura proyectada (top {ITEMS_PRONOSTICO})",
         _cobertura_items(dominio)),
        ("Validación día a día del modelo general: real vs. modelo vs. línea base", modelo.backtest),
        ("Advertencias", pd.DataFrame({"nota": notas})),
    ]


def generar_reporte(dominio) -> bytes:
    return comun.construir_excel({
        "Historico clasificado": [("Ítems de farmacia clasificados (ABC por frecuencia de dispensación y rotación)",
                                   _historico(dominio))],
        "Consumo o uso": _uso(dominio),
        "Prediccion": _prediccion(dominio),
    })
