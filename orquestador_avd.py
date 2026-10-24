#!/usr/bin/env python3
"""
Orquestador de pruebas sobre AVDs (emulador de Android Studio).

Para cada AVD:
  1. Arranca el emulador en un puerto fijo (serial conocido: emulator-<puerto>)
  2. Espera a que Android termine de arrancar (con timeout)
  3. Instala el APK (opcional)
  4. Si hay plan (--plan), elige al azar N acciones para ese AVD y genera
     un flujo de Maestro: abrir app -> acciones -> salir
  5. Ejecuta el comando de pruebas
  6. Guarda logcat, captura de pantalla y salida de las pruebas
  7. Apaga el emulador y pasa al siguiente

Requisitos: Python 3.8+, Android SDK con platform-tools y emulator,
PyYAML si se usa --plan (pip install -r requirements.txt).

Uso:
  python orquestador_avd.py --plan plan.yaml --apk app-debug.apk
  python orquestador_avd.py --plan plan.yaml --dry-run        # ver qué tocaría hoy
  python orquestador_avd.py --plan plan.yaml --seed 2026-10-24 # repetir un día concreto
  python orquestador_avd.py                                    # flows/ sin plan
"""

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

# ----------------------------------------------------------------------------
# CONFIGURACIÓN (se puede sobrescribir por línea de comandos)
# ----------------------------------------------------------------------------
CONFIG = {
    # Lista vacía = todos los AVDs que devuelva `emulator -list-avds`
    "avds": [],
    # APK a instalar antes de las pruebas (None = no instalar)
    "apk": None,
    # Paquete de la app a lanzar antes de las pruebas cuando NO hay plan
    # (con plan, el propio flujo de Maestro abre la app)
    "package": None,
    # Plan de acciones aleatorias (None = ejecutar flows/ tal cual)
    "plan": None,
    # Semilla del azar. None = "<avd>|<fecha de hoy>": distinto por VM y por día,
    # pero reproducible. Pasa una fecha (2026-10-24) para repetir ese día.
    "seed": None,
    # Comando de pruebas. Placeholders disponibles:
    #   {serial}  -> p.ej. emulator-5554
    #   {avd}     -> nombre del AVD
    #   {out}     -> carpeta de resultados de este AVD
    #   {flow}    -> flujo generado desde el plan, o "flows/" si no hay plan
    #
    # Otros ejemplos:
    #   Espresso: "adb -s {serial} shell am instrument -w com.miempresa.app.test/androidx.test.runner.AndroidJUnitRunner"
    #   Gradle:   "./gradlew connectedDebugAndroidTest"   (con ANDROID_SERIAL={serial}, ya se exporta)
    #   Appium:   "mvn test -Dsurefire.reportsDirectory={out}"
    "test_command": "maestro --device {serial} test {flow} --format junit --output {out}/junit.xml",
    # Tiempos (segundos)
    "boot_timeout": 300,
    "test_timeout": 1800,
    # Puerto del emulador (debe ser par, entre 5554 y 5682)
    "port": 5554,
    # Opciones extra del emulador
    "headless": True,          # -no-window
    "wipe_data": False,        # -wipe-data: arranque limpio en cada ejecución (más lento)
    "results_dir": "resultados",
    # Número de VMs que deben ejecutarse. Si hay menos en la lista, la validación falla.
    # None = las que haya.
    "esperadas": 12,
}

ADB = "adb"
EMULATOR = "emulator"


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def init_sdk() -> None:
    """Localiza adb y emulator en el Android SDK."""
    global ADB, EMULATOR
    sdk = None
    for var in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        if os.environ.get(var):
            sdk = Path(os.environ[var])
            break
    if sdk is None:
        home = Path.home()
        for c in (home / "AppData/Local/Android/Sdk",   # Windows
                  home / "Library/Android/sdk",         # macOS
                  home / "Android/Sdk"):                # Linux
            if c.exists():
                sdk = c
                break
    if sdk is None:
        sys.exit("No encuentro el Android SDK. Define ANDROID_SDK_ROOT o ANDROID_HOME.")
    exe = ".exe" if os.name == "nt" else ""
    ADB = str(sdk / "platform-tools" / f"adb{exe}")
    EMULATOR = str(sdk / "emulator" / f"emulator{exe}")
    for tool in (ADB, EMULATOR):
        if not Path(tool).exists():
            sys.exit(f"No encuentro {tool}")


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
# Plan de acciones aleatorias
# ----------------------------------------------------------------------------
def load_plan(path: str) -> dict:
    try:
        import yaml
    except ImportError:
        sys.exit("El plan necesita PyYAML: pip install -r requirements.txt")
    with open(path, encoding="utf-8") as f:
        plan = yaml.safe_load(f)
    if not plan or not plan.get("acciones"):
        sys.exit(f"{path}: el plan no define 'acciones'")
    if not plan.get("appId"):
        sys.exit(f"{path}: el plan no define 'appId'")
    plan.setdefault("acciones_por_ejecucion", 3)
    plan.setdefault("avds", {})
    # Validar que cada AVD solo referencia acciones existentes
    for avd, conf in (plan["avds"] or {}).items():
        for a in (conf or {}).get("acciones", []):
            if a not in plan["acciones"]:
                sys.exit(f"{path}: el AVD '{avd}' usa la acción '{a}', que no existe")
    return plan


def plan_for_avd(plan: dict, avd: str) -> dict:
    """Mezcla la config general del plan con la específica del AVD."""
    conf = (plan.get("avds") or {}).get(avd) or {}
    return {
        "appId": conf.get("appId", plan["appId"]),
        "n": conf.get("acciones_por_ejecucion", plan["acciones_por_ejecucion"]),
        "pool": conf.get("acciones", list(plan["acciones"].keys())),
    }


def pick_actions(plan: dict, avd: str, seed: str = None) -> tuple:
    """Devuelve (acciones elegidas, semilla usada). Mismo AVD + misma semilla = mismas acciones."""
    p = plan_for_avd(plan, avd)
    seed_text = f"{avd}|{seed or date.today().isoformat()}"
    rng = random.Random(seed_text)
    n = min(p["n"], len(p["pool"]))
    return rng.sample(p["pool"], n), seed_text


def build_flow(plan: dict, avd: str, actions: list, path: Path) -> None:
    """Genera el flujo de Maestro: abrir app -> acciones -> salir."""
    import yaml
    p = plan_for_avd(plan, avd)
    steps = [
        {"launchApp": {"clearState": True, "stopApp": True}},
        {"waitForAnimationToEnd": {"timeout": 10000}},
    ]
    for i, name in enumerate(actions, 1):
        steps += plan["acciones"][name]
        steps.append({"waitForAnimationToEnd": {"timeout": 5000}})
        steps.append({"takeScreenshot": f"{i:02d}_{name}"})
    steps.append("stopApp")

    header = {"appId": p["appId"], "name": f"{avd} - {', '.join(actions)}"}
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Generado por orquestador_avd.py para {avd}\n")
        yaml.safe_dump(header, f, allow_unicode=True, sort_keys=False)
        f.write("---\n")
        yaml.safe_dump(steps, f, allow_unicode=True, sort_keys=False)


# ----------------------------------------------------------------------------
# Validación: ¿la app se ejecutó de verdad en esta VM?
# ----------------------------------------------------------------------------
MOTIVOS = {
    "passed":         "OK",
    "no_existe":      "El AVD no existe en este equipo (revisa el nombre en emulator -list-avds)",
    "boot_failed":    "El emulador no terminó de arrancar",
    "install_failed": "Falló la instalación del APK",
    "app_no_abrio":   "La app no llegó a abrirse",
    "crash":          "La app crasheó",
    "anr":            "La app se colgó (ANR)",
    "failed":         "Falló un paso de la prueba",
    "test_timeout":   "Las pruebas superaron el tiempo máximo",
    "no_ejecutada":   "No se llegó a ejecutar (ejecución interrumpida)",
    "error":          "Error inesperado del orquestador",
}


def analyze_logcat(logcat: str, app_id: str) -> dict:
    """Busca en el logcat si la app arrancó, crasheó o se colgó."""
    info = {"abrio": False, "crash": None, "anr": None}
    if not app_id:
        return info
    lines = logcat.splitlines()
    for i, line in enumerate(lines):
        if app_id in line and ("Displayed" in line or "START" in line or "Start proc" in line):
            info["abrio"] = True
        if "FATAL EXCEPTION" in line and info["crash"] is None:
            bloque = lines[i:i + 4]
            if any(f"Process: {app_id}" in l for l in bloque):
                # La línea con la excepción suele ir justo después de "Process: ..."
                exc = next((l for l in bloque if "Exception" in l or "Error" in l), bloque[-1])
                info["crash"] = exc.split(": ", 1)[-1].strip()[:200]
        if f"ANR in {app_id}" in line and info["anr"] is None:
            info["anr"] = line.strip()[:200]
    return info


def failing_step(test_output: str) -> str:
    """Extrae de la salida de Maestro la línea que explica el fallo."""
    claves = ("Element not found", "Assertion is false", "Unable to launch",
              "FAILED", "❌", "Error:")
    for line in test_output.splitlines():
        if any(k in line for k in claves):
            return line.strip()[:200]
    return ""


def app_id_for(avd: str, cfg: dict, plan: dict) -> str:
    return plan_for_avd(plan, avd)["appId"] if plan else cfg["package"]


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
def prepare_flow(avd: str, cfg: dict, plan: dict, out: Path, result: dict) -> str:
    """Si hay plan, elige acciones y genera el flujo. Devuelve el valor de {flow}."""
    if not plan:
        return "flows/"
    actions, seed_text = pick_actions(plan, avd, cfg["seed"])
    flow_path = out / "flujo.yaml"
    build_flow(plan, avd, actions, flow_path)
    result["acciones"] = actions
    result["semilla"] = seed_text
    log(f"{avd}: acciones de hoy -> {', '.join(actions)}")
    return flow_path.as_posix()


def run_avd(avd: str, cfg: dict, plan: dict, run_dir: Path) -> dict:
    out = run_dir / avd
    out.mkdir(parents=True, exist_ok=True)
    port = cfg["port"]
    serial = f"emulator-{port}"
    result = {"avd": avd, "serial": serial, "status": "unknown", "duration_s": 0}
    t0 = time.time()

    flow = prepare_flow(avd, cfg, plan, out, result)

    with open(out / "emulator.log", "w") as emu_log:
        proc = start_emulator(avd, port, cfg, emu_log)
        try:
            if not wait_for_boot(serial, proc, cfg["boot_timeout"]):
                result["status"] = "boot_failed"
                result["detalle"] = f"Sin respuesta tras {cfg['boot_timeout']}s (ver emulator.log)"
                log(f"✗ {avd}: no arrancó en {cfg['boot_timeout']}s")
                return result
            log(f"{avd} arrancado en {time.time() - t0:.0f}s")

            if cfg["apk"]:
                log(f"Instalando {cfg['apk']}")
                r = adb(serial, "install", "-r", "-g", cfg["apk"], timeout=300)
                if "Success" not in r.stdout:
                    result["status"] = "install_failed"
                    msg = (r.stdout + r.stderr).strip().splitlines()
                    result["detalle"] = msg[-1][:200] if msg else ""
                    (out / "install.log").write_text(r.stdout + r.stderr)
                    log(f"✗ {avd}: fallo de instalación")
                    return result

            adb(serial, "logcat", "-c")

            if cfg["package"] and not plan:
                log(f"Lanzando {cfg['package']}")
                adb(serial, "shell", "monkey", "-p", cfg["package"],
                    "-c", "android.intent.category.LAUNCHER", "1")
                time.sleep(3)

            cmd = cfg["test_command"].format(serial=serial, avd=avd,
                                             out=out.as_posix(), flow=flow)
            log(f"Ejecutando pruebas: {cmd}")
            env = {**os.environ, "ANDROID_SERIAL": serial}
            test_output = ""
            try:
                r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                   timeout=cfg["test_timeout"], env=env)
                test_output = r.stdout + "\n" + r.stderr
                (out / "tests.log").write_text(r.stdout + "\n--- STDERR ---\n" + r.stderr)
                # am instrument devuelve 0 aunque fallen tests: miramos la salida
                failed = r.returncode != 0 or "FAILURES!!!" in r.stdout
                result["status"] = "failed" if failed else "passed"
                result["exit_code"] = r.returncode
            except subprocess.TimeoutExpired:
                result["status"] = "test_timeout"

            # Evidencias
            logcat = adb(serial, "logcat", "-d", timeout=60).stdout
            (out / "logcat.txt").write_text(logcat)
            with open(out / "screenshot.png", "wb") as f:
                subprocess.run([ADB, "-s", serial, "exec-out", "screencap", "-p"],
                               stdout=f, timeout=30)

            # Validación: el logcat confirma (o desmiente) que la app se ejecutó
            app_id = app_id_for(avd, cfg, plan)
            lc = analyze_logcat(logcat, app_id)
            result["app_abrio"] = lc["abrio"]
            if lc["crash"]:
                result["status"], result["detalle"] = "crash", lc["crash"]
            elif lc["anr"]:
                result["status"], result["detalle"] = "anr", lc["anr"]
            elif app_id and not lc["abrio"]:
                result["status"] = "app_no_abrio"
                result["detalle"] = failing_step(test_output)
            elif result["status"] == "failed":
                result["detalle"] = failing_step(test_output)

            mark = "✓" if result["status"] == "passed" else "✗"
            log(f"{mark} {avd}: {MOTIVOS.get(result['status'], result['status'])}")
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
    p.add_argument("--plan", help="plan.yaml con el catálogo de acciones aleatorias")
    p.add_argument("--seed", help="Semilla del azar (p.ej. una fecha) para repetir una ejecución")
    p.add_argument("--dry-run", action="store_true",
                   help="Solo muestra y genera los flujos; no arranca emuladores")
    p.add_argument("--esperadas", type=int, help="Número de VMs que deben ejecutarse (por defecto 12)")
    p.add_argument("--test-command")
    p.add_argument("--wipe-data", action="store_true")
    p.add_argument("--show-window", action="store_true", help="Ver el emulador (no headless)")
    a = p.parse_args()

    cfg = dict(CONFIG)
    if a.avds: cfg["avds"] = a.avds
    if a.apk: cfg["apk"] = a.apk
    if a.package: cfg["package"] = a.package
    if a.plan: cfg["plan"] = a.plan
    if a.seed: cfg["seed"] = a.seed
    if a.esperadas is not None: cfg["esperadas"] = a.esperadas
    if a.test_command: cfg["test_command"] = a.test_command
    if a.wipe_data: cfg["wipe_data"] = True
    if a.show_window: cfg["headless"] = False

    plan = load_plan(cfg["plan"]) if cfg["plan"] else None

    if a.dry_run:
        avds = cfg["avds"] or (list(plan["avds"].keys()) if plan else [])
        if not avds:
            init_sdk()
            avds = list_avds()
    else:
        init_sdk()
        avds = cfg["avds"] or list_avds()
    if not avds:
        sys.exit("No hay AVDs. Créalos en Android Studio > Device Manager.")

    run_dir = Path(cfg["results_dir"]) / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    if a.dry_run:
        if not plan:
            sys.exit("--dry-run solo tiene sentido con --plan")
        print(f"Simulación (semilla base: {cfg['seed'] or date.today().isoformat()})\n")
        for avd in avds:
            out = run_dir / avd
            out.mkdir(parents=True, exist_ok=True)
            r = {}
            flow = prepare_flow(avd, cfg, plan, out, r)
            print(f"  {avd:<25} {' -> '.join(r['acciones'])}\n  {'':<25} {flow}\n")
        return 0

    # Los AVDs pedidos que no existen en este equipo cuentan como fallo, no se ignoran
    existentes = set(list_avds())
    results = [{"avd": avd, "status": "no_existe"} for avd in avds if avd not in existentes]
    pendientes = [avd for avd in avds if avd in existentes]

    log(f"{len(pendientes)} AVD(s) a ejecutar: {', '.join(pendientes)}  →  resultados en {run_dir}")

    for i, avd in enumerate(pendientes):
        try:
            results.append(run_avd(avd, cfg, plan, run_dir))
        except KeyboardInterrupt:
            log("Interrumpido por el usuario")
            results += [{"avd": x, "status": "no_ejecutada"} for x in pendientes[i:]]
            break
        except Exception as e:  # un AVD roto no debe parar al resto
            log(f"✗ {avd}: error inesperado: {e}")
            results.append({"avd": avd, "status": "error", "detalle": str(e)[:200]})

    # Respetar el orden original de los AVDs en el informe
    orden = {avd: i for i, avd in enumerate(avds)}
    results.sort(key=lambda r: orden.get(r["avd"], len(orden)))

    return report(results, cfg["esperadas"], run_dir)


def report(results: list, esperadas: int, run_dir: Path) -> int:
    """Valida que todas las VMs ejecutaron la app y muestra cuáles fallaron."""
    ok = [r for r in results if r["status"] == "passed"]
    ko = [r for r in results if r["status"] != "passed"]
    total = max(esperadas or 0, len(results))
    faltan = total - len(results)   # VMs esperadas que ni siquiera estaban en la lista

    resumen = {
        "fecha": datetime.now().isoformat(timespec="seconds"),
        "esperadas": total,
        "ok": len(ok),
        "fallidas": len(ko) + faltan,
        "validacion": "OK" if not ko and not faltan else "FALLIDA",
        "vms": results,
    }
    (run_dir / "resumen.json").write_text(json.dumps(resumen, indent=2, ensure_ascii=False))

    print("\n" + "=" * 78)
    print(f" RESULTADO: {len(ok)}/{total} VMs ejecutaron la app correctamente")
    print("=" * 78)
    for r in results:
        mark = "✓" if r["status"] == "passed" else "✗"
        acciones = ", ".join(r.get("acciones", []))
        dur = f"{r['duration_s']}s" if r.get("duration_s") else ""
        print(f" {mark} {r['avd']:<26} {dur:>6}  {acciones}")

    if ko or faltan:
        print("\n VMs CON PROBLEMAS:")
        for r in ko:
            print(f"\n ✗ {r['avd']}")
            print(f"     Motivo:     {MOTIVOS.get(r['status'], r['status'])}")
            if r.get("detalle"):
                print(f"     Detalle:    {r['detalle']}")
            if r.get("acciones"):
                print(f"     Acciones:   {', '.join(r['acciones'])}")
                print(f"     Repetir:    --avds {r['avd']} --seed {r['semilla'].split('|', 1)[1]}")
            if r["status"] not in ("no_existe", "no_ejecutada"):
                print(f"     Evidencias: {(run_dir / r['avd']).as_posix()}/")
        if faltan:
            print(f"\n ✗ Faltan {faltan} VM(s): se esperaban {total} y solo había {len(results)} en la lista")
        print(f"\n VALIDACIÓN FALLIDA: {len(ko) + faltan} de {total} VM(s) con problemas")
    else:
        print(f"\n VALIDACIÓN OK: las {total} VMs ejecutaron la app")
    print(f" Informe: {(run_dir / 'resumen.json').as_posix()}\n")
    return 0 if not ko and not faltan else 1


if __name__ == "__main__":
    sys.exit(main())
