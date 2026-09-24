"""
comun.py — Núcleo compartido de los microservicios analíticos del HSLV.

Reúne lo que los cuatro servicios hacen igual, para que cada `model.py` solo defina su dominio:

  · Conexión SQLite de SOLO LECTURA con URI portable (Windows y POSIX) y lectura de `metadatos`.
  · Variables de calendario (festivos de Colombia) y rezagos para series diarias.
  · `PronosticadorDiario`: RandomForestRegressor con partición temporal estricta, comparación contra
    una línea base ingenua (mismo día de la semana anterior) e intervalo conformal.
  · `DominioPronostico`: plantilla con caché, entrenamiento en segundo plano y contrato de /predict.
  · Privacidad (Habeas Data): ninguna tabla ni JSON sale con identificadores de pacientes o ingresos.
  · Generación del Excel de tres hojas y manejo uniforme de errores HTTP.

Reglas que se cumplen aquí: no se leen los .txt brutos, no se usa la fecha del sistema
("hoy" = metadatos.fecha_referencia) y no se escribe en hospital.db.
"""
from __future__ import annotations

import io
import logging
import math
import os
import re
import sqlite3
import threading
import unicodedata
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import numpy as np
import pandas as pd
from flask import Flask, jsonify
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from werkzeug.exceptions import HTTPException

log = logging.getLogger("microservicios")


# ---------------------------------------------------------------------------
# Errores de dominio (cada uno se traduce a un código HTTP en registrar_manejadores)
# ---------------------------------------------------------------------------
class ErrorSolicitud(ValueError):
    """Parámetros inválidos en la petición (HTTP 400)."""


class NoEncontrado(LookupError):
    """El filtro pedido no existe en los datos (HTTP 404)."""


class DatosInsuficientes(RuntimeError):
    """La serie no tiene historia suficiente para entrenar y validar (HTTP 422)."""


class ViolacionPrivacidad(PermissionError):
    """Se intentó exponer un identificador personal (HTTP 500, falla cerrada)."""


# ---------------------------------------------------------------------------
# Privacidad (Habeas Data, Ley 1581 de 2012)
# ---------------------------------------------------------------------------
_COLUMNAS_PROHIBIDAS = {
    "id_paciente", "idpaciente2", "oid_ingreso", "consecutivo_ingreso", "consecutivo_programacion",
    "oid_triage", "oid_s", "oid_mi", "nombre_paciente", "documento", "numero_documento",
    "tipo_documento", "fecha_nacimiento",
}


def _normalizar_nombre(texto: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(texto).lower())


_PROHIBIDAS_NORM = {_normalizar_nombre(c) for c in _COLUMNAS_PROHIBIDAS}


def verificar_privacidad(df: pd.DataFrame, contexto: str = "") -> pd.DataFrame:
    """Falla cerrada: si aparece una columna identificadora se aborta la respuesta completa."""
    expuestas = [c for c in df.columns if _normalizar_nombre(c) in _PROHIBIDAS_NORM]
    if expuestas:
        raise ViolacionPrivacidad(f"Columnas identificadoras {expuestas} en {contexto or 'resultado'}")
    return df


def verificar_claves(obj: Any, ruta: str = "respuesta") -> Any:
    """Revisa recursivamente las claves de un JSON antes de enviarlo."""
    if isinstance(obj, dict):
        for clave, valor in obj.items():
            if _normalizar_nombre(clave) in _PROHIBIDAS_NORM:
                raise ViolacionPrivacidad(f"Clave identificadora '{clave}' en {ruta}")
            verificar_claves(valor, f"{ruta}.{clave}")
    elif isinstance(obj, list):
        for item in obj:
            verificar_claves(item, ruta)
    return obj


def a_json(obj: Any) -> Any:
    """Convierte tipos de numpy/pandas a JSON estándar (NaN e infinitos -> null)."""
    if isinstance(obj, dict):
        return {str(k): a_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [a_json(v) for v in obj]
    if isinstance(obj, pd.DataFrame):
        return a_json(verificar_privacidad(obj).to_dict("records"))
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        valor = float(obj)
        return round(valor, 4) if math.isfinite(valor) else None
    if obj is pd.NA or obj is pd.NaT:
        return None
    if isinstance(obj, pd.Timestamp):
        return obj.date().isoformat() if obj == obj.normalize() else obj.isoformat()
    if isinstance(obj, (datetime, date)):  # Flask serializaría date en formato HTTP, no ISO
        return obj.isoformat()
    return obj


def respuesta_json(obj: Any, status: int = 200):
    datos = verificar_claves(a_json(obj))
    return jsonify(datos), status


# ---------------------------------------------------------------------------
# Acceso a datos: solo lectura, URI portable
# ---------------------------------------------------------------------------
def conectar_solo_lectura(db_path: str | Path) -> sqlite3.Connection:
    ruta = Path(db_path).resolve()
    if not ruta.is_file():
        raise FileNotFoundError(f"No existe la base analítica {ruta}. Constrúyela con "
                                "`python database.py` en la raíz del repositorio.")
    # Resolución estricta de URI compatible con Windows y POSIX (file:///C:/... y escape de espacios)
    db_uri = f"{ruta.as_uri()}?mode=ro"
    return sqlite3.connect(db_uri, uri=True)


def consultar(db_path: str | Path, sql: str, params: tuple | dict = ()) -> pd.DataFrame:
    with closing(conectar_solo_lectura(db_path)) as conn:
        df = pd.read_sql_query(sql, conn, params=params)
    return verificar_privacidad(df, "consulta SQL")


def leer_metadatos(db_path: str | Path) -> dict[str, str]:
    df = consultar(db_path, "SELECT clave, valor FROM metadatos")
    return dict(zip(df["clave"], df["valor"]))


def huella_db(db_path: str | Path) -> tuple[int, int]:
    """(mtime, tamaño): si hospital.db se reconstruye, las cachés se invalidan solas."""
    st = Path(db_path).resolve().stat()
    return st.st_mtime_ns, st.st_size


def limites_sql(desde: pd.Timestamp, hasta: pd.Timestamp) -> tuple[str, str]:
    """Límites inclusivos como texto para columnas 'YYYY-MM-DD HH:MM:SS'."""
    return f"{desde:%Y-%m-%d} 00:00:00", f"{hasta:%Y-%m-%d} 23:59:59"


# ---------------------------------------------------------------------------
# Configuración de red
# ---------------------------------------------------------------------------
def resolver_puerto(var_url: str, por_defecto: int) -> int:
    """Puerto del servicio. Prioridad: <SERVICIO>_URL (URL completa o número) > PORT > valor por defecto.
    Se prefiere la variable propia del servicio porque PORT puede venir del .env de otra aplicación."""
    for nombre in (var_url, "PORT"):
        valor = (os.getenv(nombre) or "").strip()
        if not valor:
            continue
        if valor.isdigit():
            return int(valor)
        try:
            puerto = urlparse(valor if "://" in valor else f"http://{valor}").port
        except ValueError:
            puerto = None
        if puerto:
            return puerto
    return por_defecto


# ---------------------------------------------------------------------------
# Calendario y variables del modelo
# ---------------------------------------------------------------------------
DIAS_SEMANA = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

# Festivos de Colombia (Ley 51 de 1983, "ley Emiliani"): cambian la demanda electiva y ambulatoria.
FESTIVOS_CO = pd.DatetimeIndex(pd.to_datetime([
    "2025-01-01", "2025-01-06", "2025-03-24", "2025-04-17", "2025-04-18", "2025-05-01", "2025-06-02",
    "2025-06-23", "2025-06-30", "2025-07-20", "2025-08-07", "2025-08-18", "2025-10-13", "2025-11-03",
    "2025-11-17", "2025-12-08", "2025-12-25",
    "2026-01-01", "2026-01-12", "2026-03-23", "2026-04-02", "2026-04-03", "2026-05-01", "2026-05-18",
    "2026-06-08", "2026-06-15", "2026-06-29", "2026-07-20", "2026-08-07", "2026-08-17", "2026-10-12",
    "2026-11-02", "2026-11-16", "2026-12-08", "2026-12-25",
]))

FEATURES = [
    "dia_semana", "dia_mes", "semana_del_mes", "es_fin_semana", "festivo", "post_festivo",
    "lag_1", "lag_7", "lag_14", "media_7d", "media_14d", "media_mismo_dia_3sem",
]


def es_festivo(fechas: pd.DatetimeIndex | pd.Series) -> np.ndarray:
    return pd.DatetimeIndex(fechas).normalize().isin(FESTIVOS_CO)


def construir_features(serie: pd.Series) -> pd.DataFrame:
    """Variables para el día t usando SOLO información hasta t-1 (sin fuga del futuro).
    Se usa igual en entrenamiento y en inferencia recursiva para evitar desalineaciones."""
    idx = pd.DatetimeIndex(serie.index)
    y = pd.Series(serie.to_numpy(dtype=float), index=idx)
    pasado = y.shift(1)
    X = pd.DataFrame(index=idx)
    X["dia_semana"] = idx.dayofweek
    X["dia_mes"] = idx.day
    X["semana_del_mes"] = (idx.day - 1) // 7
    X["es_fin_semana"] = (idx.dayofweek >= 5).astype(int)
    X["festivo"] = idx.isin(FESTIVOS_CO).astype(int)
    X["post_festivo"] = (idx - pd.Timedelta(days=1)).isin(FESTIVOS_CO).astype(int)
    X["lag_1"] = pasado
    X["lag_7"] = y.shift(7)
    X["lag_14"] = y.shift(14)
    X["media_7d"] = pasado.rolling(7).mean()
    X["media_14d"] = pasado.rolling(14).mean()
    X["media_mismo_dia_3sem"] = (y.shift(7) + y.shift(14) + y.shift(21)) / 3
    return X[FEATURES]


def reindexar_diario(serie: pd.Series, inicio: pd.Timestamp, fin: pd.Timestamp) -> pd.Series:
    """Serie continua día a día: los días sin registros valen 0 (no se interpolan)."""
    s = serie.copy()
    s.index = pd.to_datetime(s.index)
    s = s.groupby(level=0).sum()
    return (s.reindex(pd.date_range(inicio, fin, freq="D"), fill_value=0.0)
            .astype(float).clip(lower=0))


def recortar_dia_incompleto(serie: pd.Series, umbral: float = 0.5) -> tuple[pd.Series, str | None]:
    """El extracto del HIS se corta en la fecha de referencia: si el último día trae menos de la mitad
    de lo habitual para ese día de la semana, se trata como día parcial y no entra al entrenamiento."""
    if len(serie) < 29:
        return serie, None
    ultimo = serie.index[-1]
    valor = float(serie.iloc[-1])
    previos = serie[serie.index.dayofweek == ultimo.dayofweek].iloc[-5:-1]
    mediana = float(previos.median()) if len(previos) else 0.0
    if mediana > 0 and valor < umbral * mediana:
        return serie.iloc[:-1], (
            f"El último día del extracto ({ultimo:%Y-%m-%d}) registra {valor:,.0f} frente a una mediana de "
            f"{mediana:,.0f} para los {DIAS_SEMANA[ultimo.dayofweek]}: se trata como corte parcial y se "
            "excluye del entrenamiento; el pronóstico arranca en ese día.")
    return serie, None


def clasificar_nivel(valores: pd.Series) -> pd.Series:
    """Etiqueta cada día según su volumen relativo al propio histórico (percentiles)."""
    p25, p75, p95 = valores.quantile([0.25, 0.75, 0.95])
    return pd.Series(np.select(
        [valores >= p95, valores > p75, valores < p25], ["Pico", "Alta", "Baja"], default="Normal"),
        index=valores.index)


def variacion_pct(actual: float, base: float) -> float | None:
    return round((actual / base - 1) * 100, 1) if base else None


def normalizar_texto(texto: Any) -> str:
    texto = unicodedata.normalize("NFKD", str(texto).strip().upper())
    return re.sub(r"\s+", " ", "".join(c for c in texto if not unicodedata.combining(c)))


# ---------------------------------------------------------------------------
# Modelo: RandomForest con validación temporal honesta
# ---------------------------------------------------------------------------
class PronosticadorDiario:
    """Pronóstico de una serie diaria.

    Validación: se entrena con el pasado y se evalúa con las últimas `dias_prueba` fechas (sin barajar).
    La evaluación es a un día vista: en cada día de prueba el modelo conoce los valores reales hasta el
    día anterior, igual que en operación. La línea base ingenua es el valor del mismo día de la semana
    anterior (lag_7). Tras validar, el modelo se reentrena con todo el histórico para producción.
    """

    HORIZONTE_MAX = 14
    MIN_DIAS_ENTRENAMIENTO = 28

    def __init__(self, dias_prueba: int = 28, nivel_intervalo: float = 0.90,
                 n_estimators: int = 300, random_state: int = 42):
        self.dias_prueba = dias_prueba
        self.nivel_intervalo = nivel_intervalo
        self.n_estimators = n_estimators
        self.random_state = random_state
        self.modelo: RandomForestRegressor | None = None
        self.metricas: dict[str, float] = {}
        self.margen = 0.0
        self.serie: pd.Series | None = None
        self.backtest: pd.DataFrame | None = None
        self.importancias: pd.DataFrame | None = None
        self.periodo_entrenamiento: tuple[pd.Timestamp, pd.Timestamp] | None = None
        self.periodo_prueba: tuple[pd.Timestamp, pd.Timestamp] | None = None
        self.advertencias: list[str] = []

    def _nuevo(self) -> RandomForestRegressor:
        return RandomForestRegressor(n_estimators=self.n_estimators, min_samples_leaf=2,
                                     random_state=self.random_state, n_jobs=1)

    def ajustar(self, serie: pd.Series) -> "PronosticadorDiario":
        serie = serie.astype(float)
        datos = construir_features(serie).assign(y=serie).dropna()
        n = len(datos)
        dias_prueba = min(self.dias_prueba, max(7, n // 4))
        if n - dias_prueba < self.MIN_DIAS_ENTRENAMIENTO:
            raise DatosInsuficientes(
                f"La serie tiene {n} días útiles; se necesitan al menos "
                f"{self.MIN_DIAS_ENTRENAMIENTO + 7} para entrenar y validar sin mirar el futuro.")

        # --- Partición temporal estricta: pasado -> entrenamiento, últimas semanas -> prueba
        train, test = datos.iloc[:-dias_prueba], datos.iloc[-dias_prueba:]
        rf = self._nuevo().fit(train[FEATURES], train["y"])
        pred = np.clip(rf.predict(test[FEATURES]), 0, None)
        base = test["lag_7"].to_numpy()
        real = test["y"].to_numpy()
        self.metricas = {
            "mae": float(mean_absolute_error(real, pred)),
            "rmse": float(math.sqrt(mean_squared_error(real, pred))),
            "r2": float(r2_score(real, pred)),
            "mae_baseline": float(mean_absolute_error(real, base)),
        }
        # Intervalo conformal: cuantil de los errores absolutos fuera de muestra (corrección de muestra finita)
        errores = np.sort(np.abs(real - pred))
        k = min(len(errores), math.ceil((len(errores) + 1) * self.nivel_intervalo))
        self.margen = float(errores[k - 1])
        self.backtest = pd.DataFrame({
            "fecha": test.index, "real": real, "prediccion_modelo": pred.round(2), "linea_base": base,
            "error_modelo": (pred - real).round(2), "error_linea_base": (base - real).round(2)})
        self.periodo_entrenamiento = (train.index.min(), train.index.max())
        self.periodo_prueba = (test.index.min(), test.index.max())

        if (serie > 0).mean() < 0.3:
            self.advertencias.append(
                "Serie intermitente (menos del 30 % de los días con movimiento): el bosque aleatorio no es el "
                "método ideal; para reposición conviene un método de demanda intermitente (Croston/SBA).")

        # --- Modelo de producción: mismo algoritmo, reentrenado con toda la historia validada
        self.modelo = self._nuevo().fit(datos[FEATURES], datos["y"])
        self.importancias = (pd.DataFrame({"variable": FEATURES,
                                           "importancia": self.modelo.feature_importances_.round(4)})
                             .sort_values("importancia", ascending=False, ignore_index=True))
        self.serie = serie
        return self

    @property
    def supera_baseline(self) -> bool:
        return bool(self.metricas and self.metricas["mae"] < self.metricas["mae_baseline"])

    def predecir_hasta(self, fecha_objetivo: pd.Timestamp) -> pd.DataFrame:
        """Pronóstico recursivo día a día hasta la fecha pedida (máximo HORIZONTE_MAX días)."""
        if self.modelo is None or self.serie is None:
            raise RuntimeError("Modelo no entrenado")
        ultimo = self.serie.index.max()
        horizonte = (pd.Timestamp(fecha_objetivo) - ultimo).days
        if horizonte < 1:
            raise ErrorSolicitud(f"La fecha debe ser posterior a {ultimo:%Y-%m-%d} (último día con datos completos).")
        if horizonte > self.HORIZONTE_MAX:
            raise ErrorSolicitud(f"Horizonte máximo: {self.HORIZONTE_MAX} días (hasta "
                                 f"{ultimo + pd.Timedelta(days=self.HORIZONTE_MAX):%Y-%m-%d}).")
        historia = self.serie.copy()
        filas = []
        for paso in range(1, horizonte + 1):
            fecha = ultimo + pd.Timedelta(days=paso)
            historia.loc[fecha] = np.nan
            x = construir_features(historia).iloc[[-1]]
            valor = float(max(self.modelo.predict(x)[0], 0.0))
            historia.loc[fecha] = valor
            # El margen se calibró a 1 día vista; se amplía con sqrt(h) como aproximación para h > 1.
            margen = self.margen * math.sqrt(paso)
            filas.append({"fecha": fecha, "dia_semana": DIAS_SEMANA[fecha.dayofweek],
                          "festivo": bool(fecha in FESTIVOS_CO), "prediccion": round(valor, 2),
                          "limite_inferior": round(max(valor - margen, 0.0), 2),
                          "limite_superior": round(valor + margen, 2), "horizonte_dias": paso})
        return pd.DataFrame(filas)


# ---------------------------------------------------------------------------
# Plantilla de dominio: caché, entrenamiento en segundo plano y contrato /predict
# ---------------------------------------------------------------------------
@dataclass
class Contexto:
    fecha_referencia: pd.Timestamp
    inicio_datos: pd.Timestamp
    inicio_modelo: pd.Timestamp
    fin_modelo: pd.Timestamp
    datos: Any
    huella: tuple[int, int]
    advertencias: list[str] = field(default_factory=list)


class DominioPronostico:
    """Cada microservicio hereda de esta clase y define su dominio clínico en model.py."""

    servicio = "base"
    dias_calentamiento = 0        # días iniciales descartados por efecto de borde del extracto
    max_modelos_cache = 48

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._ctx: Contexto | None = None
        self._lock_ctx = threading.Lock()
        self._lock_modelos = threading.Lock()
        self._modelos: OrderedDict[tuple, PronosticadorDiario] = OrderedDict()
        self._hilo: threading.Thread | None = None
        self.error_entrenamiento: str | None = None

    # --- A implementar por cada dominio ------------------------------------------------
    def cargar_datos(self, ref: pd.Timestamp, inicio: pd.Timestamp) -> Any:
        raise NotImplementedError

    def serie_objetivo(self, datos: Any, filtros: dict) -> pd.Series:
        raise NotImplementedError

    def normalizar_filtros(self, payload: dict) -> dict:
        return {}

    def describir_objetivo(self, filtros: dict) -> str:
        return "Serie diaria"

    def kpis(self) -> dict:
        raise NotImplementedError

    # --- Contexto de datos (cacheado hasta que hospital.db cambie) ----------------------
    def contexto(self) -> Contexto:
        huella = huella_db(self.db_path)
        with self._lock_ctx:
            if self._ctx is not None and self._ctx.huella == huella:
                return self._ctx
            meta = leer_metadatos(self.db_path)
            if "fecha_referencia" not in meta:
                raise RuntimeError("La tabla metadatos no tiene fecha_referencia; reconstruye hospital.db.")
            ref = pd.Timestamp(meta["fecha_referencia"]).normalize()
            inicio_datos = pd.Timestamp(meta.get("fecha_min_datos") or ref - pd.Timedelta(days=180)).normalize()
            datos = self.cargar_datos(ref, inicio_datos)
            inicio_modelo = inicio_datos + pd.Timedelta(days=self.dias_calentamiento)
            total = reindexar_diario(self.serie_objetivo(datos, {}), inicio_modelo, ref)
            recortada, aviso = recortar_dia_incompleto(total)
            advertencias = [aviso] if aviso else []
            if self.dias_calentamiento:
                advertencias.append(
                    f"Se descartan los primeros {self.dias_calentamiento} días del extracto: incluye solo "
                    "ingresos desde su inicio, así que faltan los registros de pacientes que ya estaban "
                    "hospitalizados y esas semanas subestiman la demanda real.")
            self._ctx = Contexto(ref, inicio_datos, inicio_modelo, recortada.index.max(), datos, huella,
                                 advertencias)
            with self._lock_modelos:
                self._modelos.clear()
            return self._ctx

    def serie(self, filtros: dict | None = None) -> pd.Series:
        ctx = self.contexto()
        return reindexar_diario(self.serie_objetivo(ctx.datos, filtros or {}), ctx.inicio_modelo, ctx.fin_modelo)

    # --- Modelos --------------------------------------------------------------------------
    def modelo(self, filtros: dict | None = None) -> PronosticadorDiario:
        filtros = filtros or {}
        clave = tuple(sorted(filtros.items()))
        self.contexto()
        with self._lock_modelos:
            if clave in self._modelos:
                self._modelos.move_to_end(clave)
                return self._modelos[clave]
            serie = self.serie(filtros)
            if serie.sum() <= 0:
                raise NoEncontrado(f"No hay registros en el periodo de modelado para {filtros or 'el total'}.")
            modelo = PronosticadorDiario().ajustar(serie)
            self._modelos[clave] = modelo
            while len(self._modelos) > self.max_modelos_cache:
                self._modelos.popitem(last=False)
            return modelo

    def modelo_entrenado(self) -> bool:
        return () in self._modelos

    def iniciar_entrenamiento(self) -> None:
        """Entrena el modelo general en segundo plano: /health responde de inmediato."""
        def _entrenar():
            try:
                m = self.modelo({})
                log.info("[%s] modelo listo · MAE %.2f vs base %.2f", self.servicio,
                         m.metricas["mae"], m.metricas["mae_baseline"])
            except Exception as exc:  # se reporta en /health y /predict, no tumba el servicio
                self.error_entrenamiento = f"{type(exc).__name__}: {exc}"
                log.exception("[%s] fallo de entrenamiento", self.servicio)

        self._hilo = threading.Thread(target=_entrenar, name=f"entrenar-{self.servicio}", daemon=True)
        self._hilo.start()

    # --- Contrato /predict ------------------------------------------------------------------
    def _fecha_objetivo(self, valor: Any, ctx: Contexto) -> pd.Timestamp:
        primera = ctx.fin_modelo + pd.Timedelta(days=1)
        if valor in (None, ""):
            return primera
        try:
            fecha = pd.Timestamp(str(valor)).normalize()
        except (ValueError, TypeError):
            raise ErrorSolicitud("'fecha' debe tener formato YYYY-MM-DD.") from None
        limite = ctx.fin_modelo + pd.Timedelta(days=PronosticadorDiario.HORIZONTE_MAX)
        if not primera <= fecha <= limite:
            raise ErrorSolicitud(f"'fecha' debe estar entre {primera:%Y-%m-%d} y {limite:%Y-%m-%d} "
                                 f"(fecha de referencia del extracto: {ctx.fecha_referencia:%Y-%m-%d}).")
        return fecha

    def predecir(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ErrorSolicitud("El cuerpo debe ser un objeto JSON.")
        filtros = self.normalizar_filtros(payload)
        ctx = self.contexto()
        fecha = self._fecha_objetivo(payload.get("fecha"), ctx)
        modelo = self.modelo(filtros)
        fila = modelo.predecir_hasta(fecha).iloc[-1]
        m = modelo.metricas
        advertencias = list(ctx.advertencias) + modelo.advertencias
        if not modelo.supera_baseline:
            advertencias.append(
                f"El modelo NO supera la línea base ingenua en la validación (MAE {m['mae']:.2f} frente a "
                f"{m['mae_baseline']:.2f}). Para este objetivo es más confiable usar el valor del mismo día "
                "de la semana anterior.")
        if fila["horizonte_dias"] > 1:
            advertencias.append("Intervalo calibrado a 1 día vista y ampliado por √h para horizontes mayores "
                                "(aproximación; la cobertura real puede diferir).")
        return {
            "prediccion": float(fila["prediccion"]),
            "intervalo": [float(fila["limite_inferior"]), float(fila["limite_superior"])],
            "metricas": {
                "mae": round(m["mae"], 3), "rmse": round(m["rmse"], 3), "r2": round(m["r2"], 3),
                "mae_baseline": round(m["mae_baseline"], 3), "supera_baseline": modelo.supera_baseline,
            },
            "features_usadas": list(FEATURES),
            "servicio": self.servicio,
            "objetivo": self.describir_objetivo(filtros),
            "filtros": filtros,
            "fecha_objetivo": fecha.date().isoformat(),
            "horizonte_dias": int(fila["horizonte_dias"]),
            "nivel_intervalo": modelo.nivel_intervalo,
            "validacion": {
                "entrenamiento": [d.date().isoformat() for d in modelo.periodo_entrenamiento],
                "prueba": [d.date().isoformat() for d in modelo.periodo_prueba],
                "linea_base": "valor real del mismo día de la semana anterior (lag 7)",
            },
            "advertencias": advertencias,
        }

    # --- Utilidades para KPIs y reportes ----------------------------------------------------
    def ventanas(self) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
        ref = self.contexto().fecha_referencia
        d = lambda n: ref - pd.Timedelta(days=n)  # noqa: E731
        return {"hoy": (ref, ref), "ultimos_7d": (d(6), ref), "previos_28d": (d(34), d(7)),
                "ultimos_28d": (d(27), ref), "ultimas_8_semanas": (d(55), ref)}

    def tabla_metricas(self, modelo: PronosticadorDiario, objetivo: str) -> pd.DataFrame:
        m = modelo.metricas
        mejora = (1 - m["mae"] / m["mae_baseline"]) * 100 if m["mae_baseline"] else float("nan")
        filas = [
            ("Objetivo", objetivo),
            ("Algoritmo", f"RandomForestRegressor ({modelo.n_estimators} árboles, random_state=42)"),
            ("Entrenamiento", f"{modelo.periodo_entrenamiento[0]:%Y-%m-%d} a {modelo.periodo_entrenamiento[1]:%Y-%m-%d}"),
            ("Prueba (partición temporal)", f"{modelo.periodo_prueba[0]:%Y-%m-%d} a {modelo.periodo_prueba[1]:%Y-%m-%d}"),
            ("MAE modelo", round(m["mae"], 3)),
            ("RMSE modelo", round(m["rmse"], 3)),
            ("R² modelo", round(m["r2"], 3)),
            ("MAE línea base (mismo día semana anterior)", round(m["mae_baseline"], 3)),
            ("Mejora frente a la línea base (%)", round(mejora, 1) if math.isfinite(mejora) else "N/D"),
            ("¿Supera la línea base?", "Sí" if modelo.supera_baseline else "NO: usar la línea base"),
            ("Intervalo", f"{int(modelo.nivel_intervalo * 100)} % conformal (±{modelo.margen:.2f} a 1 día)"),
        ]
        return pd.DataFrame(filas, columns=["indicador", "valor"]).astype({"valor": str})


# ---------------------------------------------------------------------------
# Excel: exactamente tres hojas, varios bloques por hoja
# ---------------------------------------------------------------------------
HOJAS_REPORTE = ("Historico clasificado", "Consumo o uso", "Prediccion")
Bloque = tuple[str, pd.DataFrame]


def construir_excel(hojas: dict[str, list[Bloque]]) -> bytes:
    if tuple(hojas) != HOJAS_REPORTE:
        raise ValueError(f"El reporte debe tener exactamente las hojas {HOJAS_REPORTE}")
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    titulo_font = Font(bold=True, size=12, color="1F4E79")
    encabezado_font = Font(bold=True, color="FFFFFF")
    encabezado_fill = PatternFill("solid", fgColor="2E7D32")
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for nombre, bloques in hojas.items():
            fila = 0
            encabezados: list[tuple[int, int]] = []
            for titulo, df in bloques:
                df = verificar_privacidad(df.copy(), f"hoja '{nombre}'")
                for col in df.columns:
                    if pd.api.types.is_datetime64_any_dtype(df[col]):
                        df[col] = df[col].dt.date
                if df.empty and len(df.columns) == 0:
                    df = pd.DataFrame({"resultado": ["Sin datos"]})
                df.to_excel(writer, sheet_name=nombre, startrow=fila + 1, index=False)
                ws = writer.sheets[nombre]
                ws.cell(row=fila + 1, column=1, value=titulo).font = titulo_font
                encabezados.append((fila + 2, len(df.columns)))
                fila += len(df) + 4
            ws = writer.sheets[nombre]
            for fila_enc, n_cols in encabezados:
                for c in range(1, n_cols + 1):
                    celda = ws.cell(row=fila_enc, column=c)
                    celda.font, celda.fill = encabezado_font, encabezado_fill
                    celda.alignment = Alignment(wrap_text=True, vertical="center")
            for columna in ws.columns:
                largo = max((len(str(c.value)) for c in columna if c.value is not None), default=8)
                ws.column_dimensions[get_column_letter(columna[0].column)].width = min(max(largo + 2, 10), 60)
                for celda in columna:
                    if isinstance(celda.value, float):
                        celda.number_format = "#,##0.00"
            if len(bloques) == 1:
                ws.freeze_panes = "A3"
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Flask: manejo uniforme de errores y lectura del cuerpo JSON
# ---------------------------------------------------------------------------
def leer_json(request) -> dict:
    if not request.get_data():
        return {}
    payload = request.get_json(silent=True)
    if payload is None:
        raise ErrorSolicitud("El cuerpo no es JSON válido (usa Content-Type: application/json).")
    return payload


def registrar_manejadores(app: Flask) -> None:
    app.json.ensure_ascii = False
    app.json.sort_keys = False

    @app.errorhandler(Exception)
    def _manejar(exc: Exception):
        if isinstance(exc, HTTPException):
            return jsonify({"error": exc.description}), exc.code
        if isinstance(exc, ErrorSolicitud):
            return jsonify({"error": str(exc)}), 400
        if isinstance(exc, NoEncontrado):
            return jsonify({"error": str(exc)}), 404
        if isinstance(exc, DatosInsuficientes):
            return jsonify({"error": str(exc)}), 422
        if isinstance(exc, (FileNotFoundError, sqlite3.Error)):
            return jsonify({"error": f"Base analítica no disponible: {exc}"}), 503
        if isinstance(exc, ViolacionPrivacidad):
            log.error("Bloqueo de privacidad: %s", exc)
            return jsonify({"error": "Respuesta bloqueada por la política de privacidad."}), 500
        log.exception("Error no controlado")
        return jsonify({"error": "Error interno del servicio."}), 500
