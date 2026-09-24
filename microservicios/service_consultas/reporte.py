"""
reporte.py — Consultas ambulatorias: reporte .xlsx con tres hojas (sin identificadores).

  1. Historico clasificado · ingresos ambulatorios por día y vía de ingreso, con clasificación del día
  2. Consumo o uso        · servicios por especialidad (28 días vs. previos) y perfil por día de la semana
  3. Prediccion           · validación, pronóstico a 7 días y pronóstico de las 5 especialidades principales
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

TOP_ESPECIALIDADES = 5


def _historico(dominio) -> pd.DataFrame:
    ctx = dominio.contexto()
    ing = ctx.datos.ingresos.copy()
    total = comun.reindexar_diario(ing[ing["via_urgencias"] == 0].groupby("fecha")["ingresos"].sum(),
                                   ctx.inicio_datos, ctx.fecha_referencia)
    clasificacion = comun.clasificar_nivel(total)
    ing["dia_semana"] = ing["fecha"].dt.dayofweek.map(lambda i: comun.DIAS_SEMANA[i])
    ing["festivo"] = comun.es_festivo(ing["fecha"])
    ing["cuenta_como_consulta"] = ing["via_urgencias"].map({0: "Sí", 1: "No (vía Urgencias)"})
    ing["ingresos_ambulatorios_dia"] = ing["fecha"].map(total)
    ing["clasificacion_dia"] = ing["fecha"].map(clasificacion)
    return (ing[["fecha", "dia_semana", "festivo", "via_ingreso", "cuenta_como_consulta", "ingresos",
                 "ingresos_ambulatorios_dia", "clasificacion_dia"]]
            .sort_values(["fecha", "via_ingreso"], ascending=[False, True], ignore_index=True))


def _uso(dominio) -> list[comun.Bloque]:
    ctx = dominio.contexto()
    v = dominio.ventanas()
    srv = ctx.datos.servicios[ctx.datos.servicios["via_urgencias"] == 0]
    inicio28 = v["ultimos_28d"][0]
    ult = srv[srv["fecha"] >= inicio28].groupby("especialidad")["servicios"].sum().rename("servicios_ultimos_28d")
    prev = (srv[srv["fecha"].between(inicio28 - pd.Timedelta(days=28), inicio28 - pd.Timedelta(days=1))]
            .groupby("especialidad")["servicios"].sum().rename("servicios_28d_previos"))
    esp = pd.concat([ult, prev], axis=1).fillna(0).reset_index()
    esp["variacion_pct"] = [comun.variacion_pct(a, b) for a, b in zip(esp["servicios_ultimos_28d"], esp["servicios_28d_previos"])]
    esp["servicios_periodo_total"] = esp["especialidad"].map(srv.groupby("especialidad")["servicios"].sum())
    esp = esp.sort_values("servicios_ultimos_28d", ascending=False, ignore_index=True)

    diario = dominio.serie({})
    perfil = diario.groupby(diario.index.dayofweek).agg(["mean", "median", "max"]).round(1)
    perfil.columns = ["promedio_ingresos", "mediana_ingresos", "maximo_ingresos"]
    perfil.insert(0, "dia_semana", [comun.DIAS_SEMANA[i] for i in perfil.index])
    return [
        ("Servicios ambulatorios por especialidad: últimos 28 días vs. 28 días previos", esp),
        ("Perfil de ingresos ambulatorios por día de la semana", perfil.reset_index(drop=True)),
        ("Nota sobre la fuente", pd.DataFrame({"nota": [dominio.nota_fuente]})),
    ]


def _prediccion(dominio) -> list[comun.Bloque]:
    ctx = dominio.contexto()
    modelo = dominio.modelo({})
    horizonte = ctx.fin_modelo + pd.Timedelta(days=7)
    srv = ctx.datos.servicios[ctx.datos.servicios["via_urgencias"] == 0]
    principales = srv.groupby("especialidad")["servicios"].sum().sort_values(ascending=False).head(TOP_ESPECIALIDADES)
    resumen = []
    for especialidad in principales.index:
        try:
            m = dominio.modelo({"especialidad": especialidad})
            tabla = m.predecir_hasta(horizonte)
            resumen.append({"especialidad": especialidad,
                            "servicios_7d_pronosticados": round(tabla["prediccion"].sum(), 1),
                            "servicios_7d_min": round(tabla["limite_inferior"].sum(), 1),
                            "servicios_7d_max": round(tabla["limite_superior"].sum(), 1),
                            "mae_modelo": round(m.metricas["mae"], 2),
                            "mae_linea_base": round(m.metricas["mae_baseline"], 2),
                            "supera_linea_base": "Sí" if m.supera_baseline else "NO"})
        except (comun.DatosInsuficientes, comun.NoEncontrado) as exc:
            resumen.append({"especialidad": especialidad, "observacion": str(exc)})
    notas = ctx.advertencias + modelo.advertencias + [dominio.nota_fuente,
                                                      "Intervalos de 7 días = suma de límites diarios (conservador)."]
    return [
        ("Validación del modelo (partición temporal estricta)", dominio.tabla_metricas(modelo, dominio.describir_objetivo({}))),
        ("Pronóstico de ingresos ambulatorios · próximos 7 días", modelo.predecir_hasta(horizonte)),
        (f"Servicios ambulatorios pronosticados a 7 días · top {TOP_ESPECIALIDADES} especialidades", pd.DataFrame(resumen)),
        ("Validación día a día: real vs. modelo vs. línea base", modelo.backtest),
        ("Advertencias", pd.DataFrame({"nota": notas})),
    ]


def generar_reporte(dominio) -> bytes:
    return comun.construir_excel({
        "Historico clasificado": [("Ingresos ambulatorios por día y vía de ingreso", _historico(dominio))],
        "Consumo o uso": _uso(dominio),
        "Prediccion": _prediccion(dominio),
    })
