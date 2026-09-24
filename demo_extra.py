"""
demo_extra.py — Más movimiento para la demostración: fila de turnos, citas de hoy, historial de citas, solicitudes
de pacientes, solicitudes de registro y cirugías pedidas desde la app.

Los pacientes adicionales NO se inventan: son personas reales del extracto del reto (anonimizadas, "Paciente
<id>") que ingresaron el 20 y el 21 de septiembre de 2026, con su edad real. La prioridad en la fila (Ley 1171 de
2007) se da a los de 62 años o más según esa edad. Todo ocurre antes de las 10:00 del 21/09, la hora del reloj de
la demo. Las dos personas que piden registro son casos sintéticos, como los demás pacientes con nombre.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import clinical_records as cr
import requests_service as rq
import scheduling as sch
import surgery_planner as sp

FMT = "%Y-%m-%d %H:%M:%S"
DAY = "2026-09-21"
DRA, DR_PAREDES, FACT, ENF = 2, 5, 7, 3


def _his_patients(analytics_db: Path, conn: sqlite3.Connection, limit: int = 36) -> list[dict]:
    src = sqlite3.connect(f"{Path(analytics_db).resolve().as_uri()}?mode=ro", uri=True)
    rows = src.execute("""
        SELECT i.id_paciente, p.edad, MIN(i.fecha_ingreso) AS llegada FROM ingresos i JOIN pacientes p USING(id_paciente)
         WHERE date(i.fecha_ingreso) IN ('2026-09-20', '2026-09-21') GROUP BY 1 ORDER BY 3""").fetchall()
    src.close()
    known = {r[0] for r in conn.execute("SELECT id_paciente FROM pacientes_clinicos")}
    out = []
    for pid, edad, llegada in rows:
        if pid in known:
            continue
        cr.import_his_patient(conn, analytics_db, pid, None, llegada)
        out.append({"id": pid, "edad": edad or 0, "llegada": llegada})
        if len(out) >= limit:
            break
    return out


def _at(minutes: int) -> str:
    return (datetime.strptime(f"{DAY} 07:00:00", FMT) + timedelta(minutes=minutes)).strftime(FMT)


def seed_extra(conn: sqlite3.Connection, analytics_db: Path) -> None:
    people = _his_patients(analytics_db, conn)
    if len(people) < 30:
        return
    senior = lambda p: p["edad"] >= 62   # noqa: E731  prioridad por edad real del extracto

    # 1) Citas de hoy con la Dra. Ruiz y el Dr. Paredes (agendadas ayer por facturación); los que ya llegaron
    #    tienen turno de consulta. Un par ya fueron atendidos.
    booked = []
    for i, p in enumerate(people[:8]):
        doctor, spec = (DR_PAREDES, "PEDIATRIA") if p["edad"] < 18 else (DRA, "MEDICINA GENERAL")
        slots = [x for x in sch.free_slots(conn, medico_id=doctor, day=DAY, now="2026-09-20 18:00:00")
                 if x["fecha_hora"] < f"{DAY} 10:00:00"]      # citas de la mañana: ya llegaron
        if not slots:
            continue
        slot = slots[0]
        try:
            cid = sch.book(conn, id_paciente=p["id"], medico_id=doctor, fecha_hora=slot["fecha_hora"],
                           motivo="PRIMERA_VEZ" if i % 2 else "CONTROL", creada_por=FACT, now="2026-09-20 18:00:00")
            booked.append((cid, p, slot["fecha_hora"]))
        except sqlite3.IntegrityError:
            continue

    # 2) Fila de turnos de la mañana: consulta, farmacia, laboratorio y admisiones.
    minute = 0
    for cid, p, when in booked:
        if True:                                  # llegaron un poco antes de su hora
            arrival = (datetime.strptime(when, FMT) - timedelta(minutes=10)).strftime(FMT)
            try:
                sch.check_in(conn, cid, FACT, arrival, prioridad=senior(p))
            except sqlite3.IntegrityError:
                pass
    plan = [("FARMACIA", people[8:15]), ("LABORATORIO", people[15:21]), ("ADMISIONES", people[21:27]),
            ("CONSULTA", people[27:31])]
    for service, group in plan:
        for p in group:
            minute += 7
            try:
                sch.issue_ticket(conn, servicio=service, id_paciente=p["id"], by=FACT, now=_at(minute),
                                 prioridad=senior(p))
            except sqlite3.IntegrityError:
                continue
    # Se atendió a los primeros de cada servicio; algunos están en ventanilla ahora mismo
    windows = {"FARMACIA": "Ventanilla farmacia 1", "LABORATORIO": "Toma de muestras 1",
               "ADMISIONES": "Ventanilla 2", "CONSULTA": "Consultorio 1"}
    for service, module in windows.items():
        done = 2 if service != "CONSULTA" else 1
        for k in range(done + 1):
            t = _at(150 + k * 12 + len(service))
            row = sch.call_next(conn, servicio=service, modulo=module, by=FACT if service != "CONSULTA" else DRA,
                                now=t)
            if row is None:
                break
            if k < done:
                sch.set_ticket_state(conn, row["id"], "ATENDIDO", FACT, _at(150 + k * 12 + len(service) + 8))
            elif service in ("FARMACIA", "ADMISIONES"):
                sch.set_ticket_state(conn, row["id"], "EN_ATENCION", FACT, t)
    # Uno que no se presentó cuando lo llamaron
    row = sch.call_next(conn, servicio="LABORATORIO", modulo="Toma de muestras 1", by=FACT, now=_at(176))
    if row is not None:
        sch.set_ticket_state(conn, row["id"], "NO_SE_PRESENTO", FACT, _at(179))

    # 3) Historial de citas de la semana pasada con resultado (atendida / no asistió / cancelada)
    with conn:
        for k, p in enumerate(people[27:36]):
            day = datetime(2026, 9, 14) + timedelta(days=k % 5, hours=8 + k % 4)
            state = ("CUMPLIDA", "CUMPLIDA", "NO_ASISTIO", "CANCELADA")[k % 4]
            conn.execute("INSERT INTO citas(id_paciente, medico_id, especialidad, fecha_hora, motivo, estado, creada_en, "
                         "motivo_cancelacion) VALUES (?,?,?,?,?,?,?,?)",
                         (p["id"], DR_PAREDES if p["edad"] < 18 else DRA,
                          "PEDIATRIA" if p["edad"] < 18 else "MEDICINA GENERAL", day.strftime(FMT),
                          "CONTROL", state, "2026-09-10 09:00:00",
                          "El paciente pidió reprogramar" if state == "CANCELADA" else None))

    # 4) Más solicitudes de cita por atender en facturación, con conversación
    asks = [(people[31], "MEDICINA_GENERAL", "Tengo tos seca desde hace una semana y en las noches me da fiebre", "TARDE"),
            (people[32], "RESULTADOS", "Me hicieron exámenes de sangre y quiero que me expliquen los resultados",
             "MANANA"),
            (people[33], "CONTROL", "Necesito el control de la tensión, se me acabaron las pastillas", "CUALQUIERA")]
    for k, (p, tipo, text, pref) in enumerate(asks):
        try:
            rid = rq.create_request(conn, id_paciente=p["id"], tipo=tipo, sintomas=text, preferencia=pref, telefono=None,
                                    canal="PORTAL" if k % 2 else "ASISTENTE", now=_at(20 + k * 25))
        except sqlite3.IntegrityError:
            continue
        if k == 0:
            rq.send_message(conn, rid, lado="PACIENTE", autor_id=None, texto="¿Me pueden atender hoy después de las 3?",
                            now=_at(40), id_paciente=p["id"])
    juan = conn.execute("SELECT s.id, s.id_paciente FROM solicitudes_cita s JOIN pacientes_clinicos p "
                        "ON p.id_paciente = s.id_paciente WHERE p.numero_documento = '1061900333' "
                        "AND s.estado = 'PENDIENTE'").fetchone()
    if juan:
        rq.send_message(conn, juan[0], lado="FACTURACION", autor_id=FACT,
                        texto="Buenos días, ¿el niño sigue con fiebre? Le buscamos cupo con pediatría hoy.",
                        now=_at(95))
        rq.send_message(conn, juan[0], lado="PACIENTE", autor_id=None,
                        texto="Sí, 38,2 esta mañana. Podemos ir a cualquier hora.", now=_at(110),
                        id_paciente=juan[1])

    # 5) Dos personas más piden registrarse (casos sintéticos)
    for nombres, apellidos, doc, mail, cel, when in (
            ("Camilo", "Rivera Ordóñez", "1002345678", "camilo.rivera@correo.demo", "3012223344", "2026-09-20 20:10:00"),
            ("Luz Marina", "Chicangana Muñoz", "34567890", "luz.chicangana@correo.demo", "3175556677",
             "2026-09-21 07:45:00")):
        try:
            rq.create_registration(conn, nombres=nombres, apellidos=apellidos, tipo_documento="CC",
                                   numero_documento=doc, correo=mail, telefono=cel, now=when)
        except sqlite3.IntegrityError:
            pass

    # 6) Cirugías pedidas hoy desde la app por la Dra. Ruiz
    for p, area, prio, proc in (
            (people[34], "QUIROFANOS - CIRUGIA GENERAL", "PRIORITARIA", "Colecistectomía laparoscópica"),
            (people[35], "QUIROFANOS - TRAUMATOLOGIA Y ORTOPEDIA", "PRIORITARIA",
             "Reducción abierta de fractura de tobillo"),
            (people[30], "QUIROFANOS - CIRUGIA GENERAL", "ELECTIVA", "Herniorrafia umbilical")):
        sp.request_surgery(conn, id_paciente=p["id"], area=area, prioridad=prio, procedimiento=proc, user_id=DRA,
                           now=_at(60))
