"""
hc_documents.py — Historia clínica en PDF y exportación / importación entre sistemas (JSON).

  * patients_pdf : historia completa de uno o varios pacientes (datos, ficha con alergias, registros con sus
                   correcciones y anulaciones, fórmulas, lista de adjuntos). Marca "confidencial".
  * export_json  : formato HSLV-HC v1 (JSON UTF-8) con los adjuntos en base64 y su huella SHA-256.
  * import_json  : crea o actualiza pacientes (por documento o, si vienen del extracto, por id), su ficha,
                   registros (conservan fecha y autor originales en `origen_externo`) y adjuntos (se verifica la
                   huella). No duplica: un registro ya importado (mismo tipo, fecha y contenido) se omite.
No depende de Streamlit.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import clinical_records as cr
import pharmacy_service as ps

FORMAT, VERSION = "HSLV-HC", 1
HOSPITAL = "Hospital Susana López de Valencia E.S.E."
LOGO = Path(__file__).resolve().parent / "assets" / "logo_hslv_icono.png"


def _ts(v: str | None) -> str:
    return f"{v[8:10]}/{v[5:7]}/{v[:4]} {v[11:16]}" if v and len(v) >= 16 else (v or "—")


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def patients_pdf(conn: sqlite3.Connection, ids: list[int], generated_by: str, now: str,
                 include_annulled: bool = True) -> bytes:
    from xml.sax.saxutils import escape as x

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    navy, green, muted = colors.HexColor("#2E3378"), colors.HexColor("#507643"), colors.HexColor("#5B6472")
    red_bg, red = colors.HexColor("#FDE2E1"), colors.HexColor("#991B1B")
    base = ParagraphStyle("b", fontName="Helvetica", fontSize=9.5, leading=13)
    small = ParagraphStyle("s", parent=base, fontSize=8, leading=10.5, textColor=muted)
    bold = ParagraphStyle("bo", parent=base, fontName="Helvetica-Bold")
    h1 = ParagraphStyle("h1", parent=base, fontName="Helvetica-Bold", fontSize=14, leading=18, textColor=navy)
    h2 = ParagraphStyle("h2", parent=base, fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=navy,
                        spaceBefore=8, spaceAfter=3)
    rec_title = ParagraphStyle("rt", parent=base, fontName="Helvetica-Bold", fontSize=10.5, leading=13)

    def p(text, style=base):
        return Paragraph(x(str(text)).replace("\n", "<br/>"), style)

    def kv_table(rows, widths=(4.6 * cm, 12.9 * cm), bg=None):
        t = Table([[p(k, bold), p(v)] for k, v in rows], colWidths=widths)
        style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#D5DBE3")),
                 ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        if bg:
            style.append(("BACKGROUND", (0, 0), (-1, -1), bg))
        t.setStyle(TableStyle(style))
        return t

    story = []
    for n, pid in enumerate(ids):
        pt = cr.get_patient(conn, pid)
        if pt is None:
            continue
        if n:
            story.append(PageBreak())
        logo = Image(str(LOGO), width=1.5 * cm, height=1.5 * cm) if LOGO.exists() else Spacer(1, 1)
        head = Table([[logo, [p(HOSPITAL, ParagraphStyle("t", parent=bold, textColor=green)),
                              p("Historia clínica", h1),
                              p(f"Generada el {_ts(now)} por {generated_by} · Documento confidencial "
                                "(Ley 1581 de 2012 y Res. 1995 de 1999)", small)]]],
                     colWidths=[1.9 * cm, 15.6 * cm])
        head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                  ("LINEBELOW", (0, 0), (-1, 0), 1.5, colors.HexColor("#8AB94F"))]))
        story += [head, Spacer(1, 8), p("Datos del paciente", h2)]
        doc = f"{pt['tipo_documento']} {pt['numero_documento']}" if pt["numero_documento"] else "Sin documento (extracto anonimizado)"
        story.append(kv_table([
            ("Paciente", cr.display_name(pt)), ("Documento", doc), ("Id interno", pt["id_paciente"]),
            ("Edad · sexo", f"{pt['edad'] if pt['edad'] is not None else '—'} años · {pt['sexo'] or '—'}"),
            ("Asegurador · régimen", f"{pt['asegurador'] or '—'} · {pt['regimen'] or '—'}"),
            ("Contacto", f"{pt['telefono'] or '—'} · {pt['direccion'] or ''} {pt['municipio'] or ''}".strip()),
        ]))
        ficha = cr.get_ficha(conn, pid)
        story.append(p("Ficha clínica", h2))
        if ficha is None:
            story.append(p("Sin ficha registrada.", small))
        else:
            allergy = ("Sin alergias conocidas" if ficha["sin_alergias_conocidas"]
                       else ficha["alergias"] or "Sin dato (preguntar al paciente)")
            story.append(kv_table([("ALERGIAS", allergy)], bg=red_bg if ficha["alergias"] else None))
            story.append(kv_table([(label, ficha[key] or "—") for key, label in (
                ("grupo_sanguineo", "Grupo sanguíneo"), ("antecedentes_personales", "Antecedentes personales"),
                ("antecedentes_quirurgicos", "Antecedentes quirúrgicos"),
                ("antecedentes_familiares", "Antecedentes familiares"), ("medicacion_habitual", "Medicación habitual"),
                ("habitos", "Hábitos"), ("contacto_emergencia", "Contacto de emergencia"),
                ("telefono_emergencia", "Teléfono de emergencia"), ("observaciones", "Observaciones"))]))
        story.append(p("Registros clínicos", h2))
        recs = cr.records(conn, pid, include_annulled=include_annulled)
        files = [dict(a) for a in cr.attachments(conn, pid, include_annulled=False)]
        if not recs:
            story.append(p("Sin registros.", small))
        for r in reversed(recs):  # orden cronológico
            block = [p(f"{_ts(r['creado_en'])} · {cr.RECORD_TYPES.get(r['tipo'], r['tipo'])} · {r['titulo']}"
                       + ("  [ANULADO]" if r["estado"] == "ANULADO" else ""),
                       ParagraphStyle("rtx", parent=rec_title, textColor=red if r["estado"] == "ANULADO" else colors.black)),
                     p(f"Profesional: {r['autor']}" + (f" · versión {r['version']} (corregido {_ts(r['actualizado_en'])}: "
                                                       f"{r['motivo_cambio']})" if r["version"] > 1 else ""), small),
                     p(r["contenido"])]
            if r["diagnostico_cie10"] or r["diagnostico_nombre"]:
                block.append(p(f"Diagnóstico: {r['diagnostico_cie10'] or ''} {r['diagnostico_nombre'] or ''}", bold))
            if r["plan"]:
                block.append(p(f"Plan: {r['plan']}"))
            attached = [a["nombre_archivo"] for a in files if a["registro_id"] == r["id"]]
            if attached:
                block.append(p("Adjuntos: " + ", ".join(attached), small))
            if r["estado"] == "ANULADO":
                block.append(p(f"Anulado: {r['motivo_anulacion']}", small))
            block.append(Spacer(1, 6))
            story.append(KeepTogether(block))
        rx = ps.patient_prescriptions(conn, pid)
        story.append(p("Fórmulas", h2))
        if not rx:
            story.append(p("Sin fórmulas.", small))
        else:
            t = Table([[p(h, bold) for h in ("Fecha", "Medicamento", "Dosis", "Entregadas", "Estado")]] +
                      [[p(_ts(r["fecha_prescripcion"])), p(str(r["producto"]).capitalize()),
                        p(f"{r['dosis']} c/{r['frecuencia_horas']} h · {r['duracion_dias']} d"),
                        p(f"{r['dosis_entregadas']}/{r['dosis_prescritas']}"), p(r["estado"].capitalize())] for r in rx],
                      colWidths=[2.6 * cm, 6.4 * cm, 4.3 * cm, 2 * cm, 2.2 * cm], repeatRows=1)
            t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#D5DBE3")),
                                   ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF4E8")),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            story.append(t)
        loose = [a for a in files if a["registro_id"] is None]
        if loose:
            story.append(p("Otros documentos adjuntos", h2))
            for a in loose:
                story.append(p(f"• {a['nombre_archivo']} · {a['descripcion'] or ''} · {_ts(a['subido_en'])} · "
                               f"SHA-256 {a['sha256'][:16]}…", small))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(muted)
        canvas.drawString(2 * cm, 1.2 * cm, f"{HOSPITAL} · Historia clínica · confidencial · datos de demostración")
        canvas.drawRightString(letter[0] - 2 * cm, 1.2 * cm, f"Página {doc.page}")
        canvas.restoreState()

    if not story:
        story = [p("Sin pacientes para imprimir.")]
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=letter, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=1.5 * cm,
                      bottomMargin=2 * cm, title="Historia clínica HSLV", author=HOSPITAL).build(
        story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Exportar / importar (JSON)
# ---------------------------------------------------------------------------
_PATIENT_OUT = ("id_paciente", "origen", *cr.PATIENT_FIELDS, "edad", "estado")


def export_json(conn: sqlite3.Connection, ids: list[int], exported_by: str, now: str) -> bytes:
    patients = []
    for pid in ids:
        pt = cr.get_patient(conn, pid)
        if pt is None:
            continue
        ficha = cr.get_ficha(conn, pid)
        recs = cr.records(conn, pid, include_annulled=True)
        files = []
        for a in cr.attachments(conn, pid, include_annulled=False):
            name, mime, data = cr.attachment_content(conn, a["id"], pid)
            files.append({"nombre_archivo": name, "tipo_mime": mime, "sha256": a["sha256"],
                          "descripcion": a["descripcion"], "subido_en": a["subido_en"],
                          "registro_ref": a["registro_id"], "contenido_base64": base64.b64encode(data).decode()})
        patients.append({
            "paciente": {k: pt[k] for k in _PATIENT_OUT},
            "ficha": {k: ficha[k] for k in cr.FICHA_FIELDS} if ficha else None,
            "registros": [{"ref": r["id"], "tipo": r["tipo"], "titulo": r["titulo"], "contenido": r["contenido"],
                           "diagnostico_cie10": r["diagnostico_cie10"], "diagnostico_nombre": r["diagnostico_nombre"],
                           "plan": r["plan"], "autor": r["autor"], "creado_en": r["creado_en"],
                           "version": r["version"], "estado": r["estado"], "motivo_anulacion": r["motivo_anulacion"]}
                          for r in recs],
            "adjuntos": files,
        })
    doc = {"formato": FORMAT, "version": VERSION, "sistema_origen": HOSPITAL, "exportado_en": now,
           "exportado_por": exported_by, "pacientes": patients}
    return json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")


def _find_patient(conn, p: dict) -> int | None:
    if p.get("numero_documento"):
        row = conn.execute("SELECT id_paciente FROM pacientes_clinicos WHERE tipo_documento = ? AND numero_documento = ?",
                           (p.get("tipo_documento"), p["numero_documento"])).fetchone()
        if row:
            return row[0]
    if p.get("origen") == "HIS" and p.get("id_paciente"):
        row = conn.execute("SELECT id_paciente FROM pacientes_clinicos WHERE id_paciente = ? AND origen = 'HIS'",
                           (p["id_paciente"],)).fetchone()
        if row:
            return row[0]
    return None


def import_json(conn: sqlite3.Connection, raw: bytes, user_id: int, now: str) -> dict:
    """Devuelve un resumen {pacientes_nuevos, pacientes_existentes, registros, omitidos, adjuntos, errores}."""
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise sqlite3.IntegrityError("El archivo no es un JSON válido") from exc
    if doc.get("formato") != FORMAT or not isinstance(doc.get("pacientes"), list):
        raise sqlite3.IntegrityError(f"Formato no reconocido: se espera {FORMAT} (exportado desde esta app)")
    source = doc.get("sistema_origen") or "sistema externo"
    out = {"pacientes_nuevos": 0, "pacientes_existentes": 0, "registros": 0, "omitidos": 0, "adjuntos": 0,
           "errores": []}
    for item in doc["pacientes"]:
        p = item.get("paciente") or {}
        try:
            pid = _find_patient(conn, p)
            if pid is None:
                if p.get("origen") == "HIS" and not p.get("numero_documento"):
                    import config
                    pid = cr.import_his_patient(conn, config.DB_PATH, int(p["id_paciente"]), user_id, now)
                else:
                    pid = cr.register_patient(conn, user_id, p, now)
                out["pacientes_nuevos"] += 1
            else:
                out["pacientes_existentes"] += 1
            if item.get("ficha"):
                cr.save_ficha(conn, pid, user_id, item["ficha"], now)
            refs = {}
            hc = cr.ensure_history(conn, pid, now)
            for r in item.get("registros") or []:
                dup = conn.execute("SELECT id FROM hc_registros WHERE historia_id = ? AND tipo = ? AND creado_en = ? "
                                   "AND contenido = ?", (hc, r["tipo"], r["creado_en"], r["contenido"])).fetchone()
                if dup:
                    refs[r.get("ref")] = dup[0]
                    out["omitidos"] += 1
                    continue
                origin = f"Importado de {source}: autor original {r.get('autor') or '—'}, registrado {r['creado_en']}"
                with conn:
                    rid = conn.execute(
                        "INSERT INTO hc_registros(historia_id, tipo, titulo, contenido, diagnostico_cie10, "
                        "diagnostico_nombre, plan, autor_id, creado_en, origen_externo) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (hc, r["tipo"], r["titulo"], r["contenido"], r.get("diagnostico_cie10"),
                         r.get("diagnostico_nombre"), r.get("plan"), user_id, r["creado_en"], origin)).lastrowid
                    if r.get("estado") == "ANULADO":
                        conn.execute("UPDATE hc_registros SET estado = 'ANULADO', motivo_anulacion = ?, "
                                     "actualizado_por = ?, actualizado_en = ? WHERE id = ?",
                                     (r.get("motivo_anulacion") or "Anulado en el sistema de origen", user_id, now, rid))
                refs[r.get("ref")] = rid
                out["registros"] += 1
            for a in item.get("adjuntos") or []:
                data = base64.b64decode(a["contenido_base64"])
                if hashlib.sha256(data).hexdigest() != a.get("sha256"):
                    out["errores"].append(f"{a.get('nombre_archivo')}: la huella no coincide (archivo alterado)")
                    continue
                if conn.execute("SELECT 1 FROM hc_adjuntos WHERE historia_id = ? AND sha256 = ?",
                                (hc, a["sha256"])).fetchone():
                    continue
                cr.add_attachment(conn, id_paciente=pid, user_id=user_id, filename=a["nombre_archivo"], data=data,
                                  registro_id=refs.get(a.get("registro_ref")), descripcion=a.get("descripcion"),
                                  now=now)
                out["adjuntos"] += 1
        except (sqlite3.IntegrityError, KeyError, ValueError) as exc:
            out["errores"].append(f"{p.get('nombres') or p.get('id_paciente')}: {exc}")
    return out
