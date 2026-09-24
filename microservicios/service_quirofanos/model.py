"""
model.py — Quirófanos: pronóstico de cirugías realizadas por día y área quirúrgica.

LIMITACIÓN DE LA FUENTE (documentada también en /kpis):
  · `programacion_cirugia` (HIS) solo trae consecutivo_programacion, id_paciente, oid_ingreso y
    codigo_servicio: NO tiene fecha programada, sala quirúrgica ni estado.
  · Por eso se usa exclusivamente la tabla derivada `cirugias` (fecha_cirugia, area_quirofano, estado,
    en_periodo) y solo los registros con fecha_cirugia IS NOT NULL.
  · fecha_cirugia = primera prestación facturada en un área de QUIRÓFANOS para el ingreso (día, no hora).
  · area_quirofano = área de servicio quirúrgica de esos cargos: es el mejor sustituto de "sala".
  · No existen horas de inicio ni de fin: no se calculan tiempos quirúrgicos ni de rotación de sala.
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

from dataclasses import dataclass  # noqa: E402

import pandas as pd  # noqa: E402

from microservicios import comun  # noqa: E402

SERVICIO = "quirofanos"
VAR_URL = "QUIROFANOS_URL"
PUERTO_POR_DEFECTO = 5002

NOTA_FUENTE = ("La tabla programacion_cirugia del HIS no incluye fecha programada, sala ni estado; se modela "
               "con la tabla derivada cirugias (fecha_cirugia IS NOT NULL). fecha_cirugia es el día del primer "
               "cargo en quirófanos y area_quirofano el área de servicio quirúrgica (sustituto de sala). "
               "Sin horas de inicio/fin no se calculan tiempos de rotación.")

SQL_CIRUGIAS = """
SELECT date(fecha_cirugia)                      AS fecha,
       COALESCE(area_quirofano, 'Sin área')     AS area_quirofano,
       COUNT(*)                                 AS cirugias
FROM cirugias
WHERE fecha_cirugia IS NOT NULL AND fecha_cirugia BETWEEN ? AND ?
GROUP BY 1, 2
"""

SQL_ESTADOS = """
SELECT estado, COUNT(*) AS programaciones
FROM cirugias
WHERE en_periodo = 1
GROUP BY estado
"""


@dataclass
class DatosQuirofanos:
    cirugias: pd.DataFrame
    estados: pd.DataFrame


class ModeloQuirofanos(comun.DominioPronostico):
    servicio = SERVICIO
    nota_fuente = NOTA_FUENTE
    # Las cirugías de pacientes que ingresaron antes del inicio del extracto no aparecen: primera semana incompleta.
    dias_calentamiento = 7

    def cargar_datos(self, ref: pd.Timestamp, inicio: pd.Timestamp) -> DatosQuirofanos:
        cir = comun.consultar(self.db_path, SQL_CIRUGIAS, comun.limites_sql(inicio, ref))
        cir["fecha"] = pd.to_datetime(cir["fecha"])
        return DatosQuirofanos(cir, comun.consultar(self.db_path, SQL_ESTADOS))

    def serie_objetivo(self, datos: DatosQuirofanos, filtros: dict) -> pd.Series:
        d = datos.cirugias
        if "area_quirofano" in filtros:
            d = d[d["area_quirofano"] == filtros["area_quirofano"]]
        return d.groupby("fecha")["cirugias"].sum()

    def areas(self) -> list[str]:
        return sorted(self.contexto().datos.cirugias["area_quirofano"].unique())

    def normalizar_filtros(self, payload: dict) -> dict:
        area = payload.get("area_quirofano", payload.get("sala"))
        if area in (None, ""):
            return {}
        disponibles = {comun.normalizar_texto(a): a for a in self.areas()}
        clave = comun.normalizar_texto(area)
        if clave not in disponibles:
            raise comun.NoEncontrado(f"Área quirúrgica '{area}' no encontrada. Disponibles: {sorted(disponibles.values())}")
        return {"area_quirofano": disponibles[clave]}

    def describir_objetivo(self, filtros: dict) -> str:
        area = f" en {filtros['area_quirofano']}" if filtros else " (todas las áreas)"
        return f"Cirugías realizadas por día{area}"

    def predecir(self, payload: dict) -> dict:
        respuesta = super().predecir(payload)
        respuesta["advertencias"].append(NOTA_FUENTE)
        return respuesta

    # --- Indicadores operativos ---------------------------------------------------------------
    def kpis(self) -> dict:
        ctx = self.contexto()
        v = self.ventanas()
        d = ctx.datos.cirugias
        diario = comun.reindexar_diario(d.groupby("fecha")["cirugias"].sum(), ctx.inicio_datos, ctx.fecha_referencia)
        u7, p28 = diario[v["ultimos_7d"][0]:v["ultimos_7d"][1]], diario[v["previos_28d"][0]:v["previos_28d"][1]]
        ult28 = d[d["fecha"].between(*v["ultimos_28d"])]
        por_area = ult28.groupby("area_quirofano", as_index=False)["cirugias"].sum().sort_values("cirugias", ascending=False)
        por_area["promedio_diario"] = (por_area["cirugias"] / 28).round(2)
        ocho = diario[v["ultimas_8_semanas"][0]:ctx.fin_modelo]
        por_dia = ocho.groupby(ocho.index.dayofweek).mean().round(1)
        estados = ctx.datos.estados.set_index("estado")["programaciones"]
        realizadas = int(estados.get("Realizada", 0))
        total = int(estados.sum())
        return {
            "servicio": SERVICIO,
            "fecha_referencia": ctx.fecha_referencia.date().isoformat(),
            "cirugias_hoy": int(diario.iloc[-1]),
            "cirugias_ultimos_7d": int(u7.sum()),
            "promedio_diario_7d": round(u7.mean(), 1),
            "promedio_diario_28d_previos": round(p28.mean(), 1),
            "variacion_pct": comun.variacion_pct(u7.mean(), p28.mean()),
            "por_area_quirofano_28d": por_area,
            "promedio_por_dia_semana_8sem": [{"dia_semana": comun.DIAS_SEMANA[i], "promedio_cirugias": float(x)}
                                             for i, x in por_dia.items()],
            "programacion_con_ingreso_en_periodo": {
                "programaciones": total, "con_evidencia_de_ejecucion": realizadas,
                "sin_evidencia": total - realizadas,
                "cumplimiento_pct": round(realizadas / total * 100, 1) if total else None,
            },
            "notas": [NOTA_FUENTE],
            "advertencias": ctx.advertencias,
        }
