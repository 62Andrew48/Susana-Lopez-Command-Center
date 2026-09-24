"""Carga del extracto en Excel/CSV: se convierte al mismo '|' del HIS sin perder filas ni columnas."""
import io
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import extract_upload as xu  # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "Datos"


def _original(name, n=200):
    return pd.read_csv(DATA / name, sep="|", dtype=str, quoting=3, nrows=n, keep_default_na=False)


@pytest.mark.parametrize("fmt", ["xlsx", "csv"])
def test_excel_y_csv_quedan_igual_que_el_txt(fmt):
    df = _original("Ingresos.txt")
    buf = io.BytesIO()
    df.to_excel(buf, index=False) if fmt == "xlsx" else buf.write(df.to_csv(index=False, sep=";").encode("cp1252",
                                                                                                          "replace"))
    name, content, rows = xu.convert(f"ingresos.{fmt}", buf.getvalue())
    assert name == "Ingresos.txt" and rows == len(df)
    back = pd.read_csv(io.BytesIO(content), sep="|", dtype=str, keep_default_na=False)
    assert list(back.columns) == xu.COLUMNS["Ingresos.txt"]
    assert back["OidIngreso"].tolist() == df["OidIngreso"].tolist()
    assert back["FechaIngreso"].tolist() == df["FechaIngreso"].tolist()


def test_nombre_o_columnas_equivocadas_se_rechazan():
    with pytest.raises(ValueError, match="no corresponde"):
        xu.convert("ventas.csv", b"a,b\n1,2\n")
    with pytest.raises(ValueError, match="faltan columnas"):
        xu.convert("Atencion.csv", b"OidIngreso\n1\n")
    assert xu.target_name("MEDICAMENTO_INSUMO.XLSX") == "MedicamentoInsumo.txt"
    _, content, _ = xu.convert("Atencion.csv", b"FechaAtencion,OidIngreso,Extra\n2026-05-01 03:13:52,7|8,x\n")
    assert content.decode() == "OidIngreso|FechaAtencion\n7 8|2026-05-01 03:13:52\n"
