#!/usr/bin/env python3
"""
Orquestador de pruebas sobre AVDs (emulador de Android Studio).

Para cada AVD:
  1. Arranca el emulador en un puerto fijo (serial conocido: emulator-<puerto>)
  2. Espera a que Android termine de arrancar (con timeout)
  3. Instala el APK (opcional) y lanza la app
  4. Ejecuta el comando de pruebas configurado
  5. Guarda logcat, captura de pantalla y salida de las pruebas
  6. Apaga el emulador y pasa al siguiente

Requisitos: Python 3.8+, Android SDK con platform-tools y emulator.
Uso:
  python orquestador_avd.py                    # usa la config de abajo
  python orquestador_avd.py --avds Pixel_7 Tablet_10
  python orquestador_avd.py --apk app-debug.apk --package com.miempresa.app
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ----------------------------------------------------------------------------
# CONFIGURACIÓN (se puede sobrescribir por línea de comandos)
# ----------------------------------------------------------------------------
CONFIG = {
    # Lista vacía = todos los AVDs que devuelva `emulator -list-avds`
    "avds": [],
    # APK a instalar antes de las pruebas (None = no instalar)
    "apk": None,
    # Paquete de la app a lanzar (None = no lanzar explícitamente)
    "package": "com.miempresa.app",
    # Comando de pruebas. Placeholders disponibles:
    #   {serial}  -> p.ej. emulator-5554
    #   {avd}     -> nombre del AVD
    #   {out}     -> carpeta de resultados de este AVD
    #
    # Ejemplos:
    #   Maestro:  "maestro --device {serial} test flows/ --format junit --output {out}/junit.xml"
    #   Espresso: "adb -s {serial} shell am instrument -w com.miempresa.app.test/androidx.test.runner.AndroidJUnitRunner"
    #   Gradle:   "./gradlew connectedDebugAndroidTest"   (con ANDROID_SERIAL={serial}, ya se exporta)
    #   Appium:   "dotnet test Tests.csproj --logger trx;LogFileName={out}/results.trx"
    "test_command": "maestro --device {serial} test flows/ --format junit --output {out}/junit.xml",
    # Tiempos (segundos)
    "boot_timeout": 300,
    "test_timeout": 1800,
    # Puerto del emulador (debe ser par, entre 5554 y 5682)
    "port": 5554,
    # Opciones extra del emulador
    "headless": True,          # -no-window
    "wipe_data": False,        # -wipe-data: arranque limpio en cada ejecución (más lento)
    "results_dir": "resultados",
}


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def sdk_root() -> Path:
    for var in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        if os.environ.get(var):
            return Path(os.environ[var])
    # Rutas por defecto de Android Studio
    home = Path.home()
    candidates = [
        home / "AppData/Local/Android/Sdk",   # Windows
        home / "Library/Android/sdk",         # macOS
        home / "Android/Sdk",                 # Linux
    ]
    for c in candidates:
        if c.exists():
            return c
    sys.exit("No encuentro el Android SDK. Define ANDROID_SDK_ROOT o ANDROID_HOME.")


SDK = sdk_root()
EXE = ".exe" if os.name == "nt" else ""
ADB = str(SDK / "platform-tools" / f"adb{EXE}")
EMULATOR = str(SDK / "emulator" / f"emulator{EXE}")


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def run(cmd, timeout=60, check=False, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=check, **kw
    )


def adb(serial: str, *args, timeout=60) -> subprocess.CompletedProcess:
    return run([ADB, "-s", serial, *args], timeout=timeout)


def list_avds() -> list:
    out = run([EMULATOR, "-list-avds"]).stdout
    return [l.strip() for l in out.splitlines() if l.strip() and not l.startswith("INFO")]


# ----------------------------------------------------------------------------
# Ciclo de vida del emulador
# ----------------------------------------------------------------------------
def start_emulator(avd: str, port: int, cfg: dict, log_file) -> subprocess.Popen:
    cmd = [EMULATOR, "-avd", avd, "-port", str(port),
           "-no-snapshot-save", "-no-boot-anim", "-no-audio"]
    if cfg["headless"]:
        cmd.append("-no-window")
    if cfg["wipe_data"]:
        cmd.append("-wipe-data")
    log(f"Arrancando {avd} en puerto {port}")
    return subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT)


def wait_for_boot(serial: str, proc: subprocess.Popen, timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            log(f"El emulador terminó inesperadamente (código {proc.returncode})")
            return False
        try:
            r = adb(serial, "shell", "getprop", "sys.boot_completed", timeout=10)
            if r.stdout.strip() == "1":
                # Desbloquear pantalla y quitar animaciones (pruebas más estables)
                adb(serial, "shell", "input", "keyevent", "82")
                for key in ("window_animation_scale", "transition_animation_scale",
                            "animator_duration_scale"):
                    adb(serial, "shell", "settings", "put", "global", key, "0")
                return True
        except subprocess.TimeoutExpired:
            pass
        time.sleep(3)
    return False


def stop_emulator(serial: str, proc: subprocess.Popen) -> None:
    log(f"Apagando {serial}")
    try:
        adb(serial, "emu", "kill", timeout=15)
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        log("No respondió a 'emu kill', forzando cierre")
        proc.kill()
        proc.wait()


# ----------------------------------------------------------------------------
# Ejecución por AVD
# ----------------------------------------------------------------------------
def run_avd(avd: str, cfg: dict, run_dir: Path) -> dict:
    out = run_dir / avd
    out.mkdir(parents=True, exist_ok=True)
    port = cfg["port"]
    serial = f"emulator-{port}"
    result = {"avd": avd, "serial": serial, "status": "unknown", "duration_s": 0}
    t0 = time.time()

    with open(out / "emulator.log", "w") as emu_log:
        proc = start_emulator(avd, port, cfg, emu_log)
        try:
            if not wait_for_boot(serial, proc, cfg["boot_timeout"]):
                result["status"] = "boot_failed"
                log(f"✗ {avd}: no arrancó en {cfg['boot_timeout']}s")
                return result
            log(f"{avd} arrancado en {time.time() - t0:.0f}s")

            if cfg["apk"]:
                log(f"Instalando {cfg['apk']}")
                r = adb(serial, "install", "-r", "-g", cfg["apk"], timeout=300)
                if "Success" not in r.stdout:
                    result["status"] = "install_failed"
                    (out / "install.log").write_text(r.stdout + r.stderr)
                    log(f"✗ {avd}: fallo de instalación")
                    return result

            adb(serial, "logcat", "-c")

            if cfg["package"]:
                log(f"Lanzando {cfg['package']}")
                adb(serial, "shell", "monkey", "-p", cfg["package"],
                    "-c", "android.intent.category.LAUNCHER", "1")
                time.sleep(3)

            cmd = cfg["test_command"].format(serial=serial, avd=avd, out=out.as_posix())
            log(f"Ejecutando pruebas: {cmd}")
            env = {**os.environ, "ANDROID_SERIAL": serial}
            try:
                r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                   timeout=cfg["test_timeout"], env=env)
                (out / "tests.log").write_text(r.stdout + "\n--- STDERR ---\n" + r.stderr)
                # am instrument devuelve 0 aunque fallen tests: miramos la salida
                failed = r.returncode != 0 or "FAILURES!!!" in r.stdout
                result["status"] = "failed" if failed else "passed"
                result["exit_code"] = r.returncode
            except subprocess.TimeoutExpired:
                result["status"] = "test_timeout"

            # Evidencias
            (out / "logcat.txt").write_text(adb(serial, "logcat", "-d", timeout=60).stdout)
            with open(out / "screenshot.png", "wb") as f:
                subprocess.run([ADB, "-s", serial, "exec-out", "screencap", "-p"],
                               stdout=f, timeout=30)

            mark = "✓" if result["status"] == "passed" else "✗"
            log(f"{mark} {avd}: {result['status']}")
            return result
        finally:
            stop_emulator(serial, proc)
            result["duration_s"] = round(time.time() - t0)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description="Orquestador de pruebas en AVDs")
    p.add_argument("--avds", nargs="*", help="AVDs a usar (por defecto: todos)")
    p.add_argument("--apk")
    p.add_argument("--package")
    p.add_argument("--test-command")
    p.add_argument("--wipe-data", action="store_true")
    p.add_argument("--show-window", action="store_true", help="Ver el emulador (no headless)")
    a = p.parse_args()

    cfg = dict(CONFIG)
    if a.avds: cfg["avds"] = a.avds
    if a.apk: cfg["apk"] = a.apk
    if a.package: cfg["package"] = a.package
    if a.test_command: cfg["test_command"] = a.test_command
    if a.wipe_data: cfg["wipe_data"] = True
    if a.show_window: cfg["headless"] = False

    for tool in (ADB, EMULATOR):
        if not Path(tool).exists() and not shutil.which(tool):
            sys.exit(f"No encuentro {tool}")

    avds = cfg["avds"] or list_avds()
    if not avds:
        sys.exit("No hay AVDs. Créalos en Android Studio > Device Manager.")

    run_dir = Path(cfg["results_dir"]) / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    log(f"{len(avds)} AVD(s): {', '.join(avds)}  →  resultados en {run_dir}")

    results = []
    for avd in avds:
        try:
            results.append(run_avd(avd, cfg, run_dir))
        except KeyboardInterrupt:
            log("Interrumpido por el usuario")
            break
        except Exception as e:  # un AVD roto no debe parar al resto
            log(f"✗ {avd}: error inesperado: {e}")
            results.append({"avd": avd, "status": "error", "error": str(e)})

    (run_dir / "resumen.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))

    print("\n=== RESUMEN ===")
    for r in results:
        print(f"  {r['avd']:<30} {r['status']:<15} {r.get('duration_s', '-')}s")
    ok = sum(r["status"] == "passed" for r in results)
    print(f"  {ok}/{len(results)} OK")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
