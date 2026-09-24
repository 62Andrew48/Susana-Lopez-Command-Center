"""
demo_cases.py — Casos clínicos SINTÉTICOS para la demostración (no son pacientes reales).

El extracto del HIS llega anonimizado (sin nombres ni notas clínicas). Para mostrar la historia clínica, la
ficha con alergias, la búsqueda y la descarga en PDF, aquí se crean cinco pacientes ficticios con casos
típicos de un hospital de segundo nivel en Popayán, con sus síntomas, hallazgos, diagnósticos CIE-10,
planes y un resultado de laboratorio en PDF. También se crean más usuarios del equipo.
Todo se declara como sintético en la interfaz y en el PDF.
"""
from __future__ import annotations

import hashlib
import io
import sqlite3
from datetime import datetime, timedelta

import clinical_records as cr
import pharmacy_service as ps
import scheduling as sch

FMT = "%Y-%m-%d %H:%M:%S"
DR_RUIZ, DR_PAREDES, ENF_GOMEZ = 2, 5, 3


def _hash(password: str) -> str:
    return "sha256$" + hashlib.sha256(password.encode()).hexdigest()


def _lab_pdf(title: str, patient: str, date: str, rows: list[tuple[str, str, str]]) -> bytes:
    """Resultado de laboratorio de ejemplo en PDF (sintético)."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    w, h = letter
    c.setFont("Helvetica-Bold", 13)
    c.drawString(60, h - 70, "Laboratorio clínico · Hospital Susana López de Valencia")
    c.setFont("Helvetica", 10)
    c.drawString(60, h - 90, f"{title} · {patient} · toma de muestra {date}")
    c.drawString(60, h - 104, "DOCUMENTO SINTÉTICO DE DEMOSTRACIÓN")
    y = h - 140
    c.setFont("Helvetica-Bold", 10)
    for x, t in ((60, "Prueba"), (300, "Resultado"), (420, "Referencia")):
        c.drawString(x, y, t)
    c.setFont("Helvetica", 10)
    for name, value, ref in rows:
        y -= 18
        c.drawString(60, y, name)
        c.drawString(300, y, value)
        c.drawString(420, y, ref)
    c.showPage()
    c.save()
    return buf.getvalue()


PATIENTS = [
    {"key": "laura", "data": {"tipo_documento": "CC", "numero_documento": "1061700001", "nombres": "Laura Sofía",
                              "apellidos": "Gómez Pérez", "fecha_nacimiento": "1992-03-14", "sexo": "Femenino",
                              "telefono": "3104567890", "asegurador": "Asmet Salud EPS", "regimen": "Subsidiado",
                              "municipio": "Popayán", "direccion": "Cra 9 # 12-40, barrio Bolívar",
                              "correo": "paciente.laura@hslv.demo"},
     "ficha": {"grupo_sanguineo": "O+", "alergias": "Penicilina (urticaria generalizada)",
               "antecedentes_personales": "Asma leve intermitente desde la infancia.",
               "antecedentes_quirurgicos": "Apendicectomía (2014).",
               "antecedentes_familiares": "Madre con diabetes tipo 2.",
               "medicacion_habitual": "Salbutamol inhalado a necesidad.", "habitos": "No fuma. Actividad física 2 veces/semana.",
               "contacto_emergencia": "Jorge Gómez (padre)", "telefono_emergencia": "3157778899"},
     "records": [
         ("2026-09-18 22:40:00", DR_RUIZ, "CONSULTA", "Urgencias: fiebre, tos y dificultad para respirar",
          "Paciente de 34 años con 4 días de fiebre hasta 39,2 °C, tos con expectoración amarillenta, dolor "
          "pleurítico derecho y disnea de medianos esfuerzos. Al examen: FC 108, FR 24, T 38,9 °C, SatO2 91 % al "
          "aire ambiente. Crépitos y soplo tubárico en base pulmonar derecha. Consciente, orientada, CURB-65: 1.",
          "J189", "Neumonía, no especificada",
          "Hospitalizar. Oxígeno por cánula nasal para SatO2 > 94 %. Radiografía de tórax, hemograma, PCR, "
          "hemocultivos. Alérgica a penicilina: esquema antibiótico sin betalactámicos según protocolo "
          "institucional."),
         ("2026-09-19 07:30:00", DR_RUIZ, "RESULTADO_EXAMEN", "Radiografía de tórax y laboratorios de ingreso",
          "Rx de tórax: consolidación en lóbulo inferior derecho con broncograma aéreo, sin derrame. Hemograma: "
          "leucocitos 15.800 con 84 % neutrófilos. PCR 96 mg/L. Creatinina 0,8 mg/dL.",
          "J181", "Neumonía lobar, no especificada", "Continuar esquema antibiótico. Control de PCR en 72 h."),
         ("2026-09-20 09:15:00", DR_RUIZ, "EVOLUCION", "Evolución día 2: mejoría clínica",
          "Afebril desde hace 18 horas. Tos menos frecuente. SatO2 95 % con oxígeno a 1 L/min. Tolera la vía oral. "
          "Murmullo vesicular disminuido en base derecha, sin signos de dificultad respiratoria.",
          "J181", "Neumonía lobar, no especificada",
          "Destete de oxígeno. Si se mantiene estable 24 h, paso a vía oral y plan de egreso."),
     ],
     "lab": ("Hemograma y PCR", "2026-09-19", [("Leucocitos", "15.800 /µL", "4.500 - 11.000"),
                                                ("Neutrófilos", "84 %", "40 - 70 %"),
                                                ("Hemoglobina", "12,9 g/dL", "12 - 16"),
                                                ("Plaquetas", "310.000 /µL", "150.000 - 450.000"),
                                                ("Proteína C reactiva", "96 mg/L", "< 5")]),
     "portal_user": "paciente.laura"},
    {"key": "carlos", "data": {"tipo_documento": "CC", "numero_documento": "10542001", "nombres": "Carlos Andrés",
                               "apellidos": "Muñoz Ruiz", "fecha_nacimiento": "1968-07-02", "sexo": "Masculino",
                               "telefono": "3206543210", "asegurador": "Nueva EPS", "regimen": "Contributivo",
                               "municipio": "Popayán", "correo": "carlos.munoz@correo.demo"},
     "ficha": {"grupo_sanguineo": "A+", "sin_alergias_conocidas": True,
               "antecedentes_personales": "Diabetes mellitus tipo 2 (2016). Hipertensión arterial (2018).",
               "antecedentes_familiares": "Padre fallecido por infarto a los 62 años.",
               "medicacion_habitual": "Metformina 850 mg c/12 h. Losartán 50 mg c/12 h.",
               "habitos": "Exfumador (20 paquetes/año, suspendió en 2019). Sedentario.",
               "contacto_emergencia": "Martha Ruiz (esposa)", "telefono_emergencia": "3001112233"},
     "records": [
         ("2026-09-10 08:20:00", DR_RUIZ, "CONSULTA", "Control de diabetes e hipertensión",
          "Asiste a control. Refiere poliuria y nicturia de 3 semanas, sin pérdida de peso. Adherencia irregular a "
          "la metformina. TA 146/92, FC 78, IMC 31,4. Pulsos pedios presentes, monofilamento normal.",
          "E119", "Diabetes mellitus tipo 2 sin complicaciones",
          "Glicemia, HbA1c, perfil lipídico, creatinina y microalbuminuria. Reforzar dieta y adherencia. "
          "Control en 2 semanas con resultados."),
         ("2026-09-19 10:00:00", DR_RUIZ, "RESULTADO_EXAMEN", "Resultados de control metabólico",
          "Glicemia en ayunas 182 mg/dL. HbA1c 8,1 %. LDL 138 mg/dL. Creatinina 1,0 mg/dL (TFG 82). "
          "Microalbuminuria 45 mg/g.",
          "E112", "Diabetes mellitus tipo 2 con complicaciones renales",
          "Agregar empagliflozina 10 mg/día. Atorvastatina 40 mg noche. Educación en autocontrol de glicemia."),
     ],
     "lab": ("Perfil metabólico", "2026-09-18", [("Glicemia en ayunas", "182 mg/dL", "70 - 100"),
                                                  ("Hemoglobina glicosilada", "8,1 %", "< 7 %"),
                                                  ("Colesterol LDL", "138 mg/dL", "< 100"),
                                                  ("Creatinina", "1,0 mg/dL", "0,7 - 1,3"),
                                                  ("Microalbuminuria", "45 mg/g", "< 30")])},
    {"key": "maria", "data": {"tipo_documento": "CC", "numero_documento": "1061800222", "nombres": "María Fernanda",
                              "apellidos": "Ortiz Castillo", "fecha_nacimiento": "1999-11-25", "sexo": "Femenino",
                              "telefono": "3125550011", "asegurador": "Emssanar EPS", "regimen": "Subsidiado",
                              "municipio": "Timbío", "correo": "maria.ortiz@correo.demo"},
     "ficha": {"grupo_sanguineo": "B+", "alergias": "Ibuprofeno (broncoespasmo)",
               "antecedentes_personales": "G2P1. Embarazo actual de 32 semanas.",
               "antecedentes_familiares": "Madre con preeclampsia.",
               "medicacion_habitual": "Sulfato ferroso, ácido fólico, calcio.",
               "contacto_emergencia": "Andrés Paz (pareja)", "telefono_emergencia": "3136660022"},
     "records": [
         ("2026-09-20 16:05:00", DR_RUIZ, "CONSULTA", "Cefalea y cifras tensionales altas en embarazo de 32 semanas",
          "Gestante de 26 años, 32 semanas por ecografía temprana. Cefalea frontal de 6 horas, fosfenos "
          "ocasionales, sin epigastralgia. TA 152/98 y 148/96 (con 15 min de diferencia). Edema de miembros "
          "inferiores grado II. Movimientos fetales presentes, FCF 142 lpm. Proteinuria en tira 2+.",
          "O141", "Preeclampsia severa",
          "Hospitalizar en alto riesgo obstétrico. Sulfato de magnesio según protocolo, labetalol si TA ≥ 160/110. "
          "Maduración pulmonar con betametasona. NO AINES (alergia a ibuprofeno). Perfil de preeclampsia."),
     ]},
    {"key": "juan", "data": {"tipo_documento": "TI", "numero_documento": "1061900333", "nombres": "Juan José",
                             "apellidos": "Pino Velasco", "fecha_nacimiento": "2018-05-09", "sexo": "Masculino",
                             "telefono": "3148889900", "asegurador": "Asmet Salud EPS", "regimen": "Subsidiado",
                             "municipio": "Popayán"},
     "ficha": {"grupo_sanguineo": "O+", "sin_alergias_conocidas": True,
               "antecedentes_personales": "Esquema de vacunación completo para la edad. Peso 25 kg.",
               "contacto_emergencia": "Diana Velasco (madre)", "telefono_emergencia": "3148889900"},
     "records": [
         ("2026-09-21 06:10:00", DR_PAREDES, "CONSULTA", "Pediatría: diarrea y vómito de 2 días",
          "Niño de 8 años con 6 deposiciones líquidas al día sin sangre y 4 vómitos en 24 h. Fiebre de 38 °C. "
          "Mucosas secas, llenado capilar 2 s, sin letargia. Peso 24 kg (pérdida estimada 4 %).",
          "A09X", "Diarrea y gastroenteritis de presunto origen infeccioso",
          "Deshidratación leve: sales de rehidratación oral 50 ml/kg en 4 h. Zinc 20 mg/día por 10 días. "
          "Signos de alarma explicados a la madre."),
     ]},
    {"key": "rosa", "data": {"tipo_documento": "CC", "numero_documento": "25270444", "nombres": "Rosa Elena",
                             "apellidos": "Quintero de Ruiz", "fecha_nacimiento": "1950-02-17", "sexo": "Femenino",
                             "telefono": "3002223344", "asegurador": "Nueva EPS", "regimen": "Contributivo",
                             "municipio": "Popayán", "correo": "rosa.quintero@correo.demo"},
     "ficha": {"grupo_sanguineo": "A-", "alergias": "Sulfas (exantema); Látex",
               "antecedentes_personales": "Insuficiencia cardiaca con FEVI 35 %. Fibrilación auricular permanente. "
                                          "Hipotiroidismo.",
               "antecedentes_quirurgicos": "Reemplazo de cadera derecha (2021).",
               "medicacion_habitual": "Warfarina 5 mg/día, furosemida 40 mg/día, bisoprolol 2,5 mg/día, "
                                      "levotiroxina 50 µg/día.",
               "contacto_emergencia": "Hernán Ruiz (hijo)", "telefono_emergencia": "3187771122"},
     "records": [
         ("2026-09-12 11:30:00", DR_RUIZ, "CONSULTA", "Disnea progresiva y edema",
          "Paciente de 76 años con disnea de pequeños esfuerzos, ortopnea de 3 almohadas y aumento de 3 kg en una "
          "semana. Ingurgitación yugular, crépitos bibasales, edema grado III. Ritmo irregular a 98 lpm. "
          "INR 3,4.", "I500", "Insuficiencia cardiaca congestiva",
          "Hospitalizar. Furosemida IV, restricción hídrica, control de peso y diuresis diaria. Suspender "
          "warfarina 1 dosis por INR alto."),
         ("2026-09-20 12:00:00", DR_RUIZ, "EPICRISIS", "Epicrisis: insuficiencia cardiaca descompensada",
          "Estancia de 8 días. Respuesta adecuada al diurético con balance negativo de 4,2 L. Sin disnea en reposo "
          "al egreso. INR 2,5 al alta. Ecocardiograma: FEVI 35 %, sin cambios respecto al previo.",
          "I500", "Insuficiencia cardiaca congestiva",
          "Egreso con furosemida 40 mg/día, bisoprolol 2,5 mg/día, warfarina 5 mg/día. Control de INR en 5 días "
          "y cita con cardiología en 2 semanas."),
     ]},
]

EXTRA_STAFF = [  # id, usuario, nombre, rol_id, registro, especialidad, correo
    (DR_PAREDES, "dr.paredes", "Dr. Paredes", 2, "RM-19-5520", "PEDIATRIA", "dr.paredes@hslv.demo"),
    (6, "enf.castro", "Enf. Castro", 3, "RE-19-3310", None, "enf.castro@hslv.demo"),
    (7, "facturacion.alejandro", "Facturación · Alejandro", 5, None, None, "facturacion.alejandro@hslv.demo"),
]
# Plantilla de enfermería y médicos adicional (sintética) para que la cobertura por servicio tenga sentido
EXTRA_TEAM = [  # id, usuario, nombre, rol_id, registro, especialidad, servicio, turno (hora inicio, horas)
    (8, "enf.mora", "Enf. Mora", 3, "RE-18-4410", None, "Hospitalización", (7, 12)),
    (9, "enf.diaz", "Enf. Díaz", 3, "RE-20-5521", None, "Hospitalización", (7, 12)),
    (10, "enf.rojas", "Enf. Rojas", 3, "RE-17-3302", None, "UCI", (7, 12)),
    (11, "enf.vargas", "Enf. Vargas", 3, "RE-21-6630", None, "Pediatría", (7, 12)),
    (12, "enf.silva", "Enf. Silva", 3, "RE-19-7741", None, "Pediatría", (7, 12)),
    (13, "enf.ortega", "Enf. Ortega", 3, "RE-16-2250", None, "Urgencias", (7, 12)),
    (14, "enf.munoz", "Enf. Muñoz", 3, "RE-22-8860", None, "Urgencias", (7, 12)),
    (15, "dr.cruz", "Dr. Cruz", 2, "RM-15-2231", "MEDICINA INTERNA", "Hospitalización", (7, 12)),
    (16, "dra.valencia", "Dra. Valencia", 2, "RM-14-1180", "MEDICINA CRITICA", "UCI", (7, 12)),
    (17, "quirofanos.bravo", "Quirófanos · Coord. Bravo", 6, None, None, "Cirugía", (7, 12)),
]
DEMO_MAILS = {1: "admin@hslv.demo", 2: "dra.ruiz@hslv.demo", 3: "enf.gomez@hslv.demo", 4: "paciente.110@hslv.demo"}


def seed_cases(conn: sqlite3.Connection) -> None:
    """Idempotente: si ya existe el primer paciente sintético, no hace nada."""
    if conn.execute("SELECT 1 FROM pacientes_clinicos WHERE numero_documento = '1061700001'").fetchone():
        return
    with conn:
        for uid, user, name, rol, reg, esp, mail in EXTRA_STAFF:
            conn.execute("INSERT OR IGNORE INTO usuarios(id, usuario, hash_password, nombre_mostrado, rol_id, "
                         "estado_cuenta, registro_profesional, especialidad, correo, creado_en) "
                         "VALUES (?,?,?,?,?, 'ACTIVO', ?,?,?, '2026-09-01 08:00:00')",
                         (uid, user, _hash("demo"), name, rol, reg, esp, mail))
        day = datetime(2026, 9, 14)
        while day <= datetime(2026, 10, 31):
            conn.execute("INSERT INTO turnos(usuario_id, servicio, tipo, inicio, fin) VALUES (?, 'Pediatría', 'TURNO', ?, ?)",
                         (DR_PAREDES, day.replace(hour=7).strftime(FMT), day.replace(hour=19).strftime(FMT)))
            conn.execute("INSERT INTO turnos(usuario_id, servicio, tipo, inicio, fin) VALUES (6, 'Hospitalización', "
                         "'TURNO', ?, ?)", (day.replace(hour=19).strftime(FMT),
                                            (day + timedelta(days=1)).replace(hour=7).strftime(FMT)))
            if day.weekday() < 6:  # admisiones: lunes a sábado
                conn.execute("INSERT INTO turnos(usuario_id, servicio, tipo, inicio, fin) VALUES (7, 'Admisiones', "
                             "'TURNO', ?, ?)", (day.replace(hour=7).strftime(FMT), day.replace(hour=19).strftime(FMT)))
            day += timedelta(days=1)
        for uid, user, name, rol, reg, esp, service, (h0, hours) in EXTRA_TEAM:
            conn.execute("INSERT OR IGNORE INTO usuarios(id, usuario, hash_password, nombre_mostrado, rol_id, "
                         "estado_cuenta, registro_profesional, especialidad, creado_en) "
                         "VALUES (?,?,?,?,?, 'ACTIVO', ?,?, '2026-09-01 08:00:00')",
                         (uid, user, _hash("demo"), name, rol, reg, esp))
            d = datetime(2026, 9, 14)
            while d <= datetime(2026, 10, 31):
                conn.execute("INSERT INTO turnos(usuario_id, servicio, tipo, inicio, fin) VALUES (?,?, 'TURNO', ?, ?)",
                             (uid, service, d.replace(hour=h0).strftime(FMT),
                              (d.replace(hour=h0) + timedelta(hours=hours)).strftime(FMT)))
                d += timedelta(days=1)
        for uid, mail in DEMO_MAILS.items():
            conn.execute("UPDATE usuarios SET correo = ? WHERE id = ? AND correo IS NULL", (mail, uid))
    next_uid = 20  # usuarios de pacientes desde el 20
    for case in PATIENTS:
        first = case["records"][0][0]
        pid = cr.register_patient(conn, 1, case["data"], first)
        cr.save_ficha(conn, pid, DR_RUIZ, case["ficha"], first)
        ids = []
        for when, author, tipo, title, text, dx, dx_name, plan in case["records"]:
            ids.append(cr.create_record(conn, id_paciente=pid, autor_id=author, tipo=tipo, titulo=title, contenido=text,
                                        diagnostico_cie10=dx, diagnostico_nombre=dx_name, plan=plan, now=when))
        if case.get("lab"):
            title, date, rows = case["lab"]
            name = f"{case['data']['nombres']} {case['data']['apellidos']}"
            target = next((i for i, r in zip(ids, case["records"]) if r[2] == "RESULTADO_EXAMEN"), ids[0])
            cr.add_attachment(conn, id_paciente=pid, user_id=DR_RUIZ, registro_id=target,
                              filename=f"{title.lower().replace(' ', '_')}_{date}.pdf",
                              data=_lab_pdf(title, name, date, rows), descripcion=f"{title} · laboratorio",
                              now=f"{date} 11:00:00")
        if case.get("portal_user"):
            with conn:
                conn.execute("INSERT INTO usuarios(id, usuario, hash_password, nombre_mostrado, rol_id, estado_cuenta, "
                             "id_paciente, correo, creado_en) VALUES (?,?,?,?, 4, 'ACTIVO', ?, ?, '2026-09-01 08:00:00')",
                             (next_uid, case["portal_user"], _hash("demo"),
                              f"{case['data']['nombres'].split()[0]} {case['data']['apellidos'].split()[0]}", pid,
                              f"{case['portal_user']}@hslv.demo"))
            next_uid += 1
    # Laura egresa con claritromicina (es alérgica a penicilina) y no alcanza el stock: queda en espera con un pedido
    # en camino. Así se ve el flujo "sin existencias → fecha estimada → se aparta al llegar".
    laura = conn.execute("SELECT id_paciente FROM pacientes_clinicos WHERE numero_documento = '1061700001'").fetchone()
    code = conn.execute("SELECT codigo FROM productos_farmacia WHERE nombre LIKE 'CLARITROMICINA 500 mg TABLETA%'").fetchone()
    if laura and code:
        stock = conn.execute("SELECT disponible FROM v_stock WHERE codigo = ?", (code[0],)).fetchone()[0]
        ps.create_order(conn, code[0], 60, "2026-09-24", 1, "Droguería del Cauca", "2026-09-21 08:00:00")
        ps.prescribe(conn, medico_id=DR_RUIZ, id_paciente=laura[0], codigo=code[0], dosis="500 mg vía oral",
                     frecuencia_horas=12, duracion_dias=7, dosis_prescritas=max(14, stock + 4), ambito="AMBULATORIA",
                     now="2026-09-21 09:30:00")
    # Agenda de hoy (21/09, reloj de la demo a las 10:00): Carlos llegó a su control y tiene turno; Rosa viene más tarde
    ids = {r["numero_documento"]: r["id_paciente"] for r in
           conn.execute("SELECT id_paciente, numero_documento FROM pacientes_clinicos WHERE origen = 'REGISTRO'")}
    now = "2026-09-21 08:00:00"
    if "10542001" in ids:
        cita = sch.book(conn, id_paciente=ids["10542001"], medico_id=DR_RUIZ, fecha_hora="2026-09-21 10:20:00",
                        motivo="CONTROL", creada_por=7, now=now, nota="Control de diabetes con resultados")
        sch.check_in(conn, cita, 7, "2026-09-21 09:55:00")
    if "25270444" in ids:
        sch.book(conn, id_paciente=ids["25270444"], medico_id=DR_RUIZ, fecha_hora="2026-09-21 11:00:00",
                 motivo="CONTROL", creada_por=7, now=now, nota="Control posegreso de insuficiencia cardiaca")
        sch.issue_ticket(conn, servicio="FARMACIA", id_paciente=ids["25270444"], by=7, now="2026-09-21 09:40:00",
                         prioridad=True)
    if "1061900333" in ids:
        sch.book(conn, id_paciente=ids["1061900333"], medico_id=DR_PAREDES, fecha_hora="2026-09-22 08:40:00",
                 motivo="CONTROL", creada_por=7, now=now, nota="Control de gastroenteritis")
    # Ficha de los pacientes que vienen del extracto
    if conn.execute("SELECT 1 FROM pacientes_clinicos WHERE id_paciente = 110").fetchone():
        cr.save_ficha(conn, 110, DR_RUIZ, {"sin_alergias_conocidas": True, "grupo_sanguineo": "O+",
                                           "antecedentes_personales": "Sin antecedentes patológicos de importancia."},
                      "2026-09-20 08:40:00")
