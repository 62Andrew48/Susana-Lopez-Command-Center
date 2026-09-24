"""
model.py — Farmacia: pronóstico de la DEMANDA diaria (histórica real) de medicamentos e insumos.

Fuentes (solo lectura): `medicamentos_insumos` (dispensaciones reales) e `inventario_farmacia`.
El stock de corte es SIMULADO (metadatos.stock_simulado = 1): nunca se modela ni se presenta como real.

Objetivos según el filtro de /predict:
  · {"codigo": "..."}         -> unidades dispensadas por día de ese ítem.
  · {"tipo_item": "..."}      -> dispensaciones (líneas) por día de ese tipo.
  · sin filtro                -> dispensaciones (líneas) por día de toda la farmacia.
Los agregados se miden en líneas y no en unidades porque sumar tabletas, ampollas y mililitros
de productos distintos no tiene sentido físico.
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

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from microservicios import comun  # noqa: E402

SERVICIO = "farmacia"
VAR_URL = "FARMACIA_URL"
PUERTO_POR_DEFECTO = 5003
DIAS_INVENTARIO_CRITICO = 5
TIPOS_ITEM = {"MEDICAMENTO": "Medicamento", "INSUMO": "Insumo / dispositivo",
              "DISPOSITIVO": "Insumo / dispositivo", "INSUMO / DISPOSITIVO": "Insumo / dispositivo"}

SQL_DEMANDA = """
SELECT date(fecha_prestacion)                 AS fecha,
       codigo,
       COALESCE(tipo_item, 'Sin clasificar')  AS tipo_item,
       SUM(cantidad)                          AS unidades,
       COUNT(*)                               AS dispensaciones
FROM medicamentos_insumos
WHERE codigo IS NOT NULL AND fecha_prestacion BETWEEN ? AND ?
GROUP BY 1, 2, 3
"""

SQL_CATALOGO = """
SELECT codigo, nombre, tipo_item, categoria_rotacion, consumo_total, consumo_30d,
       consumo_diario_promedio, dispensaciones, dias_con_movimiento, stock_actual,
       stock_simulado, dias_inventario
FROM inventario_farmacia
"""


@dataclass
class DatosFarmacia:
    demanda: pd.DataFrame
    catalogo: pd.DataFrame
    stock_simulado: bool


class ModeloFarmacia(comun.DominioPronostico):
    servicio = SERVICIO
    # El extracto solo trae ingresos desde su inicio: las primeras semanas no incluyen la medicación de
    # pacientes que ya estaban hospitalizados, por eso se descartan del entrenamiento.
    dias_calentamiento = 14

    def cargar_datos(self, ref: pd.Timestamp, inicio: pd.Timestamp) -> DatosFarmacia:
        demanda = comun.consultar(self.db_path, SQL_DEMANDA, comun.limites_sql(inicio, ref))
        demanda["fecha"] = pd.to_datetime(demanda["fecha"])
        demanda["codigo"] = demanda["codigo"].astype(str)
        catalogo = comun.consultar(self.db_path, SQL_CATALOGO)
        catalogo["codigo"] = catalogo["codigo"].astype(str)
        meta = comun.leer_metadatos(self.db_path)
        simulado = str(meta.get("stock_simulado", "1")) == "1" or bool(catalogo["stock_simulado"].fillna(1).max())
        return DatosFarmacia(demanda, catalogo, simulado)

    def serie_objetivo(self, datos: DatosFarmacia, filtros: dict) -> pd.Series:
        d = datos.demanda
        if "codigo" in filtros:
            return d[d["codigo"] == filtros["codigo"]].groupby("fecha")["unidades"].sum()
        if "tipo_item" in filtros:
            d = d[d["tipo_item"] == filtros["tipo_item"]]
        return d.groupby("fecha")["dispensaciones"].sum()

    def normalizar_filtros(self, payload: dict) -> dict:
        datos = self.contexto().datos
        codigo = payload.get("codigo")
        if codigo not in (None, ""):
            codigo = str(codigo).strip()
            if codigo not in set(datos.demanda["codigo"]):
                raise comun.NoEncontrado(f"El código '{codigo}' no tiene dispensaciones en el extracto.")
            return {"codigo": codigo}
        tipo = payload.get("tipo_item")
        if tipo not in (None, ""):
            clave = comun.normalizar_texto(tipo)
            if clave not in TIPOS_ITEM:
                raise comun.ErrorSolicitud("'tipo_item' debe ser 'Medicamento' o 'Insumo / dispositivo'.")
            return {"tipo_item": TIPOS_ITEM[clave]}
        return {}

    def nombre_item(self, codigo: str) -> str:
        cat = self.contexto().datos.catalogo
        fila = cat.loc[cat["codigo"] == codigo, "nombre"]
        return str(fila.iat[0]) if len(fila) else codigo

    def describir_objetivo(self, filtros: dict) -> str:
        if "codigo" in filtros:
            return f"Unidades dispensadas por día · {filtros['codigo']} · {self.nombre_item(filtros['codigo'])}"
        if "tipo_item" in filtros:
            return f"Dispensaciones (líneas) por día · {filtros['tipo_item']}"
        return "Dispensaciones (líneas) por día · toda la farmacia"

    def predecir(self, payload: dict) -> dict:
        respuesta = super().predecir(payload)
        if "codigo" in respuesta["filtros"]:
            datos = self.contexto().datos
            fila = datos.catalogo[datos.catalogo["codigo"] == respuesta["filtros"]["codigo"]]
            if len(fila):
                stock = fila["stock_actual"].iat[0]
                respuesta["stock_referencia"] = {
                    "stock_actual": None if pd.isna(stock) else float(stock),
                    "stock_simulado": datos.stock_simulado,
                    "nota": "Existencias simuladas: el extracto del HIS no trae inventario; la demanda sí es real.",
                }
        return respuesta

    # --- Indicadores operativos ---------------------------------------------------------------
    def kpis(self) -> dict:
        ctx = self.contexto()
        v = self.ventanas()
        d, cat = ctx.datos.demanda, ctx.datos.catalogo
        diario = comun.reindexar_diario(d.groupby("fecha")["dispensaciones"].sum(), ctx.inicio_datos, ctx.fecha_referencia)
        u7, p28 = diario[v["ultimos_7d"][0]:v["ultimos_7d"][1]], diario[v["previos_28d"][0]:v["previos_28d"][1]]
        ult30 = d[d["fecha"] >= ctx.fecha_referencia - pd.Timedelta(days=29)]
        top = (ult30.groupby("codigo", as_index=False)[["unidades", "dispensaciones"]].sum()
               .sort_values("dispensaciones", ascending=False).head(10)
               .merge(cat[["codigo", "nombre", "tipo_item"]], on="codigo", how="left"))
        por_tipo = (ult30.groupby("tipo_item").agg(dispensaciones=("dispensaciones", "sum"),
                                                    items_distintos=("codigo", "nunique")).reset_index())
        criticos = cat[cat["dias_inventario"].notna() & (cat["dias_inventario"] < DIAS_INVENTARIO_CRITICO)]
        return {
            "servicio": SERVICIO,
            "fecha_referencia": ctx.fecha_referencia.date().isoformat(),
            "stock_simulado": ctx.datos.stock_simulado,
            "dispensaciones_hoy": int(diario.iloc[-1]),
            "dispensaciones_ultimos_7d": int(u7.sum()),
            "promedio_diario_dispensaciones_7d": round(u7.mean(), 1),
            "promedio_diario_dispensaciones_28d_previos": round(p28.mean(), 1),
            "variacion_demanda_pct": comun.variacion_pct(u7.mean(), p28.mean()),
            "items_con_movimiento_30d": int(ult30["codigo"].nunique()),
            "items_en_catalogo": int(len(cat)),
            "items_menos_5_dias_inventario": int(len(criticos)),
            "items_menos_5_dias_inventario_es_simulado": ctx.datos.stock_simulado,
            "por_categoria_rotacion": cat["categoria_rotacion"].fillna("Sin dato").value_counts().to_dict(),
            "por_tipo_item_30d": por_tipo,
            "top10_items_30d": top[["codigo", "nombre", "tipo_item", "dispensaciones", "unidades"]],
            "notas": ["La demanda (dispensaciones y unidades) es real. Las existencias y los días de inventario "
                      "son simulados mientras farmacia no entregue su archivo de inventario.",
                      "Los agregados se expresan en dispensaciones (líneas), no en unidades de distinto tipo."],
            "advertencias": ctx.advertencias,
        }

    # --- Clasificación para el reporte --------------------------------------------------------
    def clasificacion_items(self) -> pd.DataFrame:
        """ABC por frecuencia de dispensación (independiente de la unidad de medida) + rotación."""
        ctx = self.contexto()
        d, cat = ctx.datos.demanda, ctx.datos.catalogo
        uso = d.groupby("codigo").agg(dispensaciones_periodo=("dispensaciones", "sum"),
                                      unidades_periodo=("unidades", "sum"),
                                      dias_con_movimiento=("fecha", "nunique"),
                                      ultima_dispensacion=("fecha", "max")).reset_index()
        uso = uso.sort_values("dispensaciones_periodo", ascending=False, ignore_index=True)
        acumulado = uso["dispensaciones_periodo"].cumsum() / uso["dispensaciones_periodo"].sum() * 100
        uso["participacion_pct"] = (uso["dispensaciones_periodo"] / uso["dispensaciones_periodo"].sum() * 100).round(2)
        uso["participacion_acumulada_pct"] = acumulado.round(2)
        uso["clase_abc"] = np.select([acumulado <= 80, acumulado <= 95], ["A", "B"], default="C")
        dias_periodo = (ctx.fecha_referencia - ctx.inicio_datos).days + 1
        uso["frecuencia_dias_pct"] = (uso["dias_con_movimiento"] / dias_periodo * 100).round(1)
        return uso.merge(cat[["codigo", "nombre", "tipo_item", "categoria_rotacion", "consumo_30d",
                              "consumo_diario_promedio", "stock_actual", "dias_inventario"]],
                         on="codigo", how="left")
