"""
file_assistant.py — El asistente también responde sobre archivos que sube el usuario.

  * CSV / Excel: se leen con pandas (máx. 20 MB). Siempre responde con un resumen verificable (filas, columnas,
    vacíos, estadísticas y vista previa). Si hay IA configurada, además responde la pregunta en lenguaje natural
    usando solo ese resumen y una muestra (nunca inventa columnas).
  * PDF: extrae el texto (pypdf). Con IA configurada responde la pregunta sobre el documento; sin IA, muestra el
    comienzo del texto y las páginas.
  * Imágenes (PNG/JPG): solo con Gemini (multimodal); sin IA lo dice claramente.
Los archivos no se guardan en el servidor. Si hay IA configurada, el contenido se envía al proveedor: la pantalla lo
advierte para no subir datos identificables de pacientes.
"""
from __future__ import annotations

import base64
import io
import re
from pathlib import Path

import pandas as pd
import requests

import config
from agent import AgentResponse, create_llm_client, fmt_num

MAX_BYTES = 20 * 1024 * 1024
FILE_TYPES = ["csv", "xlsx", "xls", "pdf", "png", "jpg", "jpeg", "txt"]
SYSTEM = ("Eres el asistente del Hospital Susana López de Valencia. Responde en español, breve y preciso, SOLO con "
          "la información del archivo que te doy. Si el dato no está, dilo. No inventes cifras. Nunca escribas "
          "nombres, documentos, datos de contacto ni diagnósticos de pacientes: resume en cifras agregadas.")


def _read_table(name: str, data: bytes) -> pd.DataFrame:
    ext = Path(name).suffix.lower()
    if ext in (".xlsx", ".xls"):
        return pd.read_excel(io.BytesIO(data))
    for sep in (",", ";", "|", "\t"):
        try:
            df = pd.read_csv(io.BytesIO(data), sep=sep, encoding="utf-8-sig", low_memory=False)
            if df.shape[1] > 1:
                return df
        except (UnicodeDecodeError, pd.errors.ParserError):
            continue
    return pd.read_csv(io.BytesIO(data), sep=None, engine="python", encoding="latin-1")


PII_COLUMN = re.compile(r"nombre|apellido|document|cedula|c[eé]dula|identific|^id_?paciente$|tel[eé]fono|celular|"
                        r"direcci[oó]n|correo|e-?mail|diagn[oó]stico|cie-?10|historia", re.IGNORECASE)


def mask_identifiers(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Quita columnas que identifican pacientes (nombre, documento, contacto, diagnóstico). Nunca salen ni van a la IA."""
    hidden = [str(c) for c in df.columns if PII_COLUMN.search(str(c))]
    return df.drop(columns=hidden), hidden


def table_summary(df: pd.DataFrame) -> tuple[str, pd.DataFrame]:
    """Resumen legible + tabla por columna (tipo, vacíos, ejemplo / promedio)."""
    rows = []
    for col in df.columns:
        s = df[col]
        info = {"columna": str(col), "tipo": "número" if pd.api.types.is_numeric_dtype(s) else "texto/fecha",
                "vacíos": int(s.isna().sum()), "distintos": int(s.nunique())}
        if pd.api.types.is_numeric_dtype(s) and s.notna().any():
            info.update(mínimo=f"{float(s.min()):g}", promedio=f"{float(s.mean()):.2f}".rstrip("0").rstrip("."),
                        máximo=f"{float(s.max()):g}")
        else:
            top = s.dropna().astype(str).value_counts().head(1)
            info["más frecuente"] = f"{top.index[0]} ({top.iat[0]})" if not top.empty else "—"
        rows.append(info)
    text = (f"El archivo tiene **{fmt_num(len(df))} filas** y **{df.shape[1]} columnas**: "
            + ", ".join(f"`{c}`" for c in list(df.columns)[:15]) + ("…" if df.shape[1] > 15 else "") + ".")
    missing = int(df.isna().sum().sum())
    if missing:
        text += f" Hay {fmt_num(missing)} celdas vacías."
    return text, pd.DataFrame(rows).fillna("—")


def _pdf_text(data: bytes) -> tuple[str, int]:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages), len(reader.pages)


def _gemini_file(question: str, mime: str, data: bytes) -> str:
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent",
        headers={"x-goog-api-key": config.GEMINI_API_KEY},
        json={"systemInstruction": {"parts": [{"text": SYSTEM}]},
              "contents": [{"parts": [{"text": question}, {"inline_data": {"mime_type": mime,
                                                                            "data": base64.b64encode(data).decode()}}]}],
              "generationConfig": {"temperature": 0}},
        timeout=config.LLM_TIMEOUT_SECONDS)
    r.raise_for_status()
    return "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"]).strip()


def answer(question: str, name: str, data: bytes, mime: str | None = None) -> AgentResponse:
    question = (question or "").strip() or "Resume este archivo."
    ext = Path(name).suffix.lower().lstrip(".")
    q = f"{question} (archivo: {name})"
    if len(data) > MAX_BYTES:
        return AgentResponse(q, "El archivo supera 20 MB.", engine="archivo")
    if ext not in FILE_TYPES:
        return AgentResponse(q, "Formato no soportado. Sube CSV, Excel, PDF, TXT o una imagen PNG/JPG.", engine="archivo")
    llm = create_llm_client()
    try:
        if ext in ("csv", "xlsx", "xls"):
            df, hidden = mask_identifiers(_read_table(name, data))
            text, cols = table_summary(df)
            if hidden:
                text += (" Por privacidad oculté las columnas que identifican pacientes: "
                         + ", ".join(f"`{c}`" for c in hidden) + ".")
            if llm:
                context = (f"Resumen por columna:\n{cols.to_string(index=False)}\n\nPrimeras filas:\n"
                           f"{df.head(30).to_string(index=False)[:6000]}")
                try:
                    reply = llm.complete(SYSTEM, [{"role": "user", "content": f"{context}\n\nPregunta: {question}"}])
                    text = reply.strip() + "\n\n" + text
                    engine = "llm"
                except Exception:
                    engine = "archivo"
            else:
                engine = "archivo"
            return AgentResponse(q, text, data=cols, engine=engine)
        if ext in ("pdf", "txt"):
            if ext == "pdf":
                content, pages = _pdf_text(data)
            else:
                content, pages = data.decode("utf-8", errors="replace"), 1
            if not content.strip():
                return AgentResponse(q, "El PDF no tiene texto seleccionable (parece escaneado).", engine="archivo")
            if llm:
                try:
                    reply = llm.complete(SYSTEM, [{"role": "user", "content": f"Documento ({pages} páginas):\n"
                                                   f"{content[:15000]}\n\nPregunta: {question}"}])
                    return AgentResponse(q, reply.strip(), engine="llm")
                except Exception:
                    pass
            preview = content.strip()[:1200]
            return AgentResponse(q, f"Documento de **{pages} página(s)**. Sin IA configurada no puedo responder "
                                    f"preguntas abiertas sobre él; este es el comienzo del texto:\n\n> "
                                 + preview.replace("\n", "\n> "), engine="archivo")
        # imágenes
        if config.GEMINI_API_KEY:
            try:
                return AgentResponse(q, _gemini_file(question, mime or f"image/{'jpeg' if ext == 'jpg' else ext}", data),
                                     engine="llm")
            except Exception:
                pass
        return AgentResponse(q, "Para analizar imágenes se necesita la IA con visión (Gemini). Configura "
                                "LLM_PROVIDER=gemini y GEMINI_API_KEY en el archivo .env.", engine="archivo")
    except Exception as exc:  # archivo dañado o formato raro: respuesta clara, sin tumbar la página
        return AgentResponse(q, f"No pude leer el archivo: {type(exc).__name__}.", engine="archivo")
