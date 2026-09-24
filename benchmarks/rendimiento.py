"""
benchmarks/rendimiento.py — Pruebas de rendimiento del centro de mando HSLV.

    python benchmarks/rendimiento.py              # funciones internas + API + microservicios
    python benchmarks/rendimiento.py --ui         # además carga de páginas con varios usuarios (requiere playwright)

Mide con los datos reales del extracto (hospital.db) y sin IA externa (Plan B), para que el resultado no dependa
de la red ni de la cuota de un proveedor. Levanta por su cuenta la API (uvicorn), los microservicios y, con --ui,
la app de Streamlit en puertos libres, y los apaga al terminar. Deja el resultado en benchmarks/resultados.md.

Métricas: p50 (mediana), p95 y máximo en milisegundos; en las pruebas de carga, además solicitudes por segundo y
errores. Los números dependen del computador: sirven para comparar versiones y encontrar cuellos de botella.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LLM_PROVIDER", "none")      # Plan B: sin red ni costo, resultado reproducible

RESULTS: list[dict] = []
QUESTIONS = ["¿Cuántas camas de UCI están ocupadas hoy?",
             "¿Cuáles son los medicamentos con menos de 5 días de inventario?",
             "¿Cuál es el tiempo de espera promedio en urgencias en la última semana?",
             "¿Qué servicio tiene más pacientes ingresados este mes?"]


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def record(group: str, name: str, times_ms: list[float], extra: str = "", errors: int = 0, rps: float | None = None):
    row = {"grupo": group, "prueba": name, "n": len(times_ms), "p50": statistics.median(times_ms),
           "p95": pct(times_ms, 95), "max": max(times_ms), "rps": rps, "errores": errors, "nota": extra}
    RESULTS.append(row)
    print(f"  {name:58s} p50 {row['p50']:8.1f} ms · p95 {row['p95']:8.1f} ms · máx {row['max']:8.1f} ms"
          + (f" · {rps:6.1f} req/s" if rps else "") + (f" · errores {errors}" if errors else ""), flush=True)


def bench(group: str, name: str, fn, n: int = 20, warmup: int = 2, extra: str = ""):
    for _ in range(warmup):
        fn()
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    record(group, name, times, extra)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 90) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status < 500:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def http(method: str, url: str, body: dict | None = None, timeout: float = 30) -> tuple[int, float]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            status = r.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception:
        status = 0
    return status, (time.perf_counter() - t0) * 1000


def load(group: str, name: str, method: str, url: str, body: dict | None, total: int, workers: int):
    """Carga concurrente: `total` solicitudes con `workers` a la vez."""
    t0 = time.perf_counter()
    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(lambda _: http(method, url, body), range(total)))
    wall = time.perf_counter() - t0
    times = [ms for _, ms in results]
    errors = sum(1 for st, _ in results if not 200 <= st < 300)
    record(group, name, times, f"{workers} simultáneos", errors, rps=total / wall)


# ---------------------------------------------------------------------------
# A. Funciones internas (en proceso, sin servidor)
# ---------------------------------------------------------------------------
def internal():
    print("\nA. Funciones internas (en proceso)", flush=True)
    import database as db
    db.ensure_database()
    t0 = time.perf_counter()
    from agent import HospitalAgent
    agent = HospitalAgent(provider="none", mode="rules")
    record("Arranque", "Crear el agente (conexión + intenciones)", [(time.perf_counter() - t0) * 1000])

    for i, q in enumerate(QUESTIONS, 1):
        bench("Agente IA (Plan B)", f"Pregunta {i} del reto", lambda q=q: agent.ask(q), n=30)
    bench("Agente IA (Plan B)", "Bloqueo de SQL malicioso (DROP TABLE)",
          lambda: agent.ask("DROP TABLE ingresos"), n=30)

    ref = agent.ref
    conn = agent.conn
    bench("Tablero", "KPIs del mes (compute_kpis)", lambda: db.compute_kpis(conn, ref.replace(day=1), ref), n=15)
    bench("Tablero", "Ocupación de camas por unidad", lambda: db.kpi_bed_occupancy(conn, ref, by="subgrupo_cama"), n=20)
    bench("Tablero", "Espera en urgencias (7 días)",
          lambda: db.kpi_wait_times(conn, ref.replace(day=max(1, ref.day - 6)), ref), n=20)
    bench("Tablero", "Alertas y recomendaciones", lambda: agent.alerts(), n=10)

    import month_report as mr
    import reports
    bench("Reportes", "Reporte del mes en curso (cálculo)", lambda: mr.month_to_date(conn, ref), n=15)
    mtd = mr.month_to_date(conn, ref)
    bench("Reportes", "Reporte del mes en Excel", lambda: reports.month_to_date_xlsx(mtd), n=10)

    import surgery_planner as sp
    import pharmacy_service as ps
    import demo_seed as ds
    tmp = Path(tempfile.mkdtemp()) / "clinico.db"
    import config
    clin = ps.init_clinical_db(config.DB_PATH, tmp)
    ds.seed(clin, config.DB_PATH)
    bench("Quirófanos", "Perfil de capacidad (16 semanas)", lambda: sp.capacity_profile(conn, ref), n=10)
    prof = sp.capacity_profile(conn, ref)
    waiting = [dict(r) for r in sp.waitlist(clin)]
    bench("Quirófanos", f"Programación sugerida ({len(waiting)} en espera, 14 días)",
          lambda: sp.plan(waiting, prof, ref, 14, {}, {}, f"{ref} 10:00:00"), n=30)

    from ui import notifications as nt
    now = ds.get_clock(clin)
    alerts = agent.alerts()
    for role, user in (("ADMIN", {"id": 1}), ("DOCTOR", {"id": 2}), ("FACTURACION", {"id": 7}),
                       ("QUIROFANOS", {"id": 17})):
        bench("Notificaciones", f"Campana de {role.lower()}", lambda r=role, u=user: nt.collect(r, u, clin, conn, now, alerts),
              n=15)

    import file_assistant as fa
    import pandas as pd
    big = pd.DataFrame({"servicio": ["UCI", "Pediatría", "Urgencias"] * 3334, "dias": list(range(10002)),
                        "nombre": ["x"] * 10002})
    csv = big.to_csv(index=False).encode()
    bench("Asistente con archivos", "CSV de 10.000 filas (resumen + privacidad)",
          lambda: fa.answer("resume", "datos.csv", csv), n=10)
    clin.close()


# ---------------------------------------------------------------------------
# B. API REST (FastAPI) bajo carga
# ---------------------------------------------------------------------------
def api_load():
    print("\nB. API REST (FastAPI + uvicorn) con usuarios simultáneos", flush=True)
    port = free_port()
    env = {**os.environ, "LLM_PROVIDER": "none"}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "api:app", "--port", str(port), "--log-level", "warning"],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = f"http://127.0.0.1:{port}"
        if not wait_http(base + "/health"):
            print("  La API no arrancó; se omite.")
            return
        for workers in (1, 10, 25):
            load("API REST", f"POST /api/query (pregunta 1 del reto)", "POST", base + "/api/query",
                 {"question": QUESTIONS[0]}, total=100, workers=workers)
        load("API REST", "GET /api/kpis", "GET", base + "/api/kpis", None, total=60, workers=10)
        load("API REST", "GET /api/alerts", "GET", base + "/api/alerts", None, total=40, workers=10)
    finally:
        proc.terminate()
        proc.wait(timeout=15)


# ---------------------------------------------------------------------------
# C. Microservicios predictivos
# ---------------------------------------------------------------------------
def microservices():
    print("\nC. Microservicios de pronóstico (Flask + Random Forest)", flush=True)
    ports = {"urgencias": 5001, "quirofanos": 5002, "farmacia": 5003, "consultas": 5004}
    up = all(wait_http(f"http://127.0.0.1:{p}/health", timeout=1) for p in ports.values())
    proc = None
    if not up:
        t0 = time.perf_counter()
        proc = subprocess.Popen([sys.executable, "microservicios/run_services.py"], cwd=ROOT,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ready = True
        for name, p in ports.items():
            end = time.time() + 180
            while time.time() < end:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{p}/health", timeout=3) as r:
                        if json.loads(r.read()).get("modelo_entrenado"):
                            break
                except Exception:
                    pass
                time.sleep(1)
            else:
                ready = False
        if not ready:
            print("  Los microservicios no quedaron listos; se omite.")
            proc.terminate()
            return
        record("Microservicios", "Arranque + entrenamiento de los 4 modelos", [(time.perf_counter() - t0) * 1000])
    try:
        for name, p in ports.items():
            load("Microservicios", f"POST /predict · {name}", "POST", f"http://127.0.0.1:{p}/predict", {},
                 total=40, workers=5)
    finally:
        if proc:
            proc.terminate()
            proc.wait(timeout=20)


# ---------------------------------------------------------------------------
# D. Interfaz (Streamlit) con varios usuarios a la vez
# ---------------------------------------------------------------------------
def ui_load(users: int):
    print(f"\nD. Interfaz Streamlit: carga de páginas ({users} usuarios a la vez)", flush=True)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  Falta playwright (pip install playwright && playwright install chromium); se omite.")
        return
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "app.py", "--server.port", str(port),
                             "--server.headless", "true"], cwd=ROOT, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    wait_http(url)
    pages = ["Mapa de camas", "Indicadores", "Programación quirúrgica", "Clínico y farmacia", "Asistente IA"]

    def ready(pg):
        """La página terminó de dibujarse: Streamlit ya no está ejecutando el script."""
        pg.wait_for_timeout(150)
        pg.wait_for_function("() => !document.querySelector('[data-testid=\"stStatusWidget\"]')", timeout=60000)

    def one_user(i: int) -> dict:
        out = {}
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 1400, "height": 900})
            t0 = time.perf_counter()
            pg.goto(url)
            pg.get_by_text("Acceso rápido para la demostración").wait_for(timeout=120000)
            out["login"] = (time.perf_counter() - t0) * 1000
            pg.get_by_text("Acceso rápido para la demostración").click()
            t0 = time.perf_counter()
            pg.get_by_role("button", name="Gerencia").click()
            pg.get_by_role("link", name="Mapa de camas").first.wait_for(timeout=120000)
            ready(pg)
            out["Ingresar (Hoy)"] = (time.perf_counter() - t0) * 1000
            for name in pages:
                t0 = time.perf_counter()
                pg.get_by_role("link", name=name).first.click()
                ready(pg)
                out[name] = (time.perf_counter() - t0) * 1000
            b.close()
        return out

    try:
        one_user(0)  # calentamiento: primera construcción de cachés
        with ThreadPoolExecutor(users) as ex:
            runs = list(ex.map(one_user, range(users)))
        for key in ["login", "Ingresar (Hoy)", *pages]:
            label = "Abrir la pantalla de inicio de sesión" if key == "login" else f"Página: {key}"
            record("Interfaz", label, [r[key] for r in runs], f"{users} usuarios a la vez")
    finally:
        proc.terminate()
        proc.wait(timeout=20)


# ---------------------------------------------------------------------------
def write_markdown(path: Path, args):
    lines = [f"# Resultados de rendimiento · {datetime.now():%Y-%m-%d %H:%M}", "",
             f"Equipo de prueba: {platform.system()} {platform.release()} · {os.cpu_count()} núcleos · Python "
             f"{platform.python_version()}. Motor del agente: Plan B (sin IA externa). Datos: extracto del reto.", "",
             "| Grupo | Prueba | n | p50 (ms) | p95 (ms) | Máx (ms) | req/s | Errores | Nota |",
             "|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for r in RESULTS:
        lines.append(f"| {r['grupo']} | {r['prueba']} | {r['n']} | {r['p50']:.1f} | {r['p95']:.1f} | {r['max']:.1f} | "
                     f"{'' if r['rps'] is None else format(r['rps'], '.1f')} | {r['errores']} | {r['nota']} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nResultados guardados en {path.relative_to(ROOT)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ui", action="store_true", help="incluir la carga de páginas de Streamlit (playwright)")
    ap.add_argument("--usuarios", type=int, default=5, help="usuarios simultáneos en la prueba de interfaz")
    ap.add_argument("--solo", choices=["interno", "api", "micro", "ui"], help="correr solo un bloque")
    args = ap.parse_args()
    blocks = {"interno": internal, "api": api_load, "micro": microservices, "ui": lambda: ui_load(args.usuarios)}
    todo = [args.solo] if args.solo else ["interno", "api", "micro"] + (["ui"] if args.ui else [])
    for key in todo:
        try:
            blocks[key]()
        except Exception as exc:  # un bloque que falla no detiene los demás
            print(f"  El bloque {key} falló: {type(exc).__name__}: {exc}")
    write_markdown(ROOT / "benchmarks" / "resultados.md", args)


if __name__ == "__main__":
    main()
