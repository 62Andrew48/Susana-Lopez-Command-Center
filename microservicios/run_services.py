"""
run_services.py — Levanta los cuatro microservicios en paralelo con un solo comando.

    python microservicios/run_services.py                     # los cuatro
    python microservicios/run_services.py urgencias farmacia  # solo algunos

Funciona igual en PowerShell, Git Bash, Linux y macOS: usa sys.executable (el Python del entorno
virtual activo) y subprocess.Popen. Cada servicio corre en su propio proceso y puerto.
Ctrl+C detiene los cuatro de forma ordenada (terminate -> wait -> kill si no responden) y libera
los puertos.

Puertos: se leen de URGENCIAS_URL, QUIROFANOS_URL, FARMACIA_URL y CONSULTAS_URL (en el entorno o en
el .env de la raíz; admiten "http://127.0.0.1:5001" o solo "5001"). Por defecto 5001-5004.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

AQUI = Path(__file__).resolve().parent
ROOT_DIR = AQUI.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
try:
    import config  # noqa: F401  (carga el .env de la raíz si python-dotenv está instalado)
except Exception:  # el orquestador no debe fallar por la configuración de la app principal
    pass

# nombre: (carpeta, variable de entorno, puerto por defecto)
SERVICIOS = {
    "urgencias": ("service_urgencias", "URGENCIAS_URL", 5001),
    "quirofanos": ("service_quirofanos", "QUIROFANOS_URL", 5002),
    "farmacia": ("service_farmacia", "FARMACIA_URL", 5003),
    "consultas": ("service_consultas", "CONSULTAS_URL", 5004),
}
ES_WINDOWS = os.name == "nt"
_imprimir = threading.Lock()


def log(nombre: str, texto: str) -> None:
    with _imprimir:
        print(f"[{nombre:<10}] {texto}", flush=True)


def puerto_de(variable: str, por_defecto: int) -> int:
    valor = (os.getenv(variable) or "").strip()
    if valor.isdigit():
        return int(valor)
    if valor:
        try:
            puerto = urlparse(valor if "://" in valor else f"http://{valor}").port
            if puerto:
                return puerto
        except ValueError:
            pass
    return por_defecto


def puerto_libre(host: str, puerto: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, puerto))
            return True
        except OSError:
            return False


def reenviar_salida(nombre: str, proceso: subprocess.Popen) -> None:
    for linea in proceso.stdout:  # termina cuando el proceso cierra su salida
        log(nombre, linea.rstrip())


def lanzar(nombre: str, carpeta: str, puerto: int, host: str, urls: dict[str, str]) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(urls)  # cada servicio conoce la URL de los demás
    env.update({"PORT": str(puerto), "HOST": host, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
    opciones: dict = {}
    if ES_WINDOWS:
        # Grupo propio: el Ctrl+C lo recibe solo el orquestador, que cierra a los hijos en orden.
        opciones["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        opciones["start_new_session"] = True
    proceso = subprocess.Popen(
        [sys.executable, "-u", str(AQUI / carpeta / "app.py")], cwd=str(AQUI / carpeta), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        bufsize=1, **opciones)
    threading.Thread(target=reenviar_salida, args=(nombre, proceso), daemon=True).start()
    return proceso


def consultar_health(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=2) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def detener(procesos: dict[str, subprocess.Popen], espera: float = 8.0) -> None:
    vivos = {n: p for n, p in procesos.items() if p.poll() is None}
    for p in vivos.values():
        p.terminate()
    limite = time.monotonic() + espera
    for nombre, p in vivos.items():
        try:
            p.wait(timeout=max(0.1, limite - time.monotonic()))
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        log(nombre, "detenido")


def _sigterm(*_):
    raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser(description="Orquestador local de los microservicios HSLV")
    parser.add_argument("servicios", nargs="*", help=f"Servicios a iniciar: {', '.join(SERVICIOS)} (por defecto, todos)")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--timeout", type=float, default=60, help="Segundos máximos para que respondan /health")
    args = parser.parse_args()
    desconocidos = [n for n in args.servicios if n not in SERVICIOS]
    if desconocidos:
        parser.error(f"Servicios desconocidos: {desconocidos}. Opciones: {list(SERVICIOS)}")
    elegidos = args.servicios or list(SERVICIOS)

    puertos = {n: puerto_de(SERVICIOS[n][1], SERVICIOS[n][2]) for n in SERVICIOS}
    urls = {SERVICIOS[n][1]: f"http://{args.host}:{puertos[n]}" for n in SERVICIOS}
    ocupados = [f"{n} ({puertos[n]})" for n in elegidos if not puerto_libre(args.host, puertos[n])]
    if ocupados:
        print("Puertos ocupados: " + ", ".join(ocupados) + ". Cierra el proceso que los usa o cambia "
              "la variable *_URL en el .env.", file=sys.stderr)
        return 1

    procesos: dict[str, subprocess.Popen] = {}
    # Si el orquestador se lanzó con SIGINT ignorado (p. ej. en segundo plano desde un script),
    # se restaura el manejador estándar para que Ctrl+C siempre produzca KeyboardInterrupt.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    if not ES_WINDOWS:  # `kill <pid>` del orquestador también cierra a los hijos
        signal.signal(signal.SIGTERM, _sigterm)
    try:
        for nombre in elegidos:
            procesos[nombre] = lanzar(nombre, SERVICIOS[nombre][0], puertos[nombre], args.host, urls)

        pendientes = set(elegidos)
        limite = time.monotonic() + args.timeout
        while pendientes and time.monotonic() < limite:
            for nombre in sorted(pendientes):
                if procesos[nombre].poll() is not None:
                    log(nombre, f"terminó durante el arranque (código {procesos[nombre].returncode})")
                    pendientes.discard(nombre)
                elif (estado := consultar_health(urls[SERVICIOS[nombre][1]])) is not None:
                    log(nombre, f"listo en {urls[SERVICIOS[nombre][1]]} · estado={estado.get('estado')} "
                                f"· fecha_referencia={estado.get('fecha_referencia')}")
                    pendientes.discard(nombre)
            time.sleep(0.3)
        for nombre in pendientes:
            log(nombre, f"no respondió /health en {args.timeout:.0f} s")

        print("\nServicios en ejecución. Ctrl+C para detenerlos.\n", flush=True)
        while any(p.poll() is None for p in procesos.values()):
            for nombre, p in procesos.items():
                if p.poll() is not None and not getattr(p, "_avisado", False):
                    log(nombre, f"se detuvo inesperadamente (código {p.returncode})")
                    p._avisado = True  # type: ignore[attr-defined]
            time.sleep(1)
        return 1
    except KeyboardInterrupt:
        print("\nDeteniendo servicios…", flush=True)
        return 0
    finally:
        detener(procesos)


if __name__ == "__main__":
    sys.exit(main())
