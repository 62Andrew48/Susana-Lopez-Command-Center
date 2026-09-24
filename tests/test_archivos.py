"""El asistente responde sobre archivos subidos, sin exponer datos que identifiquen pacientes."""
import io

import pandas as pd
import pytest

import file_assistant as fa


@pytest.fixture(autouse=True)
def sin_ia(monkeypatch):
    monkeypatch.setattr(fa, "create_llm_client", lambda: None)
    monkeypatch.setattr(fa.config, "GEMINI_API_KEY", "")


def test_csv_resumen_y_columnas_privadas_ocultas():
    csv = "nombre;documento;servicio;dias_estancia\nAna;123;UCI;4\nLuis;456;Pediatría;2\nEva;789;UCI;6\n"
    r = fa.answer("¿Cuántas filas hay?", "pacientes.csv", csv.encode())
    assert r.engine == "archivo"
    assert "3 filas" in r.answer and "2 columnas" in r.answer
    assert "nombre" in r.answer and "documento" in r.answer  # avisa qué ocultó
    assert "Ana" not in r.answer and "123" not in r.answer
    assert list(r.data["columna"]) == ["servicio", "dias_estancia"]
    fila = r.data.set_index("columna").loc["dias_estancia"]
    assert fila["promedio"] == "4" and fila["máximo"] == "6"


def test_excel():
    buf = io.BytesIO()
    pd.DataFrame({"medicamento": ["A", "B"], "stock": [10, 0]}).to_excel(buf, index=False)
    r = fa.answer("", "inventario.xlsx", buf.getvalue())
    assert "2 filas" in r.answer and r.data is not None


def test_txt_sin_ia_muestra_comienzo():
    r = fa.answer("¿De qué trata?", "nota.txt", "Protocolo de lavado de manos.\nPaso 1".encode())
    assert "Protocolo de lavado" in r.answer


def test_pdf_sin_texto_y_pdf_con_texto():
    from pypdf import PdfWriter
    buf = io.BytesIO()
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.write(buf)
    assert "escaneado" in fa.answer("", "vacio.pdf", buf.getvalue()).answer

    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, "Ocupacion UCI 92 por ciento")
    c.save()
    r = fa.answer("", "informe.pdf", buf.getvalue())
    assert "1 página" in r.answer and "Ocupacion UCI" in r.answer


def test_imagen_sin_gemini_y_formatos_invalidos():
    assert "Gemini" in fa.answer("", "foto.png", b"\x89PNG").answer
    assert "no soportado" in fa.answer("", "virus.exe", b"MZ").answer
    assert "20 MB" in fa.answer("", "grande.csv", b"x" * (fa.MAX_BYTES + 1)).answer
    assert "No pude leer" in fa.answer("", "roto.xlsx", b"no es excel").answer


def test_con_ia_no_envia_columnas_privadas(monkeypatch):
    enviado = {}

    class FakeLLM:
        def complete(self, system, messages):
            enviado["texto"] = messages[0]["content"]
            return "La UCI tiene más registros."

    monkeypatch.setattr(fa, "create_llm_client", lambda: FakeLLM())
    csv = "nombre,servicio\nAna,UCI\nLuis,UCI\n"
    r = fa.answer("¿Qué servicio se repite?", "p.csv", csv.encode())
    assert r.engine == "llm" and r.answer.startswith("La UCI")
    assert "Ana" not in enviado["texto"] and "nombre" not in enviado["texto"]
