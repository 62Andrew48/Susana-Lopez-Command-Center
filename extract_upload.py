"""
extract_upload.py — Convierte los archivos que sube gerencia (Excel .xlsx/.xls, CSV o el .txt original) al formato
del extracto del HIS: texto UTF-8 delimitado por '|', con el nombre y las columnas que espera database.py.

El archivo se reconoce por su nombre (sin importar mayúsculas ni extensión): Paciente, Ingresos, Atencion, Triage,
Servicios, MedicamentoInsumo, ProgramacionCirugia e Inventario (opcional). Debe traer las mismas columnas del
extracto; si falta alguna se rechaza con un mensaje claro y no se toca la base. No depende de Streamlit.
"""
from __future__ import annotations

import csv
import io
import unicodedata
from pathlib import Path

import pandas as pd

COLUMNS = {
    "Paciente.txt": ["TipoDocumento", "IdPaciente", "NombrePaciente", "FechaNacimiento", "Sexo", "Asegurador",
                     "Regimen", "Departamento", "Municipio", "Zona"],
    "Ingresos.txt": ["OidIngreso", "ConsecutivoIngreso", "IdPaciente", "ClaseIngreso", "ViaIngreso", "TipoRiesgo",
                     "FechaIngreso", "FechaHospitalizacion", "OidTriageA", "CodigoCama", "NombreCama",
                     "NombreGrupoCama", "NombreSubgrupoCama", "CodigoDiagnostico", "NombreDiagnostico"],
    "Atencion.txt": ["OidIngreso", "FechaAtencion"],
    "Triage.txt": ["OidTriage", "FechaTriage", "MotivoConsulta", "TensionArterial", "FrecuenciaCardiaca",
                   "FrecuenciaRespiratoria", "Temperatura", "IdPaciente2", "CodigoTriage", "ClasificacionTriage"],
    "Servicios.txt": ["OidIngreso", "CodigoServicio", "NombreServicio", "Cantidad", "FechaPrestacion",
                      "CodigoAreaServicio", "AreaServicio", "Especialidad", "OidS"],
    "MedicamentoInsumo.txt": ["OidIngreso", "CodigoServicio", "NombreServicio", "Cantidad", "FechaPrestacion",
                              "AreaServicio", "Especialidad", "OidMI"],
    "ProgramacionCirugia.txt": ["ConsecutivoProgramacion", "IdPaciente", "OidIngreso", "CodigoServicio"],
    "Inventario.txt": ["CodigoServicio", "Stock"],
}
EXTENSIONS = ("txt", "csv", "xlsx", "xls")


def _key(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return "".join(ch for ch in text if ch.isalnum())


_BY_STEM = {_key(Path(n).stem): n for n in COLUMNS}


def target_name(filename: str) -> str | None:
    """'ingresos.xlsx' → 'Ingresos.txt'; None si el nombre no corresponde a ninguna tabla del extracto."""
    return _BY_STEM.get(_key(Path(filename).stem))


def _read(filename: str, data: bytes) -> pd.DataFrame:
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext in ("xlsx", "xls"):
        try:
            return pd.read_excel(io.BytesIO(data), dtype=str, engine="xlrd" if ext == "xls" else "openpyxl")
        except ImportError as exc:
            raise ValueError(f"{filename}: falta la librería para leer .{ext} ({exc.name}). Ejecuta "
                             "'pip install -r requirements.txt' o guarda el archivo como .xlsx o .csv.") from exc
    if ext == "csv":
        text = _decode(data)
        header = text.splitlines()[0] if text.strip() else ""
        sep = max(",;|\t", key=header.count) if any(d in header for d in ",;|\t") else ","
        return pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=False)
    if ext == "txt":
        return pd.read_csv(io.StringIO(_decode(data)), sep="|", dtype=str, quoting=csv.QUOTE_NONE,
                           keep_default_na=False)
    raise ValueError(f"{filename}: formato no soportado. Usa .xlsx, .xls, .csv o .txt.")


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _clean(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).replace("|", " ").replace("\r", " ").replace("\n", " ").strip()
    if text.lower() in ("nan", "nat", "none"):
        return ""
    return text


def convert(filename: str, data: bytes) -> tuple[str, bytes, int]:
    """Devuelve (nombre destino, contenido '|' en UTF-8, filas). Lanza ValueError con un mensaje para el usuario."""
    target = target_name(filename)
    if target is None:
        raise ValueError(f"{filename}: el nombre no corresponde a ninguna tabla del extracto "
                         f"({', '.join(Path(n).stem for n in COLUMNS)}).")
    df = _read(filename, data)
    df.columns = [str(c).strip() for c in df.columns]
    by_key = {_key(c): c for c in df.columns}
    expected = COLUMNS[target]
    missing = [c for c in expected if _key(c) not in by_key]
    if missing:
        raise ValueError(f"{filename}: le faltan columnas del extracto: {', '.join(missing)}.")
    out = df[[by_key[_key(c)] for c in expected]].copy()
    out.columns = expected
    out = out.apply(lambda col: col.map(_clean))
    out = out[(out != "").any(axis=1)]                      # filas totalmente vacías de Excel
    lines = ["|".join(expected)] + ["|".join(row) for row in out.itertuples(index=False, name=None)]
    return target, ("\n".join(lines) + "\n").encode("utf-8"), len(out)
