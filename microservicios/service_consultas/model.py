"""
model.py — Consultas ambulatorias: ingresos ambulatorios y servicios prestados por especialidad.

LIMITACIÓN DE LA FUENTE: hospital.db no tiene agenda de citas ni inasistencias. No se calcula
ausentismo ni oportunidad de cita; se modela la demanda ambulatoria efectivamente atendida.

Objetivos según el filtro de /predict:
  · sin filtro                  -> ingresos ambulatorios por día.
  · {"especialidad": "..."}     -> servicios prestados por día a ingresos ambulatorios de esa especialidad.
Por defecto se excluyen los ingresos por vía Urgencias (ya los cubre service_urgencias y se evita
contarlos dos veces). {"incluir_urgencias": true} los incluye.
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

SERVICIO = "consultas"
VAR_URL = "CONSULTAS_URL"
PUERTO_POR_DEFECTO = 5004

ES_AMBULATORIO = "UPPER(clase_ingreso) LIKE 'AMBULATORI%'"
ES_URGENCIAS = "UPPER(via_ingreso) LIKE 'URGENCIA%'"

NOTA_FUENTE = ("El esquema no contiene citas agendadas ni inasistencias: se modela la demanda ambulatoria "
               "atendida (ingresos de clase Ambulatorio y los servicios que recibieron).")

SQL_INGRESOS = f"""
SELECT date(fecha_ingreso)                       AS fecha,
       COALESCE(via_ingreso, 'Sin dato')         AS via_ingreso,
       CASE WHEN {ES_URGENCIAS} THEN 1 ELSE 0 END AS via_urgencias,
       COUNT(*)                                  AS ingresos
FROM ingresos
WHERE {ES_AMBULATORIO} AND fecha_ingreso BETWEEN ? AND ?
GROUP BY 1, 2, 3
"""

SQL_SERVICIOS = """
SELECT date(s.fecha_prestacion)                          AS fecha,
       COALESCE(s.especialidad, 'Sin especialidad')      AS especialidad,
       CASE WHEN UPPER(i.via_ingreso) LIKE 'URGENCIA%' THEN 1 ELSE 0 END AS via_urgencias,
       COUNT(*)                                          AS servicios
FROM servicios s
JOIN ingresos i ON i.oid_ingreso = s.oid_ingreso
WHERE UPPER(i.clase_ingreso) LIKE 'AMBULATORI%' AND s.fecha_prestacion BETWEEN ? AND ?
GROUP BY 1, 2, 3
"""

SQL_TOTAL_INGRESOS = """
SELECT COUNT(*) AS total FROM ingresos WHERE fecha_ingreso BETWEEN ? AND ?
"""


@dataclass
class DatosConsultas:
    ingresos: pd.DataFrame
    servicios: pd.DataFrame


class ModeloConsultas(comun.DominioPronostico):
    servicio = SERVICIO
    nota_fuente = NOTA_FUENTE
    dias_calentamiento = 0  # los episodios ambulatorios son cortos: no hay efecto de borde relevante

    def cargar_datos(self, ref: pd.Timestamp, inicio: pd.Timestamp) -> DatosConsultas:
        limites = comun.limites_sql(inicio, ref)
        ing = comun.consultar(self.db_path, SQL_INGRESOS, limites)
        srv = comun.consultar(self.db_path, SQL_SERVICIOS, limites)
        for df in (ing, srv):
            df["fecha"] = pd.to_datetime(df["fecha"])
        return DatosConsultas(ing, srv)

    def serie_objetivo(self, datos: DatosConsultas, filtros: dict) -> pd.Series:
        incluir = filtros.get("incluir_urgencias", False)
        if "especialidad" in filtros:
            d = datos.servicios[datos.servicios["especialidad"] == filtros["especialidad"]]
            d = d if incluir else d[d["via_urgencias"] == 0]
            return d.groupby("fecha")["servicios"].sum()
        d = datos.ingresos if incluir else datos.ingresos[datos.ingresos["via_urgencias"] == 0]
        return d.groupby("fecha")["ingresos"].sum()

    def especialidades(self) -> list[str]:
        return sorted(self.contexto().datos.servicios["especialidad"].unique())

    def normalizar_filtros(self, payload: dict) -> dict:
        filtros: dict = {}
        incluir = payload.get("incluir_urgencias", False)
        if not isinstance(incluir, bool):
            raise comun.ErrorSolicitud("'incluir_urgencias' debe ser true o false.")
        if incluir:
            filtros["incluir_urgencias"] = True
        especialidad = payload.get("especialidad")
        if especialidad not in (None, ""):
            disponibles = {comun.normalizar_texto(e): e for e in self.especialidades()}
            clave = comun.normalizar_texto(especialidad)
            if clave not in disponibles:
                raise comun.NoEncontrado(f"Especialidad '{especialidad}' no encontrada en servicios ambulatorios.")
            filtros["especialidad"] = disponibles[clave]
        return filtros

    def describir_objetivo(self, filtros: dict) -> str:
        alcance = "incluye vía Urgencias" if filtros.get("incluir_urgencias") else "excluye vía Urgencias"
        if "especialidad" in filtros:
            return f"Servicios ambulatorios por día · {filtros['especialidad']} ({alcance})"
        return f"Ingresos ambulatorios por día ({alcance})"

    def predecir(self, payload: dict) -> dict:
        respuesta = super().predecir(payload)
        respuesta["advertencias"].append(NOTA_FUENTE)
        return respuesta

    # --- Indicadores operativos ---------------------------------------------------------------
    def kpis(self) -> dict:
        ctx = self.contexto()
        v = self.ventanas()
        ing, srv = ctx.datos.ingresos, ctx.datos.servicios
        sin_urg = ing[ing["via_urgencias"] == 0]
        diario = comun.reindexar_diario(sin_urg.groupby("fecha")["ingresos"].sum(), ctx.inicio_datos, ctx.fecha_referencia)
        u7, p28 = diario[v["ultimos_7d"][0]:v["ultimos_7d"][1]], diario[v["previos_28d"][0]:v["previos_28d"][1]]
        ult28 = v["ultimos_28d"]
        por_via = (ing[ing["fecha"].between(*ult28)].groupby("via_ingreso", as_index=False)["ingresos"].sum()
                   .sort_values("ingresos", ascending=False))
        top_esp = (srv[srv["fecha"].between(*ult28) & (srv["via_urgencias"] == 0)]
                   .groupby("especialidad", as_index=False)["servicios"].sum()
                   .sort_values("servicios", ascending=False).head(10))
        top_esp["promedio_diario"] = (top_esp["servicios"] / 28).round(1)
        total28 = int(comun.consultar(self.db_path, SQL_TOTAL_INGRESOS, comun.limites_sql(*ult28))["total"].iat[0])
        amb28 = int(sin_urg[sin_urg["fecha"].between(*ult28)]["ingresos"].sum())
        return {
            "servicio": SERVICIO,
            "fecha_referencia": ctx.fecha_referencia.date().isoformat(),
            "definicion": "Ingresos de clase Ambulatorio, excluyendo la vía Urgencias",
            "ingresos_ambulatorios_hoy": int(diario.iloc[-1]),
            "ingresos_ambulatorios_ultimos_7d": int(u7.sum()),
            "promedio_diario_7d": round(u7.mean(), 1),
            "promedio_diario_28d_previos": round(p28.mean(), 1),
            "variacion_demanda_pct": comun.variacion_pct(u7.mean(), p28.mean()),
            "participacion_ambulatoria_28d_pct": round(amb28 / total28 * 100, 1) if total28 else None,
            "ingresos_ambulatorios_por_via_28d": por_via,
            "top_especialidades_servicios_28d": top_esp,
            "notas": [NOTA_FUENTE],
            "advertencias": ctx.advertencias,
        }
