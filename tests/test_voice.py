"""Voz: limpieza de texto para leer y orden de respaldo de la transcripción."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import voice  # noqa: E402


def test_plain_removes_markdown_and_html():
    assert voice.plain("**Hola** <b>mundo</b> `x` [enlace](http://a)") == "Hola mundo x enlace"


def test_transcribe_falls_back_in_order(monkeypatch):
    calls = []
    monkeypatch.setattr(voice, "_gemini", lambda a: calls.append("g") or None)
    monkeypatch.setattr(voice, "_openai", lambda a: (_ for _ in ()).throw(RuntimeError("sin red")))
    monkeypatch.setattr(voice, "_google_free", lambda a: "cuántas camas de uci hay")
    assert voice.transcribe(b"RIFF") == ("cuántas camas de uci hay", "Google (gratuito)")
    assert calls == ["g"]
    assert voice.transcribe(b"")[0] is None


def test_speak_widget_is_escaped():
    html = voice.speak_widget('Respuesta "con" </script> comillas', "k1")
    assert "Escuchar" in html and "</script> comillas" not in html.split("<script>")[1].split("</script>")[0]
