"""
model.py — Urgencias: preparación de datos, partición temporal, entrenamiento e inferencia.

Objetivo del modelo: ingresos diarios por vía Urgencias (opcionalmente por nivel de triage).
Fuente: tabla `ingresos` de hospital.db (solo lectura). Todo se agrega en SQL: ningún identificador
(id_paciente, oid_ingreso) sale de la base.
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

SERVICIO = "urgencias"
VAR_URL = "URGENCIAS_URL"
PUERTO_POR_DEFECTO = 5001
META_TRIAGE2_MIN = 30  # Resolución 5596 de 2015

FILTRO_URGENCIAS = "UPPER(via_ingreso) LIKE 'URGENCIA%'"

SQL_BASE = f"""
SELECT date(fecha_ingreso)                                   AS fecha,
       COALESCE(nivel_triage, 0)                             AS nivel_triage,
       COALESCE(turno, 'Sin dato')                           AS turno,
       COALESCE(capitulo_cie10, 'Sin diagnóstico')           AS capitulo_cie10,
       COUNT(*)                                              AS ingresos,
       SUM(CASE WHEN espera_valida = 1 THEN 1 ELSE 0 END)    AS esperas_validas,
       SUM(CASE WHEN espera_valida = 1 THEN tiempo_espera_min ELSE 0 END) AS espera_total_min,
       SUM(CASE WHEN espera_valida = 1 AND tiempo_espera_min <= {META_TRIAGE2_MIN} THEN 1 ELSE 0 END)
                                                             AS dentro_meta
FROM ingresos
WHERE {FILTRO_URGENCIAS} AND fecha_ingreso BETWEEN ? AND ?
GROUP BY 1, 2, 3, 4
"""

SQL_ESPERAS = f"""
SELECT tiempo_espera_min FROM ingresos
WHERE {FILTRO_URGENCIAS} AND espera_valida = 1 AND fecha_ingreso BETWEEN ? AND ?
"""


def etiqueta_triage(nivel: int) -> str:
    return f"Triage {int(nivel)}" if nivel else "Sin triage"


class ModeloUrgencias(comun.DominioPronostico):
    servicio = SERVICIO
    dias_calentamiento = 0  # los ingresos se cuentan completos desde el primer día del extracto

    def cargar_datos(self, ref: pd.Timestamp, inicio: pd.Timestamp) -> pd.DataFrame:
        base = comun.consultar(self.db_path, SQL_BASE, comun.limites_sql(inicio, ref))
        base["fecha"] = pd.to_datetime(base["fecha"])
        return base

    def serie_objetivo(self, datos: pd.DataFrame, filtros: dict) -> pd.Series:
        if "nivel_triage" in filtros:
            datos = datos[datos["nivel_triage"] == filtros["nivel_triage"]]
        return datos.groupby("fecha")["ingresos"].sum()

    def normalizar_filtros(self, payload: dict) -> dict:
        nivel = payload.get("nivel_triage")
        if nivel in (None, ""):
            return {}
        try:
            nivel = int(nivel)
        except (TypeError, ValueError):
            raise comun.ErrorSolicitud("'nivel_triage' debe ser un entero de 1 a 5.") from None
        if not 1 <= nivel <= 5:
            raise comun.ErrorSolicitud("'nivel_triage' debe estar entre 1 y 5.")
        return {"nivel_triage": nivel}

    def describir_objetivo(self, filtros: dict) -> str:
        extra = f" clasificados como triage {filtros['nivel_triage']}" if filtros else ""
        return f"Ingresos diarios por urgencias{extra}"

    # --- Indicadores operativos ---------------------------------------------------------------
    def esperas(self, desde: pd.Timestamp, hasta: pd.Timestamp) -> pd.Series:
        df = comun.consultar(self.db_path, SQL_ESPERAS, comun.limites_sql(desde, hasta))
        return df["tiempo_espera_min"].astype(float)

    @staticmethod
    def resumen_espera(df: pd.DataFrame, por: str) -> pd.DataFrame:
        g = df.groupby(por, as_index=False)[["ingresos", "esperas_validas", "espera_total_min", "dentro_meta"]].sum()
        g["espera_promedio_min"] = (g["espera_total_min"] / g["esperas_validas"].where(g["esperas_validas"] > 0)).round(1)
        g["pct_atendidos_en_30_min"] = (g["dentro_meta"] / g["esperas_validas"].where(g["esperas_validas"] > 0) * 100).round(1)
        return g.drop(columns=["espera_total_min", "dentro_meta"])

    def kpis(self) -> dict:
        ctx = self.contexto()
        v = self.ventanas()
        d = ctx.datos
        diario = comun.reindexar_diario(d.groupby("fecha")["ingresos"].sum(), ctx.inicio_datos, ctx.fecha_referencia)
        u7, p28 = diario[v["ultimos_7d"][0]:v["ultimos_7d"][1]], diario[v["previos_28d"][0]:v["previos_28d"][1]]
        semana = d[d["fecha"].between(*v["ultimos_7d"])]
        esperas = self.esperas(*v["ultimos_7d"])
        validas = semana["esperas_validas"].sum()
        t2 = semana[semana["nivel_triage"] == 2]
        por_triage = self.resumen_espera(semana, "nivel_triage")
        por_triage.insert(0, "triage", por_triage.pop("nivel_triage").map(etiqueta_triage))
        caps = (semana.groupby("capitulo_cie10", as_index=False)["ingresos"].sum()
                .sort_values("ingresos", ascending=False).head(5))
        return {
            "servicio": SERVICIO,
            "fecha_referencia": ctx.fecha_referencia.date().isoformat(),
            "ingresos_hoy": int(diario.iloc[-1]),
            "ingresos_ultimos_7d": int(u7.sum()),
            "promedio_diario_ultimos_7d": round(u7.mean(), 1),
            "promedio_diario_28d_previos": round(p28.mean(), 1),
            "variacion_demanda_pct": comun.variacion_pct(u7.mean(), p28.mean()),
            "espera_promedio_min_7d": round(semana["espera_total_min"].sum() / validas, 1) if validas else None,
            "espera_mediana_min_7d": round(esperas.median(), 1) if len(esperas) else None,
            "espera_p90_min_7d": round(esperas.quantile(0.9), 1) if len(esperas) else None,
            "meta_triage2_min": META_TRIAGE2_MIN,
            "cumplimiento_meta_triage2_pct_7d": (round(t2["dentro_meta"].sum() / t2["esperas_validas"].sum() * 100, 1)
                                                 if t2["esperas_validas"].sum() else None),
            "por_triage_7d": por_triage,
            "por_turno_7d": self.resumen_espera(semana, "turno"),
            "top_capitulos_cie10_7d": caps,
            "notas": ["Espera = primera atención médica − ingreso; se excluyen esperas negativas o mayores "
                      "de 24 h (espera_valida = 1).",
                      "Ventana de 7 días hasta la fecha de referencia, comparada con los 28 días previos."],
            "advertencias": ctx.advertencias,
        }
