"""
voice.py — Hablarle al asistente y escuchar sus respuestas.

  * Entrada: el micrófono del chat (st.chat_input con accept_audio) entrega un WAV. Se transcribe con el primer
    servicio disponible: Gemini (si hay GEMINI_API_KEY), OpenAI (si hay OPENAI_API_KEY) o el reconocedor de voz
    gratuito de Google (paquete SpeechRecognition, sin clave). Si ninguno responde, se pide escribir.
  * Salida: el navegador lee la respuesta con su propia voz en español (Web Speech API): no hay costo ni envío
    de datos. El botón "Escuchar" vive dentro del propio componente para que el navegador lo permita.
"""
from __future__ import annotations

import base64
import html
import io
import json
import re

import requests

import config

TIMEOUT = 20


def _gemini(audio: bytes) -> str | None:
    if not config.GEMINI_API_KEY:
        return None
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent",
        headers={"x-goog-api-key": config.GEMINI_API_KEY},
        json={"contents": [{"parts": [
            {"text": "Transcribe literalmente este audio en español. Devuelve solo el texto, sin comentarios."},
            {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(audio).decode()}}]}],
            "generationConfig": {"temperature": 0}},
        timeout=TIMEOUT)
    r.raise_for_status()
    return "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"]).strip() or None


def _openai(audio: bytes) -> str | None:
    if not config.OPENAI_API_KEY:
        return None
    r = requests.post("https://api.openai.com/v1/audio/transcriptions",
                      headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                      files={"file": ("voz.wav", audio, "audio/wav")},
                      data={"model": "whisper-1", "language": "es"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("text", "").strip() or None


def _google_free(audio: bytes) -> str | None:
    try:
        import speech_recognition as sr
    except ImportError:
        return None
    rec = sr.Recognizer()
    with sr.AudioFile(io.BytesIO(audio)) as source:
        data = rec.record(source)
    try:
        return rec.recognize_google(data, language="es-CO").strip() or None
    except sr.UnknownValueError:
        return None


def transcribe(audio: bytes) -> tuple[str | None, str]:
    """Devuelve (texto, motor usado) o (None, motivo)."""
    if not audio:
        return None, "sin audio"
    for name, fn in (("Gemini", _gemini), ("OpenAI Whisper", _openai), ("Google (gratuito)", _google_free)):
        try:
            text = fn(audio)
        except Exception:  # red caída, cuota, formato: se intenta el siguiente
            continue
        if text:
            return text, name
    return None, "No se entendió el audio o no hay servicio de voz disponible. Escribe la pregunta."


def plain(text: str, limit: int = 900) -> str:
    """Quita markdown/HTML para que la voz no lea asteriscos ni etiquetas."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"[*_`#>|]", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def speak_widget(text: str, key: str) -> str:
    """HTML con botones Escuchar / Detener (Web Speech API, voz del navegador en español)."""
    payload = json.dumps(plain(text)).replace("</", "<\\/")
    return f"""
<div style="font-family:sans-serif;display:flex;gap:6px">
  <button id="p{html.escape(key)}" style="border:1px solid #D5DBE3;background:#fff;border-radius:8px;padding:4px 10px;
      cursor:pointer;font-size:13px">&#128266; Escuchar</button>
  <button id="s{html.escape(key)}" style="border:1px solid #D5DBE3;background:#fff;border-radius:8px;padding:4px 10px;
      cursor:pointer;font-size:13px">Detener</button>
</div>
<script>
  const t = {payload};
  document.getElementById("p{html.escape(key)}").onclick = () => {{
    const s = window.speechSynthesis; s.cancel();
    const u = new SpeechSynthesisUtterance(t); u.lang = "es-CO"; u.rate = 1.0;
    const v = s.getVoices().find(v => v.lang && v.lang.startsWith("es")); if (v) u.voice = v;
    s.speak(u);
  }};
  document.getElementById("s{html.escape(key)}").onclick = () => window.speechSynthesis.cancel();
</script>"""
