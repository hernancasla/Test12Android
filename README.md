# test12android

Orquestador de pruebas automáticas sobre emuladores Android (AVD de Android Studio).

Recorre los AVDs uno a uno. Para cada uno arranca el emulador, espera a que Android termine de bootear, instala y lanza la app, ejecuta las pruebas, guarda las evidencias y apaga el emulador antes de pasar al siguiente.

```
┌──────────────┐   adb    ┌──────────────┐
│ orquestador  │ ───────► │  AVD #1      │  arrancar → boot → instalar → lanzar → tests → apagar
│ (host)       │ ───────► │  AVD #2      │
│              │ ───────► │  AVD #N      │
└──────────────┘          └──────────────┘
```

No hace falta ningún programa dentro de los emuladores: todo se controla desde el host vía `adb`.

## Requisitos

- Python 3.8+
- Android SDK (el de Android Studio) con `platform-tools` y `emulator`
- AVDs creados en **Android Studio → Device Manager**
- El framework de pruebas que vayas a usar (Maestro, Espresso, Appium…)

El SDK se detecta con `ANDROID_SDK_ROOT` / `ANDROID_HOME` o, si no están definidas, en la ruta por defecto de Android Studio (Windows, macOS o Linux).

## Uso

```bash
# Todos los AVDs, con la configuración por defecto
python orquestador_avd.py

# AVDs concretos, instalando un APK
python orquestador_avd.py --avds Pixel_7 Tablet_10 --apk app-debug.apk --package com.miempresa.app

# Otro comando de pruebas
python orquestador_avd.py --test-command "maestro --device {serial} test flows/"

# Ver la ventana del emulador (útil para depurar)
python orquestador_avd.py --show-window

# Arranque limpio (borra los datos del AVD)
python orquestador_avd.py --wipe-data
```

## Configuración

Los valores por defecto están en el diccionario `CONFIG`, al principio del script:

| Clave          | Descripción                                                | Defecto              |
|----------------|------------------------------------------------------------|----------------------|
| `avds`         | Lista de AVDs (vacía = todos)                              | `[]`                 |
| `apk`          | APK a instalar antes de las pruebas                        | `None`               |
| `package`      | Paquete de la app a lanzar                                 | `com.miempresa.app`  |
| `test_command` | Comando de pruebas (ver placeholders)                      | Maestro              |
| `boot_timeout` | Segundos máximos de espera al arranque                     | `300`                |
| `test_timeout` | Segundos máximos para las pruebas de un AVD                | `1800`               |
| `port`         | Puerto del emulador (serial `emulator-<puerto>`)           | `5554`               |
| `headless`     | Arrancar sin ventana                                       | `True`               |
| `wipe_data`    | Borrar datos en cada arranque                              | `False`              |
| `results_dir`  | Carpeta de resultados                                      | `resultados`         |

### Placeholders de `test_command`

- `{serial}`: serial del emulador, p. ej. `emulator-5554`
- `{avd}`: nombre del AVD
- `{out}`: carpeta de resultados de ese AVD

Además, se exporta `ANDROID_SERIAL={serial}`, así que herramientas como Gradle apuntan al emulador correcto sin configuración extra.

### Ejemplos

```text
# Maestro
maestro --device {serial} test flows/ --format junit --output {out}/junit.xml

# Espresso / UI Automator (instrumentadas)
adb -s {serial} shell am instrument -w com.miempresa.app.test/androidx.test.runner.AndroidJUnitRunner

# Gradle
./gradlew connectedDebugAndroidTest

# Appium con .NET
dotnet test Tests.csproj --logger "trx;LogFileName={out}/results.trx"
```

## Resultados

```
resultados/
└── 20261024_110000/
    ├── resumen.json
    ├── Pixel_7/
    │   ├── emulator.log
    │   ├── tests.log
    │   ├── logcat.txt
    │   ├── screenshot.png
    │   └── junit.xml        (si el framework lo genera)
    └── Tablet_10/
        └── ...
```

Estados posibles por AVD: `passed`, `failed`, `boot_failed`, `install_failed`, `test_timeout`, `error`.

El script devuelve código `0` si todos los AVDs pasan y `1` en caso contrario, lo que permite usarlo en CI (Azure DevOps, GitHub Actions…).

## Próximos pasos

- [ ] Ejecución en paralelo (un puerto por emulador)
- [ ] Informe HTML agregado
- [ ] Reintentos configurables por AVD
- [ ] Pipeline de CI
