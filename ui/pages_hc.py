"""
ui/pages_hc.py — Pacientes, historia clínica (registros y adjuntos) y búsqueda de historias.

Quién puede qué (permisos de schema_clinico.sql):
  * pacientes.registrar (médico, gerencia): registrar pacientes, traerlos del extracto del HIS, editar datos.
  * hc.registrar (médico): crear registros y subir adjuntos; corregir o anular SOLO los propios.
  * hc.ver_completa (médico) / hc.ver_notas (enfermería, solo lectura): leer la historia.
  * hc.buscar (médico: con contenido · gerencia: solo el índice, sin contenido clínico).
Toda lectura de un paciente pasa por authorize() y queda en la bitácora; las búsquedas también.
"""
from __future__ import annotations

import sqlite3
from datetime import date

import pandas as pd
import streamlit as st

import clinical_records as cr
import config
import hc_documents as hd
import pharmacy_service as ps
from ui import context as ctx
from ui.theme import BORDER, EVENT_STYLE, GREY, MUTED, RX_LABEL, TEXT, banner, chip, esc, section_title

FMT_IN = "%Y-%m-%d %H:%M:%S"
CSS = f"""
<style>
  .hc-rec {{background:#FFF; border:1px solid {BORDER}; border-left:4px solid var(--c); border-radius:10px;
      padding:0.75rem 0.9rem; margin-bottom:0.35rem;}}
  .hc-rec h4 {{margin:0 0 0.15rem; font-size:1rem; color:{TEXT};}}
  .hc-meta {{font-size:0.78rem; color:{MUTED}; margin-bottom:0.4rem;}}
  .hc-body {{font-size:0.9rem; color:{TEXT}; white-space:pre-wrap;}}
  .hc-body b {{color:{TEXT};}}
  .hc-annulled {{opacity:0.6;}}
  .hc-annulled .hc-body {{text-decoration:line-through;}}
  .hc-pt {{background:#FFF; border:1px solid {BORDER}; border-radius:12px; padding:0.7rem 0.9rem;
      margin-bottom:0.6rem;}}
  .hc-pt b {{font-size:1.05rem;}}
</style>
"""


def _ts(value: str | None) -> str:
    if not value:
        return "—"
    return f"{value[8:10]}/{value[5:7]}/{value[:4]} {value[11:16]}"


def _doc(r) -> str:
    return f"{r['tipo_documento']} {r['numero_documento']}" if r["numero_documento"] else "—"


def _patient_label(r) -> str:
    doc = f" · {r['tipo_documento']} {r['numero_documento']}" if r["numero_documento"] else ""
    origin = " · extracto HIS" if r["origen"] == "HIS" else ""
    return f"{cr.display_name(r)}{doc}{origin} (id {r['id_paciente']})"


def pick_patient(key: str, label: str = "Paciente") -> int | None:
    """Buscador + selector. El paciente elegido se recuerda entre pestañas."""
    clin = ctx.get_clin()
    q = st.text_input(f"Buscar {label.lower()}", key=f"{key}_q",
                      placeholder="Nombre, apellido, número de documento o id")
    rows = cr.search_patients(clin, q, limit=40)
    current = st.session_state.get("clin_patient")
    options = [r["id_paciente"] for r in rows]
    if current and current not in options:
        row = cr.get_patient(clin, current)
        if row is not None and not q:
            rows = [row, *rows]
            options = [current, *options]
    if not options:
        st.info("No hay pacientes con ese criterio. Regístralo en la pestaña Pacientes.")
        return None
    labels = {r["id_paciente"]: _patient_label(r) for r in rows}
    widget = f"{key}_sel"
    if current in options:          # el paciente elegido en otra pestaña manda sobre el estado del selector
        st.session_state[widget] = current
    elif st.session_state.get(widget) not in options:
        st.session_state[widget] = options[0]

    def _remember():
        st.session_state.clin_patient = st.session_state[widget]

    chosen = st.selectbox(label, options, format_func=labels.get, key=widget, on_change=_remember)
    st.session_state.clin_patient = chosen
    return chosen


def gate(permission: str, id_paciente: int, key: str):
    """Autoriza (y audita) el acceso a los datos del paciente. Fuera de turno ofrece 'romper el vidrio'."""
    decision = ctx.authorize_once(permission, id_paciente, ctx.emergency_for(id_paciente))
    if decision.allowed:
        if decision.emergency:
            banner(f"<b>Acceso de emergencia activo</b> para el paciente {id_paciente}. Justificación: "
                   f"“{esc(ctx.emergency_for(id_paciente))}”. Quedó registrado en la bitácora.", "danger")
        return decision
    if "turno" in decision.reason.lower():
        banner(f"<b>Estás fuera de turno.</b> Para abrir los datos del paciente {id_paciente} debes activar el "
               "acceso de emergencia (“romper el vidrio”). La justificación quedará auditada.", "warn")
        with st.form(f"breakglass_{key}"):
            text = st.text_area("Justificación clínica", placeholder="Ej.: paciente en paro cardiorrespiratorio "
                                "en urgencias, requiero antecedentes y medicación actual.")
            if st.form_submit_button("Romper el vidrio y continuar", type="primary", icon=":material/lock_open:"):
                if len(text.strip()) < 20:
                    st.error("Escribe una justificación de al menos 20 caracteres.")
                else:
                    ctx.set_emergency(id_paciente, text.strip())
                    st.rerun()
    else:
        banner(f"Acceso denegado: {esc(decision.reason)}.", "danger")
    return decision


def _patient_header(row) -> None:
    bits = [f"{row['edad']} años" if row["edad"] is not None else None, row["sexo"], row["asegurador"],
            row["regimen"], row["municipio"], f"Tel. {row['telefono']}" if row["telefono"] else None]
    doc = _doc(row) if row["numero_documento"] else ""
    origin = chip("Extracto HIS · anonimizado", "neutral") if row["origen"] == "HIS" else chip("Registrado en la app", "info")
    state = "" if row["estado"] == "ACTIVO" else " " + chip("Inactivo", "warn")
    st.markdown(f'<div class="hc-pt"><b>{esc(cr.display_name(row))}</b> · id {row["id_paciente"]}'
                f'{" · " + esc(doc) if doc else ""} {origin}{state}<br>'
                f'<span style="color:{MUTED};font-size:0.85rem">{esc(" · ".join(str(b) for b in bits if b))}</span>'
                '</div>', unsafe_allow_html=True)


def _err(exc: Exception) -> None:
    st.error(str(exc))


# ===========================================================================
# Pacientes
# ===========================================================================
def patients_tab() -> None:
    clin, can_edit = ctx.get_clin(), ctx.can("pacientes.registrar")
    st.markdown(CSS, unsafe_allow_html=True)
    if ctx.can("hc.exportar"):
        import_panel()
    if can_edit:
        with st.expander("Registrar paciente", icon=":material/person_add:",
                         expanded=st.session_state.pop("pt_register_open", False)):
            mode = st.segmented_control("Tipo de registro", ["Paciente nuevo", "Desde el extracto del HIS"],
                                        default="Paciente nuevo", key="pt_mode")
            if mode == "Desde el extracto del HIS":
                st.caption("Abre la historia clínica de un paciente que ya está en el extracto (llega anonimizado: "
                           "sin nombre ni documento; puedes completarlos después).")
                with st.form("pt_import"):
                    pid = st.number_input("Id del paciente en el HIS", min_value=1, step=1, value=None)
                    if st.form_submit_button("Abrir historia clínica", type="primary") and pid:
                        try:
                            cr.import_his_patient(clin, config.DB_PATH, int(pid), ctx.user_id(), ctx.clock())
                            st.session_state.clin_patient = int(pid)
                            st.toast("Historia clínica abierta", icon=":material/check_circle:")
                            st.rerun()
                        except sqlite3.IntegrityError as exc:
                            _err(exc)
            else:
                with st.form("pt_new", clear_on_submit=False):
                    data = _patient_form({})
                    if st.form_submit_button("Registrar paciente", type="primary"):
                        try:
                            new_id = cr.register_patient(clin, ctx.user_id(), data, ctx.clock())
                            st.session_state.clin_patient = new_id
                            st.toast(f"Paciente registrado (id {new_id})", icon=":material/check_circle:")
                            st.rerun()
                        except sqlite3.IntegrityError as exc:
                            st.session_state.pt_register_open = True
                            _err(exc)

    q = st.text_input("Buscar paciente", key="pt_q", placeholder="Nombre, apellido, número de documento o id")
    show_inactive = st.toggle("Incluir inactivos", key="pt_inactive")
    rows = cr.search_patients(clin, q, limit=100, include_inactive=show_inactive)
    if not rows:
        st.info("Sin resultados.")
        return
    df = pd.DataFrame([{"id": r["id_paciente"], "Paciente": cr.display_name(r),
                        "Documento": _doc(r),
                        "Edad": r["edad"], "Sexo": r["sexo"] or "—", "Asegurador": r["asegurador"] or "—",
                        "Registros": r["registros"], "Fórmulas activas": r["formulas_activas"],
                        "Origen": "HIS" if r["origen"] == "HIS" else "App", "Estado": r["estado"].capitalize()}
                       for r in rows])
    event = st.dataframe(df, hide_index=True, width="stretch", height=min(38 * (len(df) + 1), 330),
                         on_select="rerun", selection_mode="single-row", key="pt_table",
                         column_config={"id": st.column_config.NumberColumn("Id", format="%d", width="small"),
                                        "Edad": st.column_config.NumberColumn(format="%d")})
    sel = event.selection.rows if event and event.selection else []
    if sel:
        st.session_state.clin_patient = int(df.iloc[sel[0]]["id"])
    pid = st.session_state.get("clin_patient")
    row = cr.get_patient(clin, pid) if pid else None
    if row is None:
        st.caption("Selecciona un paciente en la tabla para ver o editar sus datos.")
        return
    decision = ctx.authorize_once("pacientes.registrar" if can_edit else "hc.ver_notas", pid)
    if not decision.allowed:
        banner(f"Acceso denegado: {esc(decision.reason)}.", "danger")
        return
    _patient_header(row)
    clinical = "hc.ver_completa" if ctx.can("hc.ver_completa") else "hc.ver_notas" if ctx.can("hc.ver_notas") else None
    if clinical and ctx.authorize_once(clinical, pid, ctx.emergency_for(pid)).allowed:
        summary(pid, ctx.can("hc.registrar") and ctx.authorize_once("hc.registrar", pid, ctx.emergency_for(pid)).allowed,
                key="pt")
        st.caption("Registros, adjuntos y línea de tiempo: pestaña Historia clínica (el paciente ya queda elegido).")
    if not can_edit:
        return
    with st.expander("Editar datos del paciente", icon=":material/edit:"):
        with st.form(f"pt_edit_{pid}"):
            data = _patient_form(dict(row), locked_doc=row["origen"] == "HIS" and not row["numero_documento"])
            if st.form_submit_button("Guardar cambios", type="primary"):
                try:
                    changed = cr.update_patient(clin, pid, ctx.user_id(), data, ctx.clock())
                    st.toast("Datos actualizados" if changed else "Sin cambios", icon=":material/check_circle:")
                    st.rerun()
                except sqlite3.IntegrityError as exc:
                    _err(exc)
        active = row["estado"] == "ACTIVO"
        if st.button("Marcar como inactivo" if active else "Reactivar paciente", key=f"pt_state_{pid}",
                     icon=":material/person_off:" if active else ":material/person_check:"):
            cr.set_patient_status(clin, pid, ctx.user_id(), not active, ctx.clock())
            st.rerun()
        st.caption("Los pacientes no se eliminan (la historia clínica se conserva); se marcan como inactivos.")


def _patient_form(row: dict, locked_doc: bool = False) -> dict:
    c1, c2 = st.columns(2)
    docs = list(cr.DOC_TYPES)
    current_doc = row.get("tipo_documento")
    tipo = c1.selectbox("Tipo de documento", docs, index=docs.index(current_doc) if current_doc in docs else 0,
                        format_func=lambda d: f"{d} · {cr.DOC_TYPES[d]}")
    numero = c2.text_input("Número de documento", value=row.get("numero_documento") or "")
    c3, c4 = st.columns(2)
    nombres = c3.text_input("Nombres", value="" if (row.get("nombres") or "").startswith("Paciente ")
                            else row.get("nombres") or "")
    apellidos = c4.text_input("Apellidos", value=row.get("apellidos") or "")
    c5, c6, c7 = st.columns(3)
    birth = row.get("fecha_nacimiento")
    fecha = c5.date_input("Fecha de nacimiento", value=date.fromisoformat(birth) if birth else None,
                          min_value=date(1900, 1, 1), max_value=date(2026, 12, 31), format="DD/MM/YYYY")
    sexos = list(cr.SEXES)
    sexo = c6.selectbox("Sexo", sexos, index=sexos.index(row["sexo"]) if row.get("sexo") in sexos else None,
                        placeholder="Selecciona")
    telefono = c7.text_input("Teléfono", value=row.get("telefono") or "")
    c8, c9, c10 = st.columns(3)
    asegurador = c8.text_input("EPS / asegurador", value=row.get("asegurador") or "")
    regimen = c9.selectbox("Régimen", ["Contributivo", "Subsidiado", "Especial", "Particular"],
                           index=["Contributivo", "Subsidiado", "Especial", "Particular"].index(row["regimen"])
                           if row.get("regimen") in ("Contributivo", "Subsidiado", "Especial", "Particular") else None,
                           placeholder="Selecciona")
    municipio = c10.text_input("Municipio", value=row.get("municipio") or "")
    c11, c12 = st.columns(2)
    direccion = c11.text_input("Dirección", value=row.get("direccion") or "")
    correo = c12.text_input("Correo del paciente", value=row.get("correo") or "",
                            help="Con este correo y su documento el paciente puede crear su cuenta en el portal.")
    data = {"tipo_documento": tipo, "numero_documento": numero, "nombres": nombres, "apellidos": apellidos,
            "fecha_nacimiento": fecha.isoformat() if fecha else None, "sexo": sexo, "telefono": telefono,
            "asegurador": asegurador, "regimen": regimen, "municipio": municipio, "direccion": direccion,
            "correo": correo}
    if locked_doc and not numero:
        data.pop("tipo_documento")
    if row.get("nombres", "").startswith("Paciente ") and not nombres:
        data.pop("nombres")
    return data


# ===========================================================================
# Historia clínica
# ===========================================================================
def history_tab() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    clin = ctx.get_clin()
    pid = pick_patient("hc")
    if pid is None:
        return
    full = ctx.can("hc.ver_completa")
    decision = gate("hc.ver_completa" if full else "hc.ver_notas", pid, "hc")
    if not decision.allowed:
        return
    row = cr.get_patient(clin, pid)
    if row is not None:
        _patient_header(row)
    can_write = ctx.can("hc.registrar") and ctx.authorize_once("hc.registrar", pid, ctx.emergency_for(pid)).allowed
    summary(pid, can_write)
    if can_write:
        _new_record_form(pid)
    t_rec, t_files, t_line, t_rx, t_ficha = st.tabs(["Registros", "Adjuntos", "Línea de tiempo", "Fórmulas",
                                                     "Cambios de la ficha"])
    with t_ficha:
        versions = cr.ficha_versions(clin, pid)
        if not versions:
            st.caption("La ficha no ha tenido cambios.")
        for v in versions:
            st.markdown(f"**Reemplazada el {_ts(v['reemplazada_en'])}** por {esc(v['reemplazada_por_nombre'] or '—')}"
                        f" · vigente desde {_ts(v['vigente_desde'])}")
            st.json(v["contenido"], expanded=False)
    with t_rec:
        show_annulled = st.toggle("Mostrar anulados", key=f"hc_annulled_{pid}")
        recs = cr.records(clin, pid, include_annulled=show_annulled)
        if not recs:
            st.caption("Sin registros todavía." + (" Usa “Nuevo registro” para crear el primero." if can_write else ""))
        files = [dict(a) for a in cr.attachments(clin, pid, include_annulled=False)]
        for r in recs:
            _record_card(r, [a for a in files if a["registro_id"] == r["id"]], pid, can_write)
    with t_files:
        _attachments_panel(pid, can_write)
    with t_line:
        _timeline(pid, full)
    with t_rx:
        _prescriptions(pid, full)


def _new_record_form(pid: int) -> None:
    with st.expander("Nuevo registro de historia clínica", icon=":material/note_add:"):
        with st.form(f"rec_new_{pid}", clear_on_submit=True):
            c1, c2 = st.columns([1, 2])
            tipo = c1.selectbox("Tipo", list(cr.RECORD_TYPES), format_func=cr.RECORD_TYPES.get)
            titulo = c2.text_input("Título", placeholder="Ej.: Control posquirúrgico día 3")
            contenido = st.text_area("Descripción (anamnesis, examen físico, hallazgos)", height=140)
            c3, c4 = st.columns([1, 3])
            dx_code = c3.text_input("CIE-10", placeholder="Ej.: J189")
            dx_name = c4.text_input("Diagnóstico", placeholder="Ej.: Neumonía, no especificada")
            plan = st.text_area("Plan y conducta", height=80)
            uploads = _uploader(st, "Adjuntos (PDF, PNG o JPG · máx. 5 MB c/u)")
            if st.form_submit_button("Guardar registro", type="primary", icon=":material/save:"):
                clin = ctx.get_clin()
                try:
                    rid = cr.create_record(clin, id_paciente=pid, autor_id=ctx.user_id(), tipo=tipo,
                                           titulo=titulo, contenido=contenido, diagnostico_cie10=dx_code,
                                           diagnostico_nombre=dx_name, plan=plan, now=ctx.clock())
                    bad = []
                    for f in uploads or []:
                        try:
                            cr.add_attachment(clin, id_paciente=pid, user_id=ctx.user_id(), filename=f.name,
                                              data=f.getvalue(), registro_id=rid, now=ctx.clock())
                        except sqlite3.IntegrityError as exc:
                            bad.append(f"{f.name}: {exc}")
                    for b in bad:
                        st.warning(f"No se adjuntó {b}")
                    st.toast("Registro guardado en la historia clínica", icon=":material/check_circle:")
                    if not bad:
                        st.rerun()
                except sqlite3.IntegrityError as exc:
                    msg = str(exc)
                    if "CHECK constraint" in msg:
                        msg = "El título necesita al menos 3 caracteres y la descripción al menos 10."
                    st.error(msg)


def _record_card(r, files: list[dict], pid: int, can_write: bool, prefix: str = "hc") -> None:
    annulled = r["estado"] == "ANULADO"
    color = GREY if annulled else EVENT_STYLE.get("REGISTRO_HC")
    meta = (f"{esc(cr.RECORD_TYPES.get(r['tipo'], r['tipo']))} · {_ts(r['creado_en'])} · {esc(r['autor'])}"
            + (f" · versión {r['version']} (corregido {_ts(r['actualizado_en'])})" if r["version"] > 1 else ""))
    body = [esc(r["contenido"])]
    if r["diagnostico_cie10"] or r["diagnostico_nombre"]:
        body.append(f"<b>Diagnóstico:</b> {esc(r['diagnostico_cie10'] or '')} {esc(r['diagnostico_nombre'] or '')}")
    if r["plan"]:
        body.append(f"<b>Plan:</b> {esc(r['plan'])}")
    badge = chip("Anulado", "neutral") if annulled else ""
    st.markdown(f'<div class="hc-rec{" hc-annulled" if annulled else ""}" style="--c:{color}">'
                f'<h4>{esc(r["titulo"])} {badge}</h4><div class="hc-meta">{meta}</div>'
                f'<div class="hc-body">{"<br><br>".join(body)}</div></div>', unsafe_allow_html=True)
    if annulled:
        st.caption(f"Motivo de anulación: {r['motivo_anulacion']}")
    if r["origen_externo"]:
        st.caption(r["origen_externo"])
    if files:
        cols = st.columns(min(len(files), 3))
        for i, a in enumerate(files):
            _download_button(cols[i % 3], a, pid, f"{prefix}rec{r['id']}")
    versions = cr.record_versions(ctx.get_clin(), r["id"]) if r["version"] > 1 else []
    mine = can_write and not annulled and r["autor_id"] == ctx.user_id()
    if not (versions or mine):
        return
    c1, c2, c3 = st.columns(3)
    if versions:
        with c1.popover(f"Versiones anteriores ({len(versions)})", icon=":material/history:", width="stretch"):
            for v in versions:
                st.markdown(f"**Versión {v['version']}** · vigente desde {_ts(v['vigente_desde'])} · reemplazada "
                            f"{_ts(v['reemplazada_en'])} por {v['reemplazada_por_nombre']}")
                st.caption(f"Motivo: {v['motivo_reemplazo']}")
                st.text(f"{v['titulo']}\n\n{v['contenido']}" + (f"\n\nPlan: {v['plan']}" if v["plan"] else ""))
                st.divider()
    if mine:
        with c2.popover("Corregir", icon=":material/edit:", width="stretch"):
            with st.form(f"rec_edit_{r['id']}"):
                titulo = st.text_input("Título", value=r["titulo"])
                contenido = st.text_area("Descripción", value=r["contenido"], height=140)
                dx_code = st.text_input("CIE-10", value=r["diagnostico_cie10"] or "")
                dx_name = st.text_input("Diagnóstico", value=r["diagnostico_nombre"] or "")
                plan = st.text_area("Plan", value=r["plan"] or "")
                motivo = st.text_input("Motivo de la corrección (queda registrado)")
                if st.form_submit_button("Guardar corrección", type="primary"):
                    try:
                        v = cr.update_record(ctx.get_clin(), r["id"], ctx.user_id(),
                                             {"titulo": titulo, "contenido": contenido, "diagnostico_cie10": dx_code,
                                              "diagnostico_nombre": dx_name, "plan": plan}, motivo, ctx.clock())
                        st.toast(f"Corrección guardada (versión {v})", icon=":material/check_circle:")
                        st.rerun()
                    except sqlite3.IntegrityError as exc:
                        _err(exc)
        with c3.popover("Anular", icon=":material/block:", width="stretch"):
            st.caption("El registro no se borra: queda tachado y visible con el motivo.")
            with st.form(f"rec_annul_{r['id']}"):
                motivo = st.text_input("Motivo de la anulación")
                if st.form_submit_button("Anular registro"):
                    try:
                        cr.annul_record(ctx.get_clin(), r["id"], ctx.user_id(), motivo, ctx.clock())
                        st.toast("Registro anulado", icon=":material/block:")
                        st.rerun()
                    except sqlite3.IntegrityError as exc:
                        _err(exc)


def _uploader(container, label: str):
    """Selector de archivos limitado a 5 MB (Streamlit >= 1.50 acepta el límite por widget)."""
    kwargs = {"type": ["pdf", "png", "jpg", "jpeg"], "accept_multiple_files": True}
    try:
        return container.file_uploader(label, max_upload_size=cr.MAX_FILE_BYTES // (1024 * 1024), **kwargs)
    except TypeError:
        return container.file_uploader(label, **kwargs)


def _download_button(container, a: dict, pid: int, key: str) -> None:
    """El archivo se lee de la base solo al hacer clic (no en cada recarga de la página)."""
    kb = a["tamano_bytes"] / 1024
    size = f"{kb / 1024:.1f} MB" if kb >= 1024 else f"{max(kb, 1):.0f} KB"
    icon = ":material/picture_as_pdf:" if a["tipo_mime"] == "application/pdf" else ":material/image:"
    uid, now = ctx.user_id(), ctx.clock()

    def fetch() -> bytes:
        conn = _own_conn()
        try:
            data = cr.attachment_content(conn, a["id"], pid)[2]
            ps.audit(conn, uid, "DESCARGA_ADJUNTO", a["nombre_archivo"], pid, now)
            return data
        finally:
            conn.close()

    container.download_button(f"{a['nombre_archivo']} ({size})", fetch, a["nombre_archivo"], a["tipo_mime"],
                              icon=icon, key=f"dl_{key}_{a['id']}", width="stretch", on_click="ignore")


def _attachments_panel(pid: int, can_write: bool) -> None:
    clin = ctx.get_clin()
    if can_write:
        with st.form(f"att_new_{pid}", clear_on_submit=True):
            c1, c2 = st.columns([2, 1.2], vertical_alignment="bottom")
            files = _uploader(c1, "Subir exámenes o documentos (PDF, PNG o JPG · máx. 5 MB)")
            desc = c2.text_input("Descripción", placeholder="Ej.: Hemograma 20/09")
            if st.form_submit_button("Subir", type="primary", icon=":material/upload:"):
                ok = 0
                for f in files or []:
                    try:
                        cr.add_attachment(clin, id_paciente=pid, user_id=ctx.user_id(), filename=f.name,
                                          data=f.getvalue(), descripcion=desc, now=ctx.clock())
                        ok += 1
                    except sqlite3.IntegrityError as exc:
                        st.warning(f"No se subió {f.name}: {exc}")
                if ok:
                    st.toast(f"{ok} archivo(s) subido(s)", icon=":material/upload:")
                    st.rerun()
    rows = cr.attachments(clin, pid)
    if not rows:
        st.caption("Sin adjuntos.")
    for a in rows:
        a = dict(a)
        c1, c2, c3 = st.columns([2.2, 2.4, 1], vertical_alignment="center")
        state = "" if a["estado"] == "ACTIVO" else f" · anulado: {a['motivo_anulacion']}"
        c1.markdown(f"**{esc(a['nombre_archivo'])}**<br><span style='color:{MUTED};font-size:0.8rem'>"
                    f"{esc(a['descripcion'] or '')} · {_ts(a['subido_en'])} · {esc(a['subido_por_nombre'])}"
                    f"{esc(state)}</span>", unsafe_allow_html=True)
        if a["estado"] == "ACTIVO":
            _download_button(c2, a, pid, "att")
            if can_write and a["subido_por"] == ctx.user_id():
                with c3.popover("Anular", icon=":material/block:"):
                    with st.form(f"att_annul_{a['id']}"):
                        motivo = st.text_input("Motivo")
                        if st.form_submit_button("Anular adjunto"):
                            try:
                                cr.annul_attachment(clin, a["id"], ctx.user_id(), motivo, ctx.clock())
                                st.rerun()
                            except sqlite3.IntegrityError as exc:
                                _err(exc)


def _timeline(pid: int, full: bool) -> None:
    events = ps.patient_timeline(ctx.get_clin(), pid)
    if not full:
        events = [e for e in events if e["tipo"] not in ("CITA", "INTERCONSULTA")]
    items = []
    for e in events:
        ingreso = f" · ingreso {e['oid_ingreso']}" if e["oid_ingreso"] else ""
        items.append(f'<div class="tl-item" style="--dot:{EVENT_STYLE.get(e["tipo"], GREY)}">'
                     f'<div class="tl-meta">{_ts(e["fecha"])} · {esc(e["tipo"].replace("_", " ").capitalize())}'
                     f' · {esc(e["autor"])}{ingreso}</div><div class="tl-text">{esc(e["descripcion"])}</div></div>')
    st.markdown(f'<div class="timeline">{"".join(items)}</div>' if items else "Sin eventos registrados.",
                unsafe_allow_html=True)
    st.caption("La línea de tiempo no se edita ni se borra: cada registro, corrección, anulación, adjunto, "
               "fórmula y entrega queda aquí con autor y hora.")


def _prescriptions(pid: int, full: bool) -> None:
    rx = ps.patient_prescriptions(ctx.get_clin(), pid)
    if not full:
        rx = [r for r in rx if r["estado"] in ("VIGENTE", "PARCIAL")]
    if not rx:
        st.caption("Sin fórmulas.")
        return
    df = pd.DataFrame([{"Medicamento": r["producto"].capitalize(), "Dosis": f"{r['dosis']} c/{r['frecuencia_horas']} h",
                        "Entregadas": f"{r['dosis_entregadas']}/{r['dosis_prescritas']}", "Ámbito": r["ambito"].capitalize(),
                        "Estado": RX_LABEL.get(r["estado"], r["estado"].capitalize()),
                        "Apartado hasta": ("En espera" if r["estado"] == "PENDIENTE_STOCK" else
                                           _ts(r["fecha_limite_reclamo"]) if r["ambito"] == "AMBULATORIA" else "—"),
                        "Médico": r["medico"]} for r in rx])
    st.dataframe(df, hide_index=True, width="stretch")


# ===========================================================================
# Búsqueda de historias clínicas
# ===========================================================================
def search_tab() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    clin = ctx.get_clin()
    clinical = ctx.can("hc.ver_completa") or ctx.can("hc.ver_notas")
    if not clinical:
        banner("Ves el <b>índice</b> de las historias (paciente, fecha, tipo y profesional), no su contenido.", "info")
    c1, c2, c3 = st.columns([2.2, 1.2, 1.2])
    text = c1.text_input("Buscar", placeholder="Diagnóstico, CIE-10, síntoma, paciente o documento", key="hcs_text")
    tipo = c2.selectbox("Tipo", [None, *cr.RECORD_TYPES], format_func=lambda t: "Todos" if t is None
                        else cr.RECORD_TYPES[t], key="hcs_tipo")
    people = {r["id"]: r["nombre_mostrado"] for r in cr.authors(clin)}
    autor = c3.selectbox("Profesional", [None, *people], format_func=lambda a: "Todos" if a is None else people[a],
                         key="hcs_autor")
    c4, c5, c6 = st.columns([1, 1, 1.2], vertical_alignment="bottom")
    desde = c4.date_input("Desde", value=None, format="DD/MM/YYYY", key="hcs_desde")
    hasta = c5.date_input("Hasta", value=None, format="DD/MM/YYYY", key="hcs_hasta")
    annulled = c6.toggle("Incluir anulados", key="hcs_annulled")

    rows = cr.search_records(clin, text=text if clinical else "", tipo=tipo, autor_id=autor,
                             desde=desde.isoformat() if desde else None, hasta=hasta.isoformat() if hasta else None,
                             include_annulled=annulled)
    if not clinical and text:  # sin permiso clínico el texto solo filtra por paciente/documento
        words = text.lower().split()
        rows = [r for r in rows if all(w in f"{r['paciente']} {r['numero_documento'] or ''} {r['id_paciente']}".lower()
                                       for w in words)]
    key = (text, tipo, autor, str(desde), str(hasta), annulled)
    if st.session_state.get("hcs_last") != key:  # audita cada búsqueda nueva (no cada re-ejecución)
        st.session_state.hcs_last = key
        ps.audit(clin, ctx.user_id(), "BUSQUEDA_HC", f"texto={text!r} tipo={tipo} autor={autor} desde={desde} "
                 f"hasta={hasta} resultados={len(rows)}", None, ctx.clock())
    patients = len({r["id_paciente"] for r in rows})
    st.markdown(chip(f"{len(rows)} registro(s) de {patients} paciente(s)", "neutral"), unsafe_allow_html=True)
    if not rows:
        st.caption("Sin resultados con esos filtros.")
        return
    base = [{"Fecha": _ts(r["creado_en"]), "Paciente": r["paciente"], "Documento": _doc(r),
             "Tipo": cr.RECORD_TYPES.get(r["tipo"], r["tipo"]), "Profesional": r["autor"],
             "Estado": r["estado"].capitalize(), "Adjuntos": r["adjuntos"]} for r in rows]
    if clinical:
        for b, r in zip(base, rows):
            b["Título"] = r["titulo"]
            b["Diagnóstico"] = f"{r['diagnostico_cie10'] or ''} {r['diagnostico_nombre'] or ''}".strip() or "—"
    df = pd.DataFrame(base)
    order = ["Fecha", "Paciente", "Documento", "Tipo", *(["Título", "Diagnóstico"] if clinical else []),
             "Profesional", "Adjuntos", "Estado"]
    event = st.dataframe(df[order], hide_index=True, width="stretch", height=min(38 * (len(df) + 1), 420),
                         on_select="rerun", selection_mode="multi-row", key="hcs_table")
    sel = event.selection.rows if event and event.selection else []
    chosen = [rows[i] for i in sel]
    ids = list(dict.fromkeys(r["id_paciente"] for r in chosen)) or list(dict.fromkeys(r["id_paciente"] for r in rows))
    scope = f"{len(ids)} paciente(s) seleccionado(s)" if chosen else f"los {len(ids)} paciente(s) del resultado"
    if ctx.can("hc.exportar"):
        a1, a2, a3 = st.columns([1.3, 1.3, 2], vertical_alignment="center")
        pdf_button(a1, ids, "historias_clinicas.pdf", "Descargar PDF", key=f"hcs_pdf_{hash(tuple(ids))}")
        json_button(a2, ids, "historias_clinicas.json", key=f"hcs_json_{hash(tuple(ids))}")
        a3.caption(f"Descarga {scope}. Marca varias filas para elegir cuáles.")
    if not clinical or not chosen:
        if clinical:
            st.caption("Marca una o varias filas para leer los registros completos.")
        return
    for r in chosen[:5]:
        decision = gate("hc.ver_completa" if ctx.can("hc.ver_completa") else "hc.ver_notas", r["id_paciente"],
                        f"hcs_{r['id']}")
        if not decision.allowed:
            continue
        st.markdown(f"**{esc(r['paciente'])}**", unsafe_allow_html=True)
        allergy_banner(r["id_paciente"])
        files = [dict(a) for a in cr.attachments(clin, r["id_paciente"], include_annulled=False)
                 if a["registro_id"] == r["id"]]
        _record_card(r, files, r["id_paciente"], False, prefix="hcs")
    if len(chosen) > 5:
        st.caption(f"Se muestran 5 de {len(chosen)}; el PDF incluye todos.")
    if st.button("Abrir la ficha completa del primer paciente", icon=":material/folder_open:"):
        st.session_state.clin_patient = chosen[0]["id_paciente"]
        st.toast("Paciente seleccionado: ve a la pestaña Historia clínica", icon=":material/folder_open:")


# ===========================================================================
# Ficha del paciente (todo en un vistazo), descargas y exportación
# ===========================================================================
SUMMARY_CSS = f"""
<style>
  .fx-allergy {{border-radius:10px; padding:0.55rem 0.8rem; margin:0.2rem 0 0.6rem; font-size:0.92rem;}}
  .fx-allergy.bad {{background:#FEE2E2; color:#991B1B; border:1px solid #FCA5A5;}}
  .fx-allergy.ok {{background:#ECFDF5; color:#065F46; border:1px solid #A7F3D0;}}
  .fx-allergy.unk {{background:#FEF3C7; color:#92400E; border:1px solid #FCD34D;}}
  .fx-grid {{display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:0.55rem; margin-bottom:0.6rem;}}
  .fx-box {{background:#FFF; border:1px solid {BORDER}; border-radius:10px; padding:0.55rem 0.7rem;}}
  .fx-box h5 {{margin:0 0 0.2rem; font-size:0.75rem; color:{MUTED}; font-weight:650; text-transform:uppercase;
      letter-spacing:.03em;}}
  .fx-box div {{font-size:0.88rem; color:{TEXT}; white-space:pre-wrap;}}
</style>
"""


def allergy_banner(pid: int) -> None:
    f = cr.get_ficha(ctx.get_clin(), pid)
    if f is not None and f["alergias"]:
        html = f'<div class="fx-allergy bad"><b>ALERGIAS:</b> {esc(f["alergias"])}</div>'
    elif f is not None and f["sin_alergias_conocidas"]:
        html = '<div class="fx-allergy ok"><b>Sin alergias conocidas</b></div>'
    else:
        html = '<div class="fx-allergy unk"><b>Alergias sin registrar:</b> pregúntale al paciente antes de formular</div>'
    st.markdown(SUMMARY_CSS + html, unsafe_allow_html=True)


def summary(pid: int, can_write: bool, key: str = "hc") -> None:
    clin = ctx.get_clin()
    allergy_banner(pid)
    f = cr.get_ficha(clin, pid)
    bed = cr.current_bed(clin, pid)
    rx = [r for r in ps.patient_prescriptions(clin, pid) if r["estado"] in ("VIGENTE", "PARCIAL")]
    appts = [c for c in ps.patient_appointments(clin, pid) if c["estado"] == "PROGRAMADA"]
    last = cr.records(clin, pid, include_annulled=False)[:1]

    def box(title, text):
        return f'<div class="fx-box"><h5>{esc(title)}</h5><div>{esc(text or "—")}</div></div>'

    boxes = [
        box("Grupo sanguíneo", f["grupo_sanguineo"] if f else None),
        box("Antecedentes", "\n".join(x for x in ((f["antecedentes_personales"] if f else None),
                                                  (f["antecedentes_quirurgicos"] if f else None)) if x)),
        box("Medicación habitual", f["medicacion_habitual"] if f else None),
        box("Cama actual", f"{bed['codigo_cama']} · estancia estimada {bed['dias_estimados']} días" if bed else
            "Sin cama asignada en la app"),
        box("Fórmulas activas", "\n".join(f"{str(r['producto']).capitalize()[:40]} ({r['dosis_entregadas']}/"
                                          f"{r['dosis_prescritas']})" for r in rx[:3]) or "Ninguna"),
        box("Próxima cita", f"{_ts(appts[-1]['fecha_hora'])} · {appts[-1]['especialidad'].title()}" if appts else "Ninguna"),
        box("Último registro", f"{_ts(last[0]['creado_en'])} · {last[0]['titulo']}" if last else "Sin registros"),
        box("Contacto de emergencia", " · ".join(x for x in ((f["contacto_emergencia"] if f else None),
                                                           (f["telefono_emergencia"] if f else None)) if x)),
    ]
    st.markdown(f'<div class="fx-grid">{"".join(boxes)}</div>', unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    if ctx.can("hc.exportar") or ctx.can("portal.propio"):
        pdf_button(c1, [pid], f"historia_clinica_{pid}.pdf", "Descargar historia (PDF)", key=f"pdf_{key}_{pid}")
    if ctx.can("hc.exportar"):
        json_button(c2, [pid], f"historia_clinica_{pid}.json", key=f"json_{key}_{pid}")
    if can_write:
        with c3.popover("Editar ficha clínica", icon=":material/edit_note:", width="stretch"):
            _ficha_form(pid, f, key)


def _ficha_form(pid: int, f, key: str = "hc") -> None:
    f = dict(f) if f else {}
    with st.form(f"ficha_{key}_{pid}"):
        none_known = st.checkbox("Sin alergias conocidas", value=bool(f.get("sin_alergias_conocidas")))
        alergias = st.text_input("Alergias (separa con ;)", value=f.get("alergias") or "",
                                 placeholder="Ej.: Penicilina (urticaria); Látex", disabled=False)
        groups = list(cr.BLOOD_GROUPS)
        grupo = st.selectbox("Grupo sanguíneo", groups, index=groups.index(f["grupo_sanguineo"])
                             if f.get("grupo_sanguineo") in groups else None, placeholder="Sin dato")
        fields = {}
        for key, label in (("antecedentes_personales", "Antecedentes personales"),
                           ("antecedentes_quirurgicos", "Antecedentes quirúrgicos"),
                           ("antecedentes_familiares", "Antecedentes familiares"),
                           ("medicacion_habitual", "Medicación habitual"), ("habitos", "Hábitos"),
                           ("observaciones", "Observaciones")):
            fields[key] = st.text_area(label, value=f.get(key) or "", height=68)
        c1, c2 = st.columns(2)
        fields["contacto_emergencia"] = c1.text_input("Contacto de emergencia", value=f.get("contacto_emergencia") or "")
        fields["telefono_emergencia"] = c2.text_input("Teléfono", value=f.get("telefono_emergencia") or "")
        if st.form_submit_button("Guardar ficha", type="primary"):
            if none_known and alergias.strip():
                st.error("Marca “sin alergias conocidas” o escribe las alergias, no ambas.")
                return
            try:
                changed = cr.save_ficha(ctx.get_clin(), pid, ctx.user_id(),
                                        {**fields, "alergias": alergias, "sin_alergias_conocidas": none_known,
                                         "grupo_sanguineo": grupo}, ctx.clock())
                st.toast("Ficha actualizada" if changed else "Sin cambios", icon=":material/check_circle:")
                st.rerun()
            except sqlite3.IntegrityError as exc:
                _err(exc)
    st.caption("Cada cambio guarda la versión anterior (pestaña “Cambios de la ficha”).")


def _own_conn():
    """Conexión propia para trabajo en segundo plano (descargas diferidas corren en otro hilo)."""
    return ps.connect(ps.CLINICAL_DB)


def pdf_button(container, ids: list[int], filename: str, label: str, key: str) -> None:
    user, uid, now = ctx.current_user()["nombre_mostrado"], ctx.user_id(), ctx.clock()

    def build() -> bytes:
        conn = _own_conn()
        try:
            data = hd.patients_pdf(conn, ids, user, now)
            for pid in ids:
                ps.audit(conn, uid, "DESCARGA_HC_PDF", f"{len(ids)} historia(s)", pid, now)
            return data
        finally:
            conn.close()

    container.download_button(label, build, filename, "application/pdf", icon=":material/picture_as_pdf:",
                              key=key, width="stretch", on_click="ignore")


def json_button(container, ids: list[int], filename: str, key: str, label: str = "Exportar (JSON)") -> None:
    user, uid, now = ctx.current_user()["nombre_mostrado"], ctx.user_id(), ctx.clock()

    def build() -> bytes:
        conn = _own_conn()
        try:
            data = hd.export_json(conn, ids, user, now)
            for pid in ids:
                ps.audit(conn, uid, "EXPORTA_HC", f"{len(ids)} historia(s)", pid, now)
            return data
        finally:
            conn.close()

    container.download_button(label, build, filename, "application/json", icon=":material/file_export:",
                              key=key, width="stretch", on_click="ignore")


def import_panel() -> None:
    with st.expander("Importar historias clínicas (JSON de otra sede o de esta app)", icon=":material/file_upload:"):
        st.caption("Formato HSLV-HC. Los pacientes se reconocen por documento; los registros conservan autor y fecha "
                   "originales; los adjuntos se verifican con su huella SHA-256 y no se duplican.")
        with st.form("hc_import", clear_on_submit=True):
            kwargs = {"type": ["json"]}
            try:
                up = st.file_uploader("Archivo .json", max_upload_size=20, **kwargs)
            except TypeError:
                up = st.file_uploader("Archivo .json", **kwargs)
            if st.form_submit_button("Importar", type="primary", icon=":material/file_upload:") and up:
                try:
                    res = hd.import_json(ctx.get_clin(), up.getvalue(), ctx.user_id(), ctx.clock())
                    ps.audit(ctx.get_clin(), ctx.user_id(), "IMPORTA_HC",
                             f"{up.name}: {res['pacientes_nuevos']} nuevos, {res['registros']} registros", None, ctx.clock())
                    st.success(f"Pacientes nuevos: {res['pacientes_nuevos']} · ya existentes: {res['pacientes_existentes']}"
                               f" · registros importados: {res['registros']} (omitidos por duplicado: {res['omitidos']})"
                               f" · adjuntos: {res['adjuntos']}")
                    for e in res["errores"]:
                        st.warning(e)
                except sqlite3.IntegrityError as exc:
                    _err(exc)
