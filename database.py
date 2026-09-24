"""
database.py — Capa de datos (Modelo en el patrón MVC).

Responsabilidades
-----------------
1. Ingesta: lee los 7 archivos delimitados por '|' de ./Datos/.
2. Limpieza: tipos, fechas (pd.to_datetime), normalización de textos y
   anonimización (se descartan nombres, fecha de nacimiento exacta y el texto
   libre del motivo de consulta).
3. Modelo derivado: estancias estimadas, catálogo de camas, ocupación diaria,
   inventario de farmacia y consolidado de cirugías.
4. KPIs: funciones reutilizadas por el dashboard, la API y el agente.

Uso:
    python database.py            # construye hospital.db
    python database.py --rebuild  # la reconstruye desde cero
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import re
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("database")

# ---------------------------------------------------------------------------
# Catálogos de normalización
# ---------------------------------------------------------------------------
SOURCE_FILES = {
    "paciente": "Paciente.txt",
    "ingresos": "Ingresos.txt",
    "atencion": "Atencion.txt",
    "triage": "Triage.txt",
    "servicios": "Servicios.txt",
    "medicamentos": "MedicamentoInsumo.txt",
    "cirugias": "ProgramacionCirugia.txt",
}
OPTIONAL_STOCK_FILE = "Inventario.txt"  # CodigoServicio|Stock  (si el hospital lo aporta)

# Grupo de cama del HIS -> nombre de servicio legible (lo usa el agente: "UCI", "Pediatría"...)
SERVICE_BY_BED_GROUP = {
    "UNIDAD DE CUIDADO INTENSIVO": "UCI",
    "UNIDAD DE CUIDADO INTERMEDIO": "Cuidado Intermedio",
    "UNIDAD DE CUIDADO BASICO": "Cuidado Básico Neonatal",
    "HOSPITALIZACION": "Hospitalización",
    "PEDIATRIA": "Pediatría",
    "URGENCIAS": "Urgencias",
    "RECUPERACION": "Recuperación",
    "GINECO OBSTRETICIA": "Gineco-obstetricia",
    "SALA PARTOS": "Sala de partos",
}
DATETIME_FMT = "%Y-%m-%d %H:%M:%S"

# Capítulos CIE-10 (rango inicio, rango fin, nombre corto). Permiten detectar picos de demanda
# por tipo de patología sin el sesgo de "última cama asignada" del campo CodigoCama.
ICD10_CHAPTERS = [
    ("A00", "B99", "Infecciosas y parasitarias"), ("C00", "D48", "Neoplasias"),
    ("D50", "D89", "Sangre e inmunidad"), ("E00", "E90", "Endocrinas y metabólicas"),
    ("F00", "F99", "Trastornos mentales"), ("G00", "G99", "Sistema nervioso"),
    ("H00", "H59", "Ojo"), ("H60", "H95", "Oído"), ("I00", "I99", "Circulatorio"),
    ("J00", "J99", "Respiratorio"), ("K00", "K93", "Digestivo"), ("L00", "L99", "Piel"),
    ("M00", "M99", "Osteomuscular"), ("N00", "N99", "Genitourinario"),
    ("O00", "O99", "Embarazo, parto y puerperio"), ("P00", "P96", "Perinatal"),
    ("Q00", "Q99", "Malformaciones congénitas"), ("R00", "R99", "Síntomas y hallazgos anormales"),
    ("S00", "T98", "Traumatismos y envenenamientos"), ("V01", "Y98", "Causas externas"),
    ("Z00", "Z99", "Factores que influyen en la salud"),
]


def icd10_chapter(code: str | float) -> str | None:
    if not isinstance(code, str) or len(code) < 3:
        return None
    key = code[:3].upper()
    for lo, hi, name in ICD10_CHAPTERS:
        if lo <= key <= hi:
            return name
    return "Otros"


# ---------------------------------------------------------------------------
# Conexión
# ---------------------------------------------------------------------------
def get_connection(read_only: bool = False, db_path: Path | None = None) -> sqlite3.Connection:
    """Devuelve una conexión SQLite. En modo solo-lectura el motor rechaza cualquier escritura
    (segunda barrera de seguridad para las consultas generadas por el agente)."""
    path = Path(db_path or config.DB_PATH)
    if read_only:
        # as_uri() genera file:///C:/... en Windows y escapa espacios en la ruta
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Utilidades de ingesta
# ---------------------------------------------------------------------------
def read_pipe_file(name: str) -> pd.DataFrame:
    """Lee un archivo '|' como texto. QUOTE_NONE es obligatorio: el campo MotivoConsulta de
    Triage trae comillas sin cerrar que, con el lector por defecto, descartan cientos de filas."""
    path = config.DATA_DIR / SOURCE_FILES[name]
    if not path.exists():
        raise FileNotFoundError(f"No se encontró {path}. Copia los archivos del reto en {config.DATA_DIR}")
    df = pd.read_csv(path, sep="|", dtype=str, quoting=csv.QUOTE_NONE, encoding="utf-8", keep_default_na=True)
    df.columns = [c.strip() for c in df.columns]
    for col in df.columns:
        df[col] = df[col].str.strip().replace({"": np.nan, "NULL": np.nan})
    log.info("Leído %-24s %8d filas", path.name, len(df))
    return df


def to_datetime(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce", format="mixed")


def to_int(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").astype("Int64")


def fmt_dt(series: pd.Series) -> pd.Series:
    """Fechas como TEXT ISO: SQLite las compara y las procesa con date()/strftime()."""
    return series.dt.strftime(DATETIME_FMT).where(series.notna(), None)


def parse_triage_level(text: str | float) -> float:
    """'CONSULTORIOS DIFERIDA - TRIAGE 3' -> 3."""
    if not isinstance(text, str):
        return np.nan
    match = re.search(r"TRIAGE\s*[-–]?\s*([1-5])", text.upper())
    return float(match.group(1)) if match else np.nan


def shift_of(ts: pd.Series) -> pd.Series:
    """Turno asistencial según la hora de llegada."""
    hours = ts.dt.hour
    return pd.Series(
        np.select([hours.between(7, 12), hours.between(13, 18)], ["Mañana", "Tarde"], default="Noche"),
        index=ts.index,
    ).where(ts.notna(), None)


def age_group(age: pd.Series) -> pd.Series:
    bins = [-1, 0, 5, 17, 29, 59, 200]
    labels = ["<1 año", "1-5", "6-17", "18-29", "30-59", "60+"]
    return pd.cut(age, bins=bins, labels=labels).astype(str).replace("nan", None)


# ---------------------------------------------------------------------------
# Transformaciones por tabla
# ---------------------------------------------------------------------------
def build_patients(raw: pd.DataFrame, ref_day: pd.Timestamp) -> pd.DataFrame:
    """Anonimiza: se eliminan NombrePaciente y FechaNacimiento (se conserva edad y grupo etario)."""
    birth = to_datetime(raw["FechaNacimiento"])
    age = ((ref_day - birth).dt.days // 365.25).clip(lower=0)
    return pd.DataFrame({
        "id_paciente": to_int(raw["IdPaciente"]),
        "tipo_documento": raw["TipoDocumento"],
        "sexo": raw["Sexo"],
        "edad": age.astype("Int64"),
        "grupo_etario": age_group(age),
        "asegurador": raw["Asegurador"].str.replace(r"\s+", " ", regex=True),
        "regimen": raw["Regimen"],
        "departamento": raw["Departamento"],
        "municipio": raw["Municipio"],
        "zona": raw["Zona"],
    }).drop_duplicates("id_paciente")


def build_triage(raw: pd.DataFrame) -> pd.DataFrame:
    """Se descarta MotivoConsulta (texto libre con posibles datos identificables)."""
    df = pd.DataFrame({
        "oid_triage": to_int(raw["OidTriage"]),
        "fecha_triage": to_datetime(raw["FechaTriage"]),
        "id_paciente": to_int(raw["IdPaciente2"]),
        "codigo_triage": raw["CodigoTriage"],
        "clasificacion_triage": raw["ClasificacionTriage"],
        "tension_arterial": raw["TensionArterial"],
        "frecuencia_cardiaca": pd.to_numeric(raw["FrecuenciaCardiaca"], errors="coerce"),
        "frecuencia_respiratoria": pd.to_numeric(raw["FrecuenciaRespiratoria"], errors="coerce"),
        "temperatura": pd.to_numeric(raw["Temperatura"], errors="coerce"),
    })
    df = df.dropna(subset=["oid_triage"]).drop_duplicates("oid_triage")
    df["nivel_triage"] = df["clasificacion_triage"].map(parse_triage_level).astype("Int64")
    return df


def build_admissions(raw: pd.DataFrame, attention: pd.DataFrame, triage: pd.DataFrame,
                     last_activity: pd.Series) -> pd.DataFrame:
    """Ingresos enriquecidos con: fecha de atención, espera, triage, turno y estancia estimada."""
    df = pd.DataFrame({
        "oid_ingreso": to_int(raw["OidIngreso"]),
        "consecutivo_ingreso": to_int(raw["ConsecutivoIngreso"]),
        "id_paciente": to_int(raw["IdPaciente"]),
        "clase_ingreso": raw["ClaseIngreso"],
        "via_ingreso": raw["ViaIngreso"],
        "tipo_riesgo": raw["TipoRiesgo"],
        "fecha_ingreso": to_datetime(raw["FechaIngreso"]),
        "fecha_hospitalizacion": to_datetime(raw["FechaHospitalizacion"]),
        "oid_triage": to_int(raw["OidTriageA"]),
        "codigo_cama": raw["CodigoCama"],
        "nombre_cama": raw["NombreCama"],
        "grupo_cama": raw["NombreGrupoCama"],
        "subgrupo_cama": raw["NombreSubgrupoCama"],
        "codigo_diagnostico": raw["CodigoDiagnostico"],
        "nombre_diagnostico": raw["NombreDiagnostico"],
    }).drop_duplicates("oid_ingreso")
    df["servicio"] = df["grupo_cama"].map(SERVICE_BY_BED_GROUP).fillna(df["grupo_cama"].str.title())
    df["capitulo_cie10"] = df["codigo_diagnostico"].map(icd10_chapter)

    # Tiempo de espera = FechaAtencion - FechaIngreso (minutos)
    df = df.merge(attention, on="oid_ingreso", how="left")
    df["tiempo_espera_min"] = (df["fecha_atencion"] - df["fecha_ingreso"]).dt.total_seconds() / 60
    df["espera_valida"] = df["tiempo_espera_min"].between(0, config.WAIT_MAX_VALID_MIN).astype(int)

    # Triage (merge sin nulos en la llave para no multiplicar filas)
    tri = triage[["oid_triage", "fecha_triage", "nivel_triage", "clasificacion_triage"]]
    df = df.merge(tri, on="oid_triage", how="left")
    df["espera_triage_atencion_min"] = (df["fecha_atencion"] - df["fecha_triage"]).dt.total_seconds() / 60
    df["turno"] = shift_of(df["fecha_ingreso"])

    # Estancia estimada: el extracto no trae fecha de egreso; se usa la última prestación
    # (servicio o medicamento) registrada para el ingreso como fin estimado.
    df["fecha_inicio_estancia"] = df["fecha_hospitalizacion"].fillna(df["fecha_ingreso"])
    df["ultima_actividad"] = df["oid_ingreso"].map(last_activity)
    df["fecha_fin_estimada"] = df[["ultima_actividad", "fecha_inicio_estancia"]].max(axis=1)
    df["estancia_horas"] = (
        (df["fecha_fin_estimada"] - df["fecha_inicio_estancia"]).dt.total_seconds() / 3600
    ).round(2)
    return df.drop(columns=["ultima_actividad"])


def build_beds(adm: pd.DataFrame) -> pd.DataFrame:
    """Catálogo de camas observado en el periodo (proxy de capacidad instalada)."""
    beds = (adm.groupby("codigo_cama")
            .agg(nombre_cama=("nombre_cama", lambda s: s.mode().iat[0]),
                 grupo_cama=("grupo_cama", lambda s: s.mode().iat[0]),
                 subgrupo_cama=("subgrupo_cama", lambda s: s.mode().iat[0]),
                 servicio=("servicio", lambda s: s.mode().iat[0]))
            .reset_index())
    beds["es_virtual"] = beds["nombre_cama"].str.contains("VIRTUAL", case=False, na=False).astype(int)
    return beds


def build_daily_occupancy(adm: pd.DataFrame, beds: pd.DataFrame, ref_day: pd.Timestamp) -> pd.DataFrame:
    """Camas ocupadas por día y subgrupo: una cama cuenta como ocupada si alguna estancia
    estimada se cruza con ese día calendario."""
    stays = adm.dropna(subset=["fecha_inicio_estancia"]).copy()
    stays["d0"] = stays["fecha_inicio_estancia"].dt.normalize()
    stays["d1"] = stays["fecha_fin_estimada"].dt.normalize().clip(upper=ref_day)
    stays = stays[stays["d1"] >= stays["d0"]]
    stays["fecha"] = [pd.date_range(a, b, freq="D") for a, b in zip(stays["d0"], stays["d1"])]
    daily = stays[["fecha", "oid_ingreso", "codigo_cama", "subgrupo_cama"]].explode("fecha")
    occ = (daily.groupby(["fecha", "subgrupo_cama"])
           .agg(camas_ocupadas=("codigo_cama", "nunique"), pacientes=("oid_ingreso", "nunique"))
           .reset_index())

    capacity = (beds.groupby(["servicio", "grupo_cama", "subgrupo_cama"])
                .agg(capacidad=("codigo_cama", "nunique"), camas_virtuales=("es_virtual", "sum"))
                .reset_index())
    all_days = pd.date_range(adm["fecha_ingreso"].min().normalize(), ref_day, freq="D")
    grid = capacity.merge(pd.DataFrame({"fecha": all_days}), how="cross")
    out = grid.merge(occ, on=["fecha", "subgrupo_cama"], how="left").fillna({"camas_ocupadas": 0, "pacientes": 0})
    out[["camas_ocupadas", "pacientes"]] = out[["camas_ocupadas", "pacientes"]].astype(int)
    out["porcentaje_ocupacion"] = (out["camas_ocupadas"] / out["capacidad"] * 100).round(1)
    out["fecha"] = out["fecha"].dt.strftime("%Y-%m-%d")
    return out[["fecha", "servicio", "grupo_cama", "subgrupo_cama", "capacidad", "camas_virtuales",
                "camas_ocupadas", "pacientes", "porcentaje_ocupacion"]]


def build_services(raw: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({
        "oid_s": to_int(raw["OidS"]),
        "oid_ingreso": to_int(raw["OidIngreso"]),
        "codigo_servicio": raw["CodigoServicio"],
        "nombre_servicio": raw["NombreServicio"],
        "cantidad": pd.to_numeric(raw["Cantidad"], errors="coerce").fillna(0),
        "fecha_prestacion": to_datetime(raw["FechaPrestacion"]),
        "codigo_area_servicio": raw["CodigoAreaServicio"],
        "area_servicio": raw["AreaServicio"],
        "especialidad": raw["Especialidad"],
    })


def build_medications(raw: pd.DataFrame) -> pd.DataFrame:
    df = pd.DataFrame({
        "oid_mi": to_int(raw["OidMI"]),
        "oid_ingreso": to_int(raw["OidIngreso"]),
        "codigo": raw["CodigoServicio"],
        "nombre": raw["NombreServicio"],
        "cantidad": pd.to_numeric(raw["Cantidad"], errors="coerce").fillna(0),
        "fecha_prestacion": to_datetime(raw["FechaPrestacion"]),
        "area_servicio": raw["AreaServicio"],
        "especialidad": raw["Especialidad"],
    })
    # Tipo de ítem: el HIS dispensa dispositivos/insumos desde áreas "DISPOS..."
    is_device = df["area_servicio"].str.contains(r"DISPOS", case=False, na=False)
    device_share = is_device.groupby(df["codigo"]).mean()
    df["tipo_item"] = np.where(df["codigo"].map(device_share) >= 0.5, "Insumo / dispositivo", "Medicamento")
    return df


def _simulated_coverage_days(code: str) -> int:
    """Cobertura simulada determinística (1-45 días) derivada del código: la demo es
    reproducible y no depende de aleatoriedad. Se reemplaza al cargar Inventario.txt real."""
    digest = int(hashlib.md5(code.encode("utf-8")).hexdigest(), 16)
    return 1 + digest % 45


def build_pharmacy_inventory(meds: pd.DataFrame, ref_day: pd.Timestamp) -> pd.DataFrame:
    """Rotación real (consumo) + stock. El extracto no trae existencias: si no hay
    Datos/Inventario.txt el stock se SIMULA y queda marcado en la columna stock_simulado."""
    window_start = ref_day - timedelta(days=29)
    period_days = max((ref_day - meds["fecha_prestacion"].min().normalize()).days + 1, 1)
    last30 = meds[meds["fecha_prestacion"] >= window_start]

    inv = (meds.groupby("codigo")
           .agg(nombre=("nombre", lambda s: s.mode().iat[0]),
                tipo_item=("tipo_item", "first"),
                consumo_total=("cantidad", "sum"),
                dispensaciones=("oid_mi", "count"),
                dias_con_movimiento=("fecha_prestacion", lambda s: s.dt.normalize().nunique()),
                ultima_dispensacion=("fecha_prestacion", "max"))
           .reset_index())
    inv["consumo_30d"] = inv["codigo"].map(last30.groupby("codigo")["cantidad"].sum()).fillna(0)
    inv["consumo_diario_promedio"] = np.where(
        inv["consumo_30d"] > 0, inv["consumo_30d"] / 30, inv["consumo_total"] / period_days).round(3)

    # Categoría de rotación por percentiles del consumo diario
    q20, q80 = inv["consumo_diario_promedio"].quantile([0.2, 0.8])
    inv["categoria_rotacion"] = np.select(
        [inv["consumo_30d"] == 0, inv["consumo_diario_promedio"] >= q80, inv["consumo_diario_promedio"] <= q20],
        ["Sin movimiento 30 días", "Alta", "Baja"], default="Media")

    stock_file = config.DATA_DIR / OPTIONAL_STOCK_FILE
    if stock_file.exists():
        real = pd.read_csv(stock_file, sep="|", dtype={"CodigoServicio": str})
        inv["stock_actual"] = inv["codigo"].map(real.set_index("CodigoServicio")["Stock"])
        inv["stock_simulado"] = 0
        log.info("Stock real cargado desde %s", stock_file.name)
    else:
        coverage = inv["codigo"].map(_simulated_coverage_days)
        inv["stock_actual"] = np.ceil(inv["consumo_diario_promedio"] * coverage)
        inv["stock_simulado"] = 1
        log.warning("Sin %s: stock SIMULADO (marcado con stock_simulado=1)", OPTIONAL_STOCK_FILE)

    inv["dias_inventario"] = np.where(
        inv["consumo_30d"] > 0, (inv["stock_actual"] / inv["consumo_diario_promedio"]).round(1), np.nan)
    inv["ultima_dispensacion"] = fmt_dt(inv["ultima_dispensacion"])
    return inv


def build_surgeries(raw: pd.DataFrame, services: pd.DataFrame, adm: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Programadas vs. realizadas. Una programación se considera realizada si su ingreso
    registra algún servicio en un área de QUIRÓFANOS."""
    prog = pd.DataFrame({
        "consecutivo_programacion": raw["ConsecutivoProgramacion"],
        "id_paciente": to_int(raw["IdPaciente"]),
        "oid_ingreso": to_int(raw["OidIngreso"]),
        "codigo_servicio": raw["CodigoServicio"],
    })
    or_services = services[services["area_servicio"].str.startswith("QUIROFANOS", na=False)]
    or_by_adm = (or_services.groupby("oid_ingreso")
                 .agg(fecha_cirugia=("fecha_prestacion", "min"),
                      area_quirofano=("area_servicio", lambda s: s.mode().iat[0])))
    summary = (prog.groupby("consecutivo_programacion")
               .agg(id_paciente=("id_paciente", "first"),
                    oid_ingreso=("oid_ingreso", "first"),
                    procedimientos=("codigo_servicio", "count"))
               .reset_index())
    summary = summary.merge(or_by_adm, left_on="oid_ingreso", right_index=True, how="left")
    in_period = summary["oid_ingreso"].isin(adm["oid_ingreso"])
    summary["en_periodo"] = in_period.astype(int)
    summary["estado"] = np.select(
        [summary["fecha_cirugia"].notna(), in_period],
        ["Realizada", "Programada sin evidencia de ejecución"],
        default="Ingreso fuera del periodo o sin ingreso")
    summary = summary.merge(adm[["oid_ingreso", "fecha_ingreso", "servicio"]], on="oid_ingreso", how="left")
    summary["fecha_cirugia"] = fmt_dt(summary["fecha_cirugia"])
    summary["fecha_ingreso"] = fmt_dt(summary["fecha_ingreso"])
    return prog, summary


# ---------------------------------------------------------------------------
# Construcción de la base
# ---------------------------------------------------------------------------
INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_ing_fecha ON ingresos(fecha_ingreso)",
    "CREATE INDEX IF NOT EXISTS ix_ing_paciente ON ingresos(id_paciente)",
    "CREATE INDEX IF NOT EXISTS ix_ing_servicio ON ingresos(servicio, subgrupo_cama)",
    "CREATE INDEX IF NOT EXISTS ix_ing_estancia ON ingresos(fecha_inicio_estancia, fecha_fin_estimada)",
    "CREATE INDEX IF NOT EXISTS ix_ing_triage ON ingresos(oid_triage)",
    "CREATE INDEX IF NOT EXISTS ix_ing_cap ON ingresos(capitulo_cie10)",
    "CREATE INDEX IF NOT EXISTS ix_srv_nombre ON servicios(nombre_servicio)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_pac ON pacientes(id_paciente)",
    "CREATE INDEX IF NOT EXISTS ix_ate_ing ON atenciones(oid_ingreso)",
    "CREATE INDEX IF NOT EXISTS ix_tri_oid ON triage(oid_triage)",
    "CREATE INDEX IF NOT EXISTS ix_mi_ing ON medicamentos_insumos(oid_ingreso)",
    "CREATE INDEX IF NOT EXISTS ix_mi_fecha ON medicamentos_insumos(fecha_prestacion)",
    "CREATE INDEX IF NOT EXISTS ix_mi_codigo ON medicamentos_insumos(codigo)",
    "CREATE INDEX IF NOT EXISTS ix_srv_ing ON servicios(oid_ingreso)",
    "CREATE INDEX IF NOT EXISTS ix_srv_fecha ON servicios(fecha_prestacion)",
    "CREATE INDEX IF NOT EXISTS ix_srv_esp ON servicios(especialidad)",
    "CREATE INDEX IF NOT EXISTS ix_occ ON ocupacion_diaria(fecha, servicio)",
    "CREATE INDEX IF NOT EXISTS ix_inv_dias ON inventario_farmacia(dias_inventario)",
    "CREATE INDEX IF NOT EXISTS ix_cir_estado ON cirugias(estado)",
]


def _write(conn: sqlite3.Connection, name: str, df: pd.DataFrame) -> None:
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = fmt_dt(out[col])
    out.to_sql(name, conn, if_exists="replace", index=False, chunksize=50_000)
    log.info("Tabla %-22s %8d filas", name, len(out))


def build_database(rebuild: bool = False) -> Path:
    """Pipeline ETL completo. Idempotente: si la base existe y rebuild=False no hace nada."""
    db = Path(config.DB_PATH)
    if db.exists() and not rebuild:
        log.info("Base existente en %s (usa --rebuild para regenerarla)", db)
        return db
    t0 = time.time()
    if db.exists():
        db.unlink()

    raw = {k: read_pipe_file(k) for k in SOURCE_FILES}
    ingress_dates = to_datetime(raw["ingresos"]["FechaIngreso"])
    ref_day = (pd.Timestamp(config.REFERENCE_DATE) if config.REFERENCE_DATE
               else ingress_dates.max().normalize())
    log.info("Fecha de referencia ('hoy'): %s", ref_day.date())

    patients = build_patients(raw["paciente"], ref_day)
    triage = build_triage(raw["triage"])
    attention = pd.DataFrame({
        "oid_ingreso": to_int(raw["atencion"]["OidIngreso"]),
        "fecha_atencion": to_datetime(raw["atencion"]["FechaAtencion"]),
    }).dropna().sort_values("fecha_atencion").drop_duplicates("oid_ingreso")
    services = build_services(raw["servicios"])
    meds = build_medications(raw["medicamentos"])
    last_activity = pd.concat([
        services[["oid_ingreso", "fecha_prestacion"]], meds[["oid_ingreso", "fecha_prestacion"]]
    ]).groupby("oid_ingreso")["fecha_prestacion"].max()

    admissions = build_admissions(raw["ingresos"], attention, triage, last_activity)
    beds = build_beds(admissions)
    occupancy = build_daily_occupancy(admissions, beds, ref_day)
    inventory = build_pharmacy_inventory(meds, ref_day)
    programming, surgeries = build_surgeries(raw["cirugias"], services, admissions)

    metadata = pd.DataFrame([
        ("fecha_referencia", ref_day.strftime("%Y-%m-%d")),
        ("fecha_min_datos", admissions["fecha_ingreso"].min().strftime("%Y-%m-%d")),
        ("fecha_max_datos", admissions["fecha_ingreso"].max().strftime("%Y-%m-%d")),
        ("stock_simulado", str(int(inventory["stock_simulado"].max()))),
        ("construida_en", datetime.now().strftime(DATETIME_FMT)),
    ], columns=["clave", "valor"])

    with closing(get_connection()) as conn:
        for name, df in [("pacientes", patients), ("ingresos", admissions), ("atenciones", attention),
                         ("triage", triage), ("servicios", services), ("medicamentos_insumos", meds),
                         ("programacion_cirugia", programming), ("cirugias", surgeries), ("camas", beds),
                         ("ocupacion_diaria", occupancy), ("inventario_farmacia", inventory),
                         ("metadatos", metadata)]:
            _write(conn, name, df)
        for stmt in INDEXES:
            conn.execute(stmt)
        conn.commit()
        conn.execute("VACUUM")
    log.info("Base construida en %.1f s -> %s (%.0f MB)", time.time() - t0, db, db.stat().st_size / 1e6)
    return db


def ensure_database() -> Path:
    return build_database(rebuild=False)


# ---------------------------------------------------------------------------
# Helpers de periodo
# ---------------------------------------------------------------------------
def query_df(conn: sqlite3.Connection, sql: str, params: tuple | dict = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)


def get_metadata(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT clave, valor FROM metadatos").fetchall())


def get_reference_date(conn: sqlite3.Connection) -> date:
    return date.fromisoformat(get_metadata(conn)["fecha_referencia"])


def day_bounds(start: date, end: date) -> tuple[str, str]:
    """Límites inclusivos como texto para comparar con columnas fecha/hora."""
    return f"{start:%Y-%m-%d} 00:00:00", f"{end:%Y-%m-%d} 23:59:59"


def last_n_days(ref: date, n: int) -> tuple[date, date]:
    return ref - timedelta(days=n - 1), ref


def month_to_date(ref: date) -> tuple[date, date]:
    return ref.replace(day=1), ref


# ---------------------------------------------------------------------------
# KPIs hospitalarios (reutilizados por dashboard, API y agente)
# ---------------------------------------------------------------------------
def kpi_bed_occupancy(conn, day: date | None = None, by: str = "servicio",
                      include_virtual: bool = False) -> pd.DataFrame:
    """Ocupación de camas en un día. by = 'servicio' | 'subgrupo_cama'."""
    day = day or get_reference_date(conn)
    cols = "servicio" if by == "servicio" else "servicio, subgrupo_cama"
    virtual_filter = "" if include_virtual else "AND camas_virtuales < capacidad"
    df = query_df(conn, f"""
        SELECT {cols}, SUM(capacidad) AS capacidad, SUM(camas_ocupadas) AS camas_ocupadas,
               SUM(pacientes) AS pacientes
        FROM ocupacion_diaria WHERE fecha = ? {virtual_filter}
        GROUP BY {cols} ORDER BY camas_ocupadas DESC""", (day.isoformat(),))
    df["porcentaje_ocupacion"] = (df["camas_ocupadas"] / df["capacidad"] * 100).round(1)
    return df


def kpi_global_occupancy(conn, day: date | None = None) -> dict:
    """Ocupación global de camas físicas (excluye Urgencias con camas virtuales)."""
    df = kpi_bed_occupancy(conn, day)
    df = df[df["servicio"] != "Urgencias"]
    cap, occ = df["capacidad"].sum(), df["camas_ocupadas"].sum()
    return {"capacidad": int(cap), "ocupadas": int(occ),
            "porcentaje": float(round(occ / cap * 100, 1)) if cap else 0.0}


def kpi_occupancy_trend(conn, start: date, end: date) -> pd.DataFrame:
    df = query_df(conn, """
        SELECT fecha, servicio, SUM(capacidad) AS capacidad, SUM(camas_ocupadas) AS camas_ocupadas
        FROM ocupacion_diaria WHERE fecha BETWEEN ? AND ? AND servicio <> 'Urgencias'
        GROUP BY fecha, servicio ORDER BY fecha""", (start.isoformat(), end.isoformat()))
    df["porcentaje_ocupacion"] = (df["camas_ocupadas"] / df["capacidad"] * 100).round(1)
    return df


def kpi_wait_times(conn, start: date, end: date, via: str | None = "Urgencias") -> dict:
    """Espera (FechaAtencion - FechaIngreso) en minutos: total, por triage y por turno."""
    lo, hi = day_bounds(start, end)
    via_sql = "AND via_ingreso = :via" if via else ""
    params = {"lo": lo, "hi": hi, "via": via}
    base = f"FROM ingresos WHERE espera_valida = 1 AND fecha_ingreso BETWEEN :lo AND :hi {via_sql}"
    row = conn.execute(f"SELECT COUNT(*), AVG(tiempo_espera_min) {base}", params).fetchone()
    values = query_df(conn, f"SELECT tiempo_espera_min {base}", params)["tiempo_espera_min"]
    by_triage = query_df(conn, f"""
        SELECT COALESCE('Triage ' || nivel_triage, 'Sin triage') AS triage, COUNT(*) AS atenciones,
               ROUND(AVG(tiempo_espera_min), 1) AS espera_promedio_min
        {base} GROUP BY 1 ORDER BY 1""", params)
    by_shift = query_df(conn, f"""
        SELECT turno, COALESCE('Triage ' || nivel_triage, 'Sin triage') AS triage,
               COUNT(*) AS atenciones, ROUND(AVG(tiempo_espera_min), 1) AS espera_promedio_min
        {base} GROUP BY 1, 2""", params)
    return {
        "atenciones": int(row[0] or 0),
        "promedio_min": round(row[1], 1) if row[1] is not None else None,
        "mediana_min": round(float(values.median()), 1) if len(values) else None,
        "p90_min": round(float(values.quantile(0.9)), 1) if len(values) else None,
        "por_triage": by_triage,
        "por_turno_triage": by_shift,
    }


def kpi_top_diagnoses(conn, start: date, end: date, n: int = 10) -> pd.DataFrame:
    lo, hi = day_bounds(start, end)
    return query_df(conn, """
        SELECT codigo_diagnostico, nombre_diagnostico, COUNT(*) AS ingresos
        FROM ingresos WHERE fecha_ingreso BETWEEN ? AND ? AND codigo_diagnostico IS NOT NULL
        GROUP BY 1, 2 ORDER BY ingresos DESC LIMIT ?""", (lo, hi, n))


def kpi_item_consumption(conn, start: date, end: date, n: int = 10,
                         item_type: str | None = "Medicamento", lowest: bool = False) -> pd.DataFrame:
    """Mayor (o menor) rotación de medicamentos/insumos en el periodo."""
    lo, hi = day_bounds(start, end)
    type_sql = "AND tipo_item = :t" if item_type else ""
    order = "ASC" if lowest else "DESC"
    return query_df(conn, f"""
        SELECT codigo, nombre, tipo_item, SUM(cantidad) AS unidades, COUNT(*) AS dispensaciones
        FROM medicamentos_insumos WHERE fecha_prestacion BETWEEN :lo AND :hi {type_sql}
        GROUP BY 1, 2, 3 ORDER BY unidades {order} LIMIT :n""", {"lo": lo, "hi": hi, "t": item_type, "n": n})


def kpi_critical_stock(conn, max_days: float | None = None, limit: int = 50) -> pd.DataFrame:
    max_days = config.STOCK_CRITICAL_DAYS if max_days is None else max_days
    return query_df(conn, """
        SELECT codigo, nombre, tipo_item, stock_actual, consumo_diario_promedio, dias_inventario,
               categoria_rotacion, stock_simulado
        FROM inventario_farmacia WHERE dias_inventario IS NOT NULL AND dias_inventario < ?
        ORDER BY consumo_diario_promedio * (? - dias_inventario) DESC LIMIT ?""", (max_days, max_days, limit))


def kpi_admissions_daily(conn, start: date, end: date) -> pd.DataFrame:
    lo, hi = day_bounds(start, end)
    return query_df(conn, """
        SELECT date(fecha_ingreso) AS fecha, clase_ingreso, COUNT(*) AS ingresos
        FROM ingresos WHERE fecha_ingreso BETWEEN ? AND ? GROUP BY 1, 2 ORDER BY 1""", (lo, hi))


def kpi_demographics(conn, start: date, end: date) -> dict[str, pd.DataFrame]:
    lo, hi = day_bounds(start, end)
    base = """FROM ingresos i JOIN pacientes p ON p.id_paciente = i.id_paciente
              WHERE i.fecha_ingreso BETWEEN ? AND ?"""
    out = {}
    for key in ("sexo", "regimen", "grupo_etario", "zona"):
        out[key] = query_df(conn, f"SELECT p.{key} AS categoria, COUNT(DISTINCT i.id_paciente) AS pacientes "
                                  f"{base} GROUP BY 1 ORDER BY 2 DESC", (lo, hi))
    return out


def kpi_specialty_demand(conn, start: date, end: date, n: int = 10) -> pd.DataFrame:
    lo, hi = day_bounds(start, end)
    return query_df(conn, """
        SELECT especialidad, COUNT(*) AS servicios, COUNT(DISTINCT oid_ingreso) AS ingresos
        FROM servicios WHERE fecha_prestacion BETWEEN ? AND ?
        GROUP BY 1 ORDER BY servicios DESC LIMIT ?""", (lo, hi, n))


def kpi_service_area_demand(conn, start: date, end: date, n: int = 10) -> pd.DataFrame:
    lo, hi = day_bounds(start, end)
    return query_df(conn, """
        SELECT area_servicio, COUNT(*) AS servicios, COUNT(DISTINCT oid_ingreso) AS ingresos
        FROM servicios WHERE fecha_prestacion BETWEEN ? AND ?
        GROUP BY 1 ORDER BY servicios DESC LIMIT ?""", (lo, hi, n))


def kpi_admissions_by_service(conn, start: date, end: date) -> pd.DataFrame:
    lo, hi = day_bounds(start, end)
    return query_df(conn, """
        SELECT servicio, subgrupo_cama, COUNT(DISTINCT id_paciente) AS pacientes, COUNT(*) AS ingresos
        FROM ingresos WHERE fecha_ingreso BETWEEN ? AND ?
        GROUP BY 1, 2 ORDER BY pacientes DESC""", (lo, hi))


def kpi_surgeries(conn, start: date, end: date) -> dict:
    """Uso de quirófanos: programadas (con ingreso en el periodo) vs. realizadas."""
    lo, hi = day_bounds(start, end)
    df = query_df(conn, """
        SELECT estado, COUNT(*) AS programaciones FROM cirugias
        WHERE en_periodo = 1 AND fecha_ingreso BETWEEN ? AND ? GROUP BY 1""", (lo, hi))
    total = int(df["programaciones"].sum())
    done = int(df.loc[df["estado"] == "Realizada", "programaciones"].sum())
    by_area = query_df(conn, """
        SELECT area_quirofano, COUNT(*) AS cirugias FROM cirugias
        WHERE estado = 'Realizada' AND fecha_cirugia BETWEEN ? AND ? GROUP BY 1 ORDER BY 2 DESC""", (lo, hi))
    return {"programadas": total, "realizadas": done,
            "cumplimiento_pct": round(done / total * 100, 1) if total else None,
            "por_estado": df, "por_area": by_area}


def kpi_demand_trend(conn, ref: date | None = None, by: str = "capitulo_cie10",
                     min_baseline: float = 5) -> pd.DataFrame:
    """Ingresos de los últimos 7 días vs. promedio semanal de las 4 semanas previas.

    Por defecto agrupa por capítulo CIE-10. No se agrupa por servicio de cama porque el HIS guarda
    la ÚLTIMA cama del episodio: los ingresos recientes aún están en UCI y los antiguos ya
    migraron a otra cama, lo que fabrica 'picos' falsos en servicios de paso."""
    if by not in {"capitulo_cie10", "via_ingreso", "clase_ingreso", "tipo_riesgo"}:
        raise ValueError("Agrupación no permitida")
    ref = ref or get_reference_date(conn)
    recent_lo, hi = day_bounds(ref - timedelta(days=6), ref)
    base_lo, base_hi = day_bounds(ref - timedelta(days=34), ref - timedelta(days=7))
    df = query_df(conn, f"""
        SELECT {by} AS categoria,
               SUM(CASE WHEN fecha_ingreso BETWEEN :rlo AND :hi THEN 1 ELSE 0 END) AS ultimos_7d,
               ROUND(SUM(CASE WHEN fecha_ingreso BETWEEN :blo AND :bhi THEN 1 ELSE 0 END) / 4.0, 1)
                 AS promedio_semanal_base
        FROM ingresos WHERE {by} IS NOT NULL GROUP BY {by}""",
                  {"rlo": recent_lo, "hi": hi, "blo": base_lo, "bhi": base_hi})
    df = df[df["promedio_semanal_base"] >= min_baseline].copy()
    df["variacion_pct"] = (((df["ultimos_7d"] / df["promedio_semanal_base"]) - 1) * 100).round(1)
    return df.sort_values("variacion_pct", ascending=False).reset_index(drop=True)


def kpi_billed_bed_days(conn, start: date, end: date) -> pd.DataFrame:
    """Días-cama facturados (CUPS de INTERNACIÓN) por mes y tipo de unidad. Complementa la
    ocupación histórica, que por diseño del extracto subestima UCI e intermedios."""
    lo, hi = day_bounds(start, end)
    df = query_df(conn, """
        SELECT substr(fecha_prestacion, 1, 7) AS mes, nombre_servicio, SUM(cantidad) AS dias_cama
        FROM servicios WHERE nombre_servicio LIKE 'INTERNACI%' AND fecha_prestacion BETWEEN ? AND ?
        GROUP BY 1, 2""", (lo, hi))
    name = df["nombre_servicio"].str.upper()
    df["unidad"] = np.select(
        [name.str.contains("INTENSIVO"), name.str.contains("INTERMEDIO"), name.str.contains("BÁSICO|BASICO"),
         name.str.contains("PEDI")],
        ["UCI", "Cuidado Intermedio", "Cuidado Básico Neonatal", "Hospitalización pediátrica"],
        default="Hospitalización adultos")
    return df.groupby(["mes", "unidad"], as_index=False)["dias_cama"].sum()


def records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> lista JSON-segura (sin NaN ni tipos numpy)."""
    return json.loads(df.to_json(orient="records", force_ascii=False))


def _plain(d: dict) -> dict:
    return json.loads(json.dumps({k: v for k, v in d.items() if not isinstance(v, pd.DataFrame)}, default=float))


def compute_kpis(conn, start: date | None = None, end: date | None = None) -> dict:
    """Resumen serializable (lo expone GET /api/kpis)."""
    ref = get_reference_date(conn)
    end = end or ref
    start = start or month_to_date(ref)[0]
    waits = kpi_wait_times(conn, start, end)
    surg = kpi_surgeries(conn, start, end)
    return {
        "fecha_referencia": ref.isoformat(),
        "periodo": {"inicio": start.isoformat(), "fin": end.isoformat()},
        "ocupacion_global": kpi_global_occupancy(conn, end),
        "ocupacion_por_servicio": records(kpi_bed_occupancy(conn, end)),
        "espera_urgencias": _plain(waits),
        "espera_por_triage": records(waits["por_triage"]),
        "top_diagnosticos": records(kpi_top_diagnoses(conn, start, end, 5)),
        "top_medicamentos": records(kpi_item_consumption(conn, start, end, 5)),
        "stock_critico": records(kpi_critical_stock(conn, limit=10)),
        "cirugias": _plain(surg),
        "tendencia_demanda_cie10": records(kpi_demand_trend(conn, end)),
    }


# ---------------------------------------------------------------------------
# Operación del día: ubicación de camas y cola de urgencias
# ---------------------------------------------------------------------------
# El código de cama del HIS trae el piso y la habitación en las alas de hospitalización:
#   H-203C -> piso 2, habitación 203, cama C   ·   G-108B -> piso 1, habitación 108, cama B
# Las demás unidades (UCI, intermedios, observación...) solo traen unidad + número de cama.
_ROOM_CODE = re.compile(r"^([GH])-(\d)(\d{2})([A-Z]?)$")
# Metas de oportunidad por nivel de triage (Resolución 5596 de 2015): I inmediata, II hasta 30 min.
# Los niveles III a V no tienen meta nacional; cada institución define la suya.
TRIAGE_TARGET_MIN = {1: 0, 2: config.WAIT_TARGET_TRIAGE2_MIN}


def unit_label(subgroup: str | None) -> str:
    """Nombre legible de la unidad ('UNIDAD DE CUIDAD BASICO NEONATAL' -> 'Unidad De Cuidado Basico Neonatal')."""
    return str(subgroup or "Sin unidad").replace("CUIDAD ", "CUIDADO ").title()


def bed_population(subgroup: str | None) -> str:
    """Población que atiende la unidad: una cama libre solo sirve a pacientes compatibles."""
    name = str(subgroup or "").upper()
    if "NEONAT" in name:
        return "Neonatal"
    if "PEDIATR" in name:
        return "Pediátrica"
    if "GINECO" in name or "PARTO" in name:
        return "Materna"
    if "INTENSIV" in name or "INTERMEDIO" in name:
        return "Crítica adultos"
    if "RECUPERACION" in name:
        return "Recuperación quirúrgica"   # posoperatorio inmediato: no recibe hospitalizaciones
    return "Adultos"


def bed_location(code: str | None, subgroup: str | None = None) -> dict:
    """Piso, habitación y cama a partir del código del HIS. Sin patrón de habitación -> solo unidad."""
    m = _ROOM_CODE.match(str(code or ""))
    unit = unit_label(subgroup)
    if not m:
        return {"piso": None, "habitacion": None, "cama": code, "ubicacion": f"{unit} · Cama {code}"}
    _, floor, room, letter = m.groups()
    room_no = f"{floor}{room}"
    bed = letter or "única"
    return {"piso": int(floor), "habitacion": room_no, "cama": bed,
            "ubicacion": f"Piso {floor} · Hab. {room_no} · Cama {bed}"}


def bed_map(conn, day: date | None = None) -> pd.DataFrame:
    """Estado de cada cama en el día (misma regla que ocupacion_diaria: una estancia estimada que se
    cruza con el día ocupa la cama), con su ubicación física y la población que atiende."""
    day = day or get_reference_date(conn)
    lo, hi = day_bounds(day, day)
    df = query_df(conn, """
        SELECT c.codigo_cama, c.subgrupo_cama, c.servicio, c.es_virtual,
               COUNT(i.oid_ingreso) AS pacientes, MIN(i.fecha_inicio_estancia) AS desde
          FROM camas c
          LEFT JOIN ingresos i ON i.codigo_cama = c.codigo_cama
                              AND i.fecha_inicio_estancia <= ? AND i.fecha_fin_estimada >= ?
         GROUP BY c.codigo_cama, c.subgrupo_cama, c.servicio, c.es_virtual""", (hi, lo))
    loc = pd.DataFrame([bed_location(c, s) for c, s in zip(df["codigo_cama"], df["subgrupo_cama"])],
                       index=df.index)
    df = pd.concat([df, loc], axis=1)
    df["unidad"] = df["subgrupo_cama"].map(unit_label)
    df["poblacion"] = df["subgrupo_cama"].map(bed_population)
    df["ocupada"] = (df["pacientes"] > 0).astype(int)
    since = pd.to_datetime(df["desde"], errors="coerce")
    df["dias_estancia"] = ((pd.Timestamp(hi) - since).dt.total_seconds() / 86400).round(1)
    return df.drop(columns=["desde"]).sort_values(["piso", "habitacion", "cama", "codigo_cama"],
                                                  na_position="last").reset_index(drop=True)


def free_beds(conn, day: date | None = None, subgroup: str | None = None, population: str | None = None,
              limit: int = 10) -> pd.DataFrame:
    """Camas físicas libres de internación (sin Urgencias), primero las que tienen piso y habitación."""
    beds = bed_map(conn, day)
    beds = beds[(beds["ocupada"] == 0) & (beds["es_virtual"] == 0) & (beds["servicio"] != "Urgencias")]
    if subgroup:
        beds = beds[beds["subgrupo_cama"] == subgroup]
    if population:
        beds = beds[beds["poblacion"] == population]
    return beds.head(limit).reset_index(drop=True)


def _triage_area(classification: str | None) -> str:
    """'PEDIATRIA URGENCIAS CONSULTORIO DOS-TRIAGE 2 (NARANJA)' -> 'Pediatria Urgencias Consultorio Dos'."""
    text = re.split(r"-\s*TRIAGE", str(classification or ""), maxsplit=1, flags=re.I)[0]
    return re.sub(r"\s+", " ", text).strip(" -").title() or "Sin clasificar"


def triage_queue(conn, now: datetime | str, lookback_hours: int = 24) -> pd.DataFrame:
    """Pacientes de urgencias que a la hora `now` ya ingresaron y aún no reciben la primera atención.
    Sin identificadores: el código URG-xxxx es un resumen no reversible del ingreso."""
    now = pd.Timestamp(now)
    lo = (now - pd.Timedelta(hours=lookback_hours)).strftime(DATETIME_FMT)
    hi = now.strftime(DATETIME_FMT)
    cols = ["codigo", "nivel_triage", "area", "llegada", "espera_min", "meta_min", "fuera_de_meta",
            "sexo", "grupo_etario"]
    df = query_df(conn, """
        SELECT i.oid_ingreso, i.fecha_ingreso, i.nivel_triage, i.clasificacion_triage, i.subgrupo_cama,
               p.sexo, p.grupo_etario
          FROM ingresos i LEFT JOIN pacientes p ON p.id_paciente = i.id_paciente
         WHERE i.via_ingreso = 'Urgencias' AND i.fecha_ingreso BETWEEN ? AND ?
           AND (i.fecha_atencion IS NULL OR i.fecha_atencion > ?)""", (lo, hi, hi))
    if df.empty:
        return pd.DataFrame(columns=cols)
    arrival = pd.to_datetime(df["fecha_ingreso"])
    df["codigo"] = df["oid_ingreso"].map(
        lambda oid: "URG-" + hashlib.sha1(f"hslv-{oid}".encode()).hexdigest()[:4].upper())
    df["espera_min"] = ((now - arrival).dt.total_seconds() / 60).round(0).astype(int)
    df["llegada"] = arrival.dt.strftime("%H:%M")
    df["area"] = [(_triage_area(c) if isinstance(c, str) else unit_label(s))
                  for c, s in zip(df["clasificacion_triage"], df["subgrupo_cama"])]
    df["meta_min"] = df["nivel_triage"].map(TRIAGE_TARGET_MIN)
    df["fuera_de_meta"] = (df["meta_min"].notna() & (df["espera_min"] > df["meta_min"])).astype(int)
    df = df.sort_values(["nivel_triage", "espera_min"], ascending=[True, False], na_position="last")
    return df[cols].reset_index(drop=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ETL del HIS -> SQLite")
    parser.add_argument("--rebuild", action="store_true", help="Reconstruir la base desde cero")
    args = parser.parse_args()
    build_database(rebuild=args.rebuild)
    with closing(get_connection(read_only=True)) as c:
        summary = compute_kpis(c)
        log.info("Ocupación global: %s", summary["ocupacion_global"])
        log.info("Espera urgencias (mes): %s", summary["espera_urgencias"])
