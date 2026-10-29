<p align="center">
  <a href="README.md"><img src="https://img.shields.io/badge/Lang-Italiano-green?style=for-the-badge" alt="Italiano"></a>
  <a href="README.en.md"><img src="https://img.shields.io/badge/Lang-English-blue?style=for-the-badge" alt="English"></a>
  <a href="README.fr.md"><img src="https://img.shields.io/badge/Lang-Français-blue?style=for-the-badge" alt="Français"></a>
  <a href="README.de.md"><img src="https://img.shields.io/badge/Lang-Deutsch-blue?style=for-the-badge" alt="Deutsch"></a>
  <a href="README.es.md"><img src="https://img.shields.io/badge/Lang-Español-orange?style=for-the-badge" alt="Español"></a>
</p>

# KlamAV-Py

**Licencia:** GNU General Public License v3.0

Frontend mínimo para ClamAV mediante clamd — CLI y GUI para Linux.

Reescritura mínima, en Python, de la idea base de KlamAV 0.22 (frontend a ClamAV), sin las partes que se han convertido en tecnología muerta (Dazuko, DCOP, Qt3) y sin los problemas de seguridad del original (inyección de shell mediante KShellProcess): no se invoca ningún ejecutable externo para el escaneo, y las rutas nunca pasan por un shell.

<p align="center">
  <img src="docs/screenshots/scan.png" alt="KlamAV-Py — escaneo" width="45%" />
  <img src="docs/screenshots/quarantine.png" alt="Cuarentena" width="45%" />
</p>
<p align="center">
  <img src="docs/screenshots/history.png" alt="Historial" width="45%" />
  <img src="docs/screenshots/settings.png" alt="Ajustes" width="45%" />
</p>

## Características

- Escaneo mediante el protocolo nativo de clamd (INSTREAM/IDSESSION sobre socket Unix), CLI y GUI.
- CLI sin dependencias externas (solo biblioteca estándar), GUI en PySide6.
- Cuarentena con permisos neutralizados (0400), nombre en disco impredecible, restauración de los permisos originales, índice compartido y protegido por lock entre CLI/GUI/worker.
- Escaneos programados: temporizador interno de la GUI y/o unidades systemd de usuario (mediante el paquete .deb), con registro persistente de los resultados.
- Monitorización en tiempo real RECURSIVA de las carpetas configuradas (QFileSystemWatcher, con expansión de subcarpetas, reconciliación periódica de los watch perdidos y techo configurable para no agotar fs.inotify.max_user_watches).
- Pausa/Reanudación de los escaneos, guardia de "un escaneo a la vez".
- Pre-comprobación dimensional: los archivos que superan StreamMaxLength se marcan como "no verificados" sin malgastar la sesión de clamd.
- Toma de conocimiento de las detecciones no movidas (firmas heurísticas, archivos de correo): tras verificarlas ya no se cuentan entre los infectados, hasta que el contenido cambie.
- Integración en el menú contextual de Dolphin, inicio automático, bandeja del sistema, single-instance con IPC.
- Control de actualizaciones de la aplicación mediante GitHub Releases, manual o al inicio.

## Requisitos

- clamav-daemon instalado y activo (clamd), no solo los binarios clamscan/freshclam: este proyecto habla con el demonio por socket, no invoca ejecutables externos para el escaneo.
- Python 3.10+ como línea base declarada. El mínimo técnico real es 3.9 (Path.is_relative_to); el operador walrus usado en clamd_client.py existe desde 3.8. Probado localmente en 3.14 (Debian Sid) y en CI en 3.10–3.14.
- CLI: sin dependencias externas en tiempo de ejecución — funciona también con el Python del sistema, sin venv.
- GUI: PySide6, debe instalarse en un venv dedicado (ver más abajo), no en el Python del sistema.
- GUI, solo para la actualización de la base de datos de virus desde el botón "Actualizar Base de Datos": freshclam en el PATH y pkexec (PolicyKit) disponibles, ya que la operación requiere privilegios de root.
- GUI, solo para la integración con el menú contextual de Dolphin: un entorno KDE Plasma con kbuildsycoca5/kbuildsycoca6.
- Desarrollo/pruebas: pytest (ver requirements-dev.txt), no requerido en tiempo de ejecución.

## Instalación

### Desde release precompilada (recomendada para el uso diario)

Descarga el paquete .deb desde la página Releases e instala:

```bash
sudo dpkg -i klamav-py_*.deb
sudo apt-get install -f    # corrige las dependencias faltantes, si hacen falta
```

Instala klamav-py (CLI) y klamav-py-gui en /usr/bin/, habilita automáticamente el temporizador systemd de usuario para el escaneo programado y registra la aplicación en los menús. Detalles y advertencias en la sección "Empaquetado .deb" más abajo.

### Desde AUR (Arch Linux y derivadas)

```sh
git clone https://aur.archlinux.org/klamav-py.git
cd klamav-py && makepkg -si
```

O con un helper de AUR: `yay -S klamav-py`, `paru -S klamav-py`.

El paquete también vale para Manjaro, EndeavourOS, CachyOS y las otras derivadas que usan AUR. `pyside6` y `clamav` son dependencias opcionales a nivel pacman: la CLI funciona sin GUI, pero para el uso normal se necesitan ambas.

A diferencia del `.deb`, el temporizador de escaneo programado **no** se habilita durante la instalación: en Arch la política es que el estado del sistema lo decide el usuario. El paquete imprime la instrucción a ejecutar:

```sh
systemctl --user enable --now klamav-scan.timer
```

### Desde fuentes (desarrollo o build del paquete)

**Build del .deb:**

```bash
sudo apt install devscripts debhelper dh-python pybuild-plugin-pyproject \
    python3-all python3-setuptools
cd klamav-py
dpkg-buildpackage -us -uc -b
sudo dpkg -i ../klamav-py_<versione>_all.deb
```

**Entorno de desarrollo (GUI desde checkout):**

```bash
cd klamav-py
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Con Python 3.10 y 3.11 `requirements.txt` instala PySide6 6.11.x: las wheels desde la 6.12 están compiladas para Python 3.12 y en esas versiones abortan la GUI tras unos pocos cientos de señales (`Fatal Python error: bool_dealloc`; [PYSIDE-3474](https://qt-project.atlassian.net/browse/PYSIDE-3474)). En un venv ya existente basta repetir `pip install -r requirements.txt`; para comprobar: `python -c "import PySide6; print(PySide6.__version__)"` debe decir 6.11 o anterior.

Con el venv activo (o llamando directamente a venv/bin/python):

```bash
python3 -m klamav_py.gui.app
```

La CLI en cambio no necesita el venv y funciona con el Python del sistema.

Nota para el desarrollo: si en el venv existe también una copia instalada del paquete (`pip install .` no-editable), el import de klamav_py puede resolverse allí en lugar de en el checkout según el directorio actual. Para evitar la trampa usa `pip install -e .`; y en una máquina con checkout y .deb instalados juntos, verifica siempre qué instancia de la GUI está realmente viva (`ps aux | grep klamav`): el single-instance hace que el segundo lanzamiento sea absorbido por la primera instancia existente, sea cual sea.

## Uso rápido

```bash
# CLI: verifica que clamd responda (funciona también sin venv)
python3 -m klamav_py.cli ping

# CLI: escaneo con cuarentena automática
python3 -m klamav_py.cli scan /home/utente/Scaricati \
    --quarantine ~/.local/share/klamav-py/quarantine

# CLI: escaneo de la home con exclusiones y log de errores en archivo
python3 -m klamav_py.cli scan ~ \
    --exclude ~/.local/share/klamav-py \
    --exclude ~/.cache \
    --log-errors ~/klamav-errors.log

# GUI (requiere el venv activo o el intérprete del venv, ver arriba)
python3 -m klamav_py.gui.app

# o bien, con socket/cuarentena no estándar:
python3 -m klamav_py.gui.app --socket /run/clamav/clamd.ctl \
    --quarantine-dir ~/.local/share/klamav-py/quarantine

# o bien para arrancar directamente el escaneo de una ruta (usado
# también por la integración de Dolphin, ver más abajo):
python3 -m klamav_py.gui.app --scan-target /percorso/da/scansionare
```

El directorio pasado a `--quarantine` se excluye automáticamente del recorrido, tanto en CLI como en GUI: los archivos ya gestionados no deben ser re-detectados (y re-cuarentenados) en cada escaneo que cubra su posición.

## CLI: opciones de escaneo

- `--quarantine DIR` — mueve los archivos infectados a DIR (creada con permisos 0700).
- `--exclude DIR` — directorio a excluir del recorrido recursivo (repetible). Los directorios excluidos ni siquiera se leen (pruning): útil para cache, Trash, mounts de red sincronizados.
- `--max-stream-size BYTES` — umbral de la pre-comprobación dimensional (por defecto 25MB, alineado con StreamMaxLength de clamd.conf; 0 desactiva la pre-comprobación).
- `--log-errors FILE` — detalle de cada error (ruta y motivo) en archivo. El archivo se crea con permisos 0600; symlink, FIFO y archivos ya existentes de otro usuario se rechazan (exit 2). Evita aun así directorios compartidos como /tmp: el log lista rutas de tus archivos.
- `--version` — imprime la versión y sale (opción global, no del subcomando scan).
- `--no-persistent` / `--session-batch-size N` — control de la sesión persistente (ver sección dedicada).

Códigos de salida de `scan` — útiles para OnFailure= en systemd o para scripts de monitorización. Vale el primer caso que se aplique, en este orden:

| Orden | Salida | Caso |
| --- | --- | --- |
| 1 | 2 | Error bloqueante, el escaneo no arranca o se interrumpe: clamd inalcanzable; ruta inexistente, enlace roto, ni directorio ni archivo regular, o no legible; cuarentena o `--log-errors` no utilizables |
| 2 | 1 | Al menos una infección encontrada, también con fallos de E/S |
| 3 | 2 | Al menos un fallo de E/S sin infecciones (ver más abajo) |
| 4 | 0 | En caso contrario |

Los permisos denegados, los archivos desaparecidos durante el escaneo, los archivos demasiado grandes y los errores de clamd sobre un solo archivo no cambian el código. Una subcarpeta con permisos denegados no cambia el código: aparece en stderr como `CARTELLA NON LEGGIBILE` y tiene un contador aparte en el resumen; si es esperada (carpetas creadas con sudo, bind mount de contenedores) debe excluirse con `--exclude`. Otro error de lectura, sobre un archivo o una subcarpeta (EIO de un disco que degrada, ESTALE o ETIMEDOUT de un mount NFS que no responde), es un fallo: cuenta entre los errores y, sin infecciones, el escaneo sale con 2 porque parte del árbol no fue controlada; la última línea del resumen lo indica también con salida 1. En la GUI el resultado es «Completada con errores», y la misma frase aparece en el estado y en el informe del escaneo manual, en la notificación y en el registro de la programación interna; el Historial tiene una columna «Fallos de E/S».

## Detecciones no movidas y toma de conocimiento

Las firmas heurísticas (`Heuristics.*`) y los archivos en los archivos de correo solo se señalan, nunca se mueven automáticamente: mover un mensaje rompe el archivo del cliente de correo. Una detección de este tipo, sin embargo, se repetía en cada escaneo, y con el temporizador del sistema cada noche llegaba la misma notificación, hasta que dejaba de mirarse.

Tras verificar el archivo, elimínalo o registra la toma de conocimiento:

```bash
journalctl --user -u klamav-scan.service | grep INFETTO
klamav-py --acknowledge ~/.local/share/local-mail/trash/new/1695.R42.host
klamav-py --list-acknowledged        # prefijo del hash, firma, ruta, fechas
klamav-py --unacknowledge 3f2a9c01be47   # por prefijo del hash o por ruta
```

`--acknowledge` vuelve a escanear el archivo con clamd y registra la toma de conocimiento solo si sigue infectado con una detección de "solo señalización". La regla es la del escaneo, por lo que debe llamarse con las mismas `--report-only` (y `--quarantine-all`) usadas en el escaneo, indicadas antes: `klamav-py --report-only ~/Archivio --acknowledge ~/Archivio/vecchio.exe`. Los escaneos siguientes imprimen `GIÀ VALUTATO` y no lo cuentan entre los infectados: una detección **nueva** sigue saliendo con 1 y notifica. La clave es el SHA-256 del contenido más la firma, no la ruta: un mensaje que pasa de `new/` a `cur/` queda evaluado, un contenido cambiado vuelve a señalarse, y una toma de conocimiento nunca oculta una detección que iría a cuarentena. La toma de conocimiento vale, por tanto, para el contenido y la firma, en cualquier ruta: una copia idéntica en otra parte, con la misma firma, resulta ya evaluada. Las entradas que ningún escaneo vuelve a encontrar en 90 días se eliminan. Registro en `~/.local/share/klamav-py/acknowledged.json` (0600), compartido con la GUI, donde la página **Detecciones** ofrece las mismas acciones (y la eliminación, solo si el archivo sigue siendo el detectado; para un maildir gestionado por Akonadi puede hacer falta luego `akonadictl fsck`). La página muestra también las detecciones del temporizador del sistema y de la CLI: cada escaneo las escribe en `~/.local/share/klamav-py/pending-reports.json` (0600) en cuanto las encuentra, así que sobreviven a un escaneo interrumpido, y permanecen allí hasta que eliminas el archivo, registras la toma de conocimiento o el archivo deja de existir.

## Sesión persistente, pre-comprobación dimensional, reconstrucción

Por defecto `scan_stream` reutiliza una única conexión (sesión IDSESSION) para varios archivos seguidos en lugar de abrir una nueva para cada uno — en árboles con decenas de miles de archivos evita el overhead de connect repetido. La sesión se cierra y reabre aun así cada 500 archivos (`--session-batch-size`) para limitar el impacto de límites no documentados en sesiones muy largas, y se recrea desde cero cada vez que algo va mal: un archivo problemático no compromete el resto del escaneo.

Cuando clamd cierra la conexión (caso típico: rechazo por StreamMaxLength), la sesión se marca como muerta y se recrea antes del archivo siguiente, así los archivos siguientes no mueren en cascada sobre una conexión ya cerrada.

Pre-comprobación dimensional: los archivos por encima del umbral (por defecto 25MB, como StreamMaxLength) no se envían en absoluto a clamd — salen inmediatamente con status TOO_LARGE ("no verificado"), distinto de los errores reales tanto en CLI como en GUI. Sin pre-comprobación, clamd rechaza el stream a mitad de envío cerrando la conexión: el error que vería el usuario sería una "tubería rota" sobre un archivo que en realidad está bien (ver "Diagnóstico de los errores comunes").

La pre-comprobación tiene una race conocida y aceptada — un archivo que crece por encima del umbral entre el control y el envío se gestiona aun así con el fallback que clasifica los errores de stream sobre archivos por encima del umbral como TOO_LARGE.

Entradas no regulares (desde la 0.1.5): symlink, socket, FIFO y device se descartan durante el recorrido y no se envían a clamd.

`os.walk()` clasifica como "archivo" todo lo que no es un directorio, así que acababan en la lista a escanear con dos consecuencias: los symlink colgantes (frecuentísimos en node_modules y en los stores pnpm limpiados) producían cientos de falsos ERRORE "[Errno 2] File o directory non esistente", y `open()` sobre una FIFO sin escritor BLOQUEA indefinidamente en el kernel — ninguna excepción, ningún timeout: una sola FIFO en el árbol colgaba todo el escaneo.

No son huecos de cobertura: un symlink no tiene contenido propio (el target, si está en el árbol, se escanea como archivo real), y socket, FIFO y device no tienen contenido persistente infectable. Por eso no aparecen en los resultados ni entran en el invariante `scanned = limpios + infectados + errores + demasiado_grandes`; se cuentan aparte y se muestran en el resumen de la CLI. TOO_LARGE y los permisos denegados siguen siendo visibles precisamente porque son huecos reales.

La apertura usa `O_NOFOLLOW|O_NONBLOCK` con verificación `fstat()` sobre el descriptor ya abierto: el control del tipo se refiere exactamente al objeto que se va a leer, sin ventana TOCTOU respecto a la ruta.

Timeout (desde la 0.1.5): la espera del veredicto de clamd tiene un timeout propio (120s), distinto del de conexión y envío (30s). Son cosas distintas: mandar los bytes es rápido, pero el análisis de un archivo o de un PDF con muchos objetos anidados puede requerir mucho más. Con un valor único bajo esos archivos salían como "sesión clamd interrumpida: timed out" pese a ser perfectamente legibles.

Los timeouts dan derecho a un segundo intento sobre sesión limpia (solo los timeouts: sobre tubería rota significaría rechazo por StreamMaxLength y reintentar sería desperdicio); un archivo no verificado ni en el segundo intento tiene un mensaje distinto, porque ese sí es un hueco de cobertura. El valor debe mantenerse coherente con CommandReadTimeout/ReadTimeout en clamd.conf: si clamd se rinde antes, alargar el timeout lado cliente no sirve.

Si sospechas que un problema está ligado a la sesión persistente, `--no-persistent` vuelve al comportamiento "una conexión por archivo" para aislar el caso.

Al final del escaneo la CLI imprime un resumen de los errores agrupados por tipo (ej. "2 Permission denied", "1 Broken pipe"), en lugar de tener que hacer grep/sort/uniq -c a mano.

## GUI: panorámica de la interfaz

Aplicación PySide6 de ventana única con barra lateral y estilo inspirado en KDE Plasma (usa siempre los colores de la paleta del sistema, nunca colores hardcoded, salvo los estados semánticos como "infectado"). Ocho secciones:

- **Escaneo** — escaneo manual sobre una ruta a elegir, en un QThread separado (ScanWorker): la ventana permanece reactiva incluso en directorios grandes, y la barra de estado muestra el archivo en proceso en tiempo real (con elisión del texto para no hacer crecer el layout en rutas largas). Los contadores distinguen escaneados / infectados / errores / no verificados (demasiado grandes), y solo infectados/errores/no-verificados producen filas en la lista.
- **Suspender / Reanudar.** La pausa tiene granularidad de archivo completo: el archivo en streaming se completa, luego el worker se detiene (el botón sigue las señales del worker, no el clic, por lo que el estado mostrado es siempre el efectivo). "Interrumpir" permanece activo también en pausa. Tras una pausa más larga de ~25s (cerca del IdleTimeout de clamd, 30s por defecto) la sesión se recrea proactivamente al reanudar, para no producir un error falso sobre el primer archivo. Con la ventana minimizada en la bandeja, el tooltip del icono muestra los contadores del escaneo en curso y el estado de pausa.
- **Cuarentena manual por defecto.** La casilla "Poner en cuarentena automáticamente los archivos infectados" está desactivada por defecto: los archivos señalados quedan donde están y los mueves tú con "Poner en cuarentena los seleccionados" — cómodo para evaluar un falso positivo antes de mover algo. "Copiar log" copia al portapapeles todas las filas de la lista (infectados, errores, no verificados).
- **Historial** — registro persistente (~/.local/share/klamav-py/history.json, últimas 1000 entradas; el directorio es 0700 y los archivos 0600, porque listan rutas y archivos infectados) con fecha/hora, tipo, ruta, escaneados, infectados, errores, no verificados, carpetas no legibles, ya evaluados y fallos de E/S (un subconjunto de los errores), más el motivo de un escaneo no completado o pospuesto. Los escaneos programados indexan también el log detallado en disco (tooltip sobre la fila): el detalle de infectados/errores de un escaneo en segundo plano, que no pasa por ninguna lista de la UI, queda así siempre inspeccionable.
- **Cuarentena** — lee el mismo índice JSON usado por la CLI (index.json en el directorio de cuarentena): las dos herramientas son intercambiables sobre los mismos datos. Restauración en la posición original (con restauración de los permisos originales, rechazo explícito si la ruta ha sido reocupada mientras tanto) o eliminación definitiva.
- **Detecciones** — los archivos detectados pero dejados en su sitio (firmas heurísticas, archivos de correo) en los escaneos de la sesión, con "He verificado, no volver a señalar" (toma de conocimiento, ver "Detecciones no movidas y toma de conocimiento") y "Eliminar…" con confirmación; debajo, las tomas de conocimiento registradas, revocables.
- **Actualizaciones** — descarga las definiciones de virus lanzando `freshclam --stdout` mediante pkexec (autenticación PolicyKit), deteniendo el demonio clamav-freshclam/freshclam del sistema para evitar conflictos de lock sobre el archivo de log y reiniciándolo al final. Desde la 0.1.7 el reinicio se produce también si la actualización se interrumpe (logout, señal), y afecta solo a los servicios que estaban activos antes: un demonio detenido a propósito permanece detenido. Salida mostrada en tiempo real. Eligiendo "Salir" durante la actualización, la app se cierra en cuanto termina en lugar de interrumpirla. Por defecto arranca automáticamente 1.5s después del inicio de la app (desactivable en Ajustes).
- **Tiempo real** — registro de los escaneos automáticos sobre las carpetas monitorizadas (configurables en Ajustes). Usa QFileSystemWatcher: creación/modificación de un archivo → escaneo tras un debounce de 3s (para no escanear archivos aún en escritura), y los archivos infectados se ponen siempre en cuarentena automáticamente. Los archivos demasiado grandes se etiquetan "No verificado", no "Seguro" (ver elecciones de diseño).
- **Programación** — escaneo recurrente (cada N horas o días) sobre una carpeta a elegir, mediante QTimer interno: requiere la app en ejecución (aunque minimizada en la bandeja). Durante la ejecución la página muestra el estado y los contadores en vivo; el tooltip de la bandeja refleja el avance; al final del escaneo el log detallado se escribe en ~/.local/share/klamav-py/logs/scheduled-<timestamp>.log (rotación: últimas 10 ejecuciones) y se referencia en el Historial. No es un temporizador del sistema como el del paquete .deb, que funciona también con la GUI cerrada (ver "Escaneos programados: los dos mecanismos").
- **Ajustes** — socket de clamd, directorio de cuarentena, carpetas monitorizadas para el tiempo real (con indicador "Tiempo real activo en N carpetas (M subcarpetas incluidas, recursivo)"), inicio automático al login, arranque minimizado en bandeja, actualización de la BD al inicio, control de actualizaciones de la aplicación (botón y opción al inicio), integración con Dolphin.

## Monitorización en tiempo real: cómo funciona (0.1.5)

QFileSystemWatcher no es recursivo: observa exactamente las carpetas que se le pasan. Hasta la 0.1.4 esto significaba que los archivos creados en las SUBCARPETAS de una carpeta monitorizada no generaban ningún evento — la interfaz declaraba el tiempo real activo sobre una carpeta quedando ciega sobre todo lo que había debajo.

Desde la 0.1.5 las carpetas configuradas se expanden recursivamente (symlink no seguidos, carpetas ocultas saltadas porque son sobre todo cache que genera eventos continuamente). Tres vías mantienen actualizada la cobertura:

- subcarpetas creadas tras el arranque: enganchadas enseguida, al recibir el evento sobre la carpeta que las contiene;
- árboles movidos dentro con mv o extraídos de un archivo: mv genera un solo evento sobre el nivel superior, así que las subcarpetas que llegan ya llenas se recuperan mediante la reconciliación periódica (hasta 60s de latencia);
- watch perdidos (carpeta eliminada y recreada): misma reconciliación.

Los archivos ya presentes en una carpeta descubierta DESPUÉS del arranque se analizan, no se ponen en baseline: es el escenario del archivo extraído en otro sitio y luego movido dentro, en el que de otro modo el contenido nunca se escanearía. Al arrancar la aplicación el comportamiento es el opuesto y querido: los archivos ya presentes se convierten en el estado de referencia sin escanearse, de lo contrario cada reinicio re-escanearía toda la carpeta monitorizada.

Cada carpeta observada consume un watch inotify, y fs.inotify.max_user_watches es compartido con TODOS los procesos del usuario (IDE, sincronizadores cloud, indexadores). Por eso hay un techo (MAX_WATCH_DIRS, por defecto 8000): controla tu propio límite con `cat /proc/sys/fs/inotify/max_user_watches` y ajústalo en consecuencia.

Tanto el alcanzar el techo como los fallos de addPaths() se señalan explícitamente en el indicador de estado — un tiempo real que se declara activo mientras es ciego sobre la mitad de las carpetas es peor que uno apagado.

La versión del programa es visible en el título de la ventana, en el tooltip de reposo de la bandeja del sistema y en el panel Ajustes.

Todos los ajustes se guardan con QSettings (organización "KlamAV-Py", aplicación "KlamAV-Py" — en Linux típicamente ~/.config/KlamAV-Py/KlamAV-Py.conf). Los ajustes de versiones anteriores a la 0.1.3-2 (archivo ~/.config/KlamAV/KlamAV.conf) se migran automáticamente en el primer arranque; el viejo archivo queda en disco.

## Elecciones de diseño intencionales

Cuarentena manual en Escaneo, automática en Tiempo real. No es una incoherencia: las carpetas monitorizadas en Tiempo real son típicamente destinos de archivos recién descargados/recibidos desde fuera (p. ej. ~/Scaricati) — el riesgo de falso positivo sobre un archivo nuevo y nunca tocado es bajo, por lo que tiene sentido actuar de inmediato. La página Escaneo puede apuntar a carpetas con datos importantes, donde un falso positivo conviene verificarlo antes de mover algo.

Guardia "un escaneo a la vez" entre manual y programado. Un escaneo manual no arranca si hay uno programado en curso (y viceversa el programado se salta, con entrada explícita en el Historial y notificación, si hay uno manual activo). Motivos: contención sobre clamd (dos recorridos de toda la home en paralelo duplican E/S y memoria del demonio) y doble carga sobre el sistema de archivos. El Tiempo real está deliberadamente exento de la guardia: es la protección primaria, actúa sobre archivos individuales (segundos), y clamd gestiona conexiones concurrentes por diseño — suspenderlo mientras corre un manual sería peor. Un escaneo en pausa sigue "en curso" a efectos de la guardia: bloquea el programado hasta que se reanude o se interrumpa.

Exclusión del directorio de cuarentena. Los archivos en cuarentena son los datos gestionados por la aplicación: se excluyen del recorrido (pruning, ni siquiera se leen) y, como defensa en profundidad, quarantine_file() rechaza explícitamente volver a cuarentenar un archivo que ya está dentro del directorio de cuarentena. Sin estas guardias, un escaneo de toda la home detectaría los viejos EICAR en cuarentena en cada ciclo, inflando para siempre infectados y errores con "fantasmas" ya gestionados.

## Bandeja del sistema y single-instance

La app permanece activa en la bandeja del sistema incluso cerrando la ventana (la X la oculta, no la termina: se sale desde el menú de la bandeja o desde "Salir"). Está pensada para quedarse en segundo plano para programación y Tiempo real. Al salir, los hilos en curso (escaneos, ping a clamd, control de actualizaciones) se detienen y se esperan como máximo 3 segundos; los bloqueados en E/S más allá de ese límite no impiden el cierre.

Es además single-instance: si lanzas la GUI mientras otra instancia ya está activa, la segunda envía su propio `--scan-target` (si lo tiene) a la primera y termina enseguida, en lugar de abrir una segunda ventana. Es el mecanismo que hace sensata la integración con Dolphin: al hacer clic en "Escanea con KlamAV-Py" sobre varios archivos/carpetas, todas las peticiones acaban en la misma ventana ya abierta.

El socket IPC está en el directorio runtime del usuario (`$XDG_RUNTIME_DIR/klamav-py-ipc`, típicamente `/run/user/<uid>/`), no en `/tmp`, y antes de enviar cualquier dato la segunda instancia verifica con `SO_PEERCRED` que el proceso en escucha pertenezca al mismo usuario. Si no existe un directorio runtime seguro, o el servidor IPC no logra arrancar, la GUI arranca aun así sin single-instance y lo señala: un segundo arranque abriría otra copia.

Actualizando desde la 0.1.6 o anteriores, una instancia vieja aún abierta usa el viejo socket y no es reconocida por la nueva: ciérrala desde la bandeja antes de reiniciar la aplicación.

## Integración con Dolphin

Desde el panel Ajustes se puede instalar la entrada "Escanea con KlamAV-Py" en el menú contextual de Dolphin, escribiendo un archivo .desktop en ~/.local/share/kservices5/ServiceMenus/ y ~/.local/share/kio/servicemenus/ y regenerando la cache de servicios KDE (kbuildsycoca5/6). El Exec= invoca directamente el comando sin shell intermedia: un nombre de archivo con ", ` o $ no puede romper el quoting.

## Inicio automático

Si se habilita, escribe un archivo .desktop en ~/.config/autostart/ que lanza la GUI con el intérprete Python actualmente en uso; si se deshabilita, elimina el archivo.

## Control de actualizaciones de la aplicación

El botón "Comprobar actualizaciones" en Ajustes interroga la API de releases de GitHub del proyecto y compara la última versión publicada con la que se usa. La opción "Comprobar actualizaciones al inicio" está desactivada por defecto: sin ella la aplicación no contacta GitHub salvo petición explícita. Si hay una versión nueva aparece un enlace a la página de la release (y una notificación en la bandeja, para el control al inicio); la instalación sigue siendo manual, mediante .deb o AUR.

El control al inicio arranca como máximo una vez cada seis horas: la API de GitHub no autenticada concede 60 peticiones por hora por dirección IP, y reiniciar a menudo la aplicación no debe consumirlas. El botón en Ajustes no está sujeto al límite.

La respuesta de GitHub se trata como dato no confiable: se lee hasta un máximo de 256 KB, versión y notas se escapan antes de mostrarse, y el enlace se hace clickeable solo si apunta a las releases del repositorio oficial.

## Escaneos programados: los dos mecanismos

- **Temporizador de la GUI (página Programación):** corre solo con la app activa (aunque minimizada en la bandeja), pero ofrece progreso en vivo, log persistente e integración completa con la UI.
- **Unidad systemd de usuario (instalada por el .deb y por el paquete Arch):** arranca al login de cada usuario sin configuración, funciona con la GUI cerrada. Para que el temporizador corra también con el usuario desconectado:
  `loginctl enable-linger $USER`. Usa la misma ruta de cuarentena por defecto de la GUI (~/.local/share/klamav-py/quarantine). Errores y carpetas no legibles del último escaneo, uno por línea, están también en `~/.local/state/log/klamav-py/scan-errors.log` (`$XDG_STATE_HOME/log/klamav-py`, el `LogsDirectory=` de la unit), además del journal.
  La cuarentena, la conexión a clamd y las carpetas excluidas elegidas en la GUI llegan al temporizador mediante un drop-in, `~/.config/systemd/user/klamav-scan.service.d/50-klamav-py.conf`, reconocido por la primera línea `# Generado por KlamAV-Py: no modificar a mano.`. Lo escriben los guardados de Ajustes y Programación; al arrancar la GUI reescribe también un drop-in suyo que haya quedado en una versión anterior (por ejemplo sin `--log-errors`) y lo dice en la página Programación. Un archivo con ese nombre sin esa primera línea, es decir, escrito a mano, nunca se toca. Para ver el comando que usará el temporizador: `systemctl --user cat klamav-scan.service`.

Ya no se distribuye una unidad systemd de sistema (desde la 0.1.9): no podía leer las home privadas sin ampliar sus permisos, mientras que la unidad de usuario corre como el propio usuario y lee su home tal cual.

## Diagnóstico de los errores comunes

En un escaneo de toda la home de una máquina de desarrollo real (330k+ archivos) los errores residuales son fisiológicos y reconocibles:

- **"Tubería rota" sobre archivos grandes** — clamd ha rechazado el stream por StreamMaxLength (25MB por defecto en clamd.conf) cerrando la conexión a mitad de envío. Con la pre-comprobación dimensional activa casi ha desaparecido: los archivos por encima del umbral salen como "no verificados" sin tocar la sesión. Para elevar el límite:
  `StreamMaxLength 100M` en clamd.conf y reinicio de clamd — con el trade-off de que un límite más alto facilita saturar clamd con streams enormes.

  Nota de honestidad: incluso los archivos dentro del límite de stream siguen sujetos a MaxScanSize/MaxFileSize/MaxRecursion de clamd (defaults aproximados 100M/25M/16): un archivo comprimido voluminoso puede resultar "OK" con escaneo parcial — límite del propio clamd, no detectable por este proyecto.

- **Errno 13 "Permiso denegado"** — permisos reales del sistema de archivos (p. ej. archivos propiedad de contenedores Docker dentro de la home). No es un problema del proyecto.

- **Errno 2 "Archivo o directorio no existente"** — desde la 0.1.5 debería ser raro. Hasta la 0.1.4 la causa dominante NO era la documentada aquí (archivos desaparecidos durante el recorrido): eran symlink colgantes, que os.walk() clasifica como archivos y que open() no puede abrir. En árboles con node_modules o stores pnpm limpiados eran cientos de líneas que ocultaban los errores reales. Ahora los symlink se descartan aguas arriba. Lo que queda es la causa originalmente hipotetizada — archivos que de verdad desaparecieron entre el recorrido y la lectura (Trash vaciado, sync cloud, navegador que reescribe la cache) — y en esa forma es ruido esperado.

- **Errno 6 "Dispositivo o dirección no existente"** — sockets AF_UNIX (agent GPG, MEGAsync, native messaging host de los navegadores). Como arriba: descartados aguas arriba desde la 0.1.5, no deberían aparecer ya.

- **"timed out"** — también aquí la explicación hasta la 0.1.4 era incompleta. No solo latencia de mount de red: el timeout único de 30s cubría tanto el envío como la ESPERA del veredicto, y clamd puede tardar mucho más en archivos o PDF con muchos objetos anidados. Archivos locales perfectamente legibles resultaban "sesión clamd interrumpida: timed out" de forma reproducible. Desde la 0.1.5 la espera tiene un timeout propio (120s) y los timeouts tienen un segundo intento. Si aún ves alguno, comprueba CommandReadTimeout/ReadTimeout en clamd.conf antes de sospechar del sistema de archivos.

- **Errores en avalancha de un solo tipo (miles)** — casi con seguridad clamd muerto o reiniciado a mitad del escaneo:
  `journalctl -u clamav-daemon.service` en el intervalo del escaneo, y `dmesg | grep -i oom` para el caso OOM. El resumen por tipo de la CLI (o el log persistente del programado) hace este caso inmediatamente distinguible del ruido fisiológico.

## Fix de seguridad en la 0.1.8

- **Datos de la aplicación legibles por otros usuarios locales.** Historial, logs de los escaneos programados y logs de errores de la CLI se creaban con los permisos por defecto del umask (directorio 0755, archivos 0644): listan rutas de los archivos escaneados, nombres de las firmas y archivos infectados. En un sistema con home 0755 — default histórico, aún frecuente en instalaciones actualizadas — eran legibles por otro usuario local. Ahora el directorio de datos es 0700 (restringido al arrancar, por lo que también los archivos de versiones anteriores se vuelven inaccesibles) y los archivos 0600. El log de `--log-errors` era el peor caso, porque el README sugería su uso en /tmp, donde otro usuario podía pre-crearla para leer su contenido.

- **Archivo de configuración a 0644.** `~/.config/KlamAV-Py/KlamAV-Py.conf` lista las carpetas monitorizadas por el Tiempo real y las rutas de escaneo y cuarentena, pero lo escribe Qt y escapaba al tratamiento de los demás datos. Se lleva a 0600 al arrancar, junto con el archivo de configuración legacy de la 0.1.3 si aún existe.

- **Respuestas de clamd sin techo.** Las respuestas se acumulaban en memoria mientras el socket las proporcionara: un clamd remoto por TCP, o un socket en una ruta configurable por el usuario, podía agotar la RAM. La lectura se detiene ahora en 1 MiB. No afecta al clamd del sistema, que es de root.

## Fix de seguridad en la 0.1.7

- **Evasión de la cuarentena mediante hardlink + symlink.** La verificación de la identidad del inodo tras el traslado (introducida en la 0.1.4, ver más abajo) usaba `os.stat()`, que sigue los symlink. Un archivo infectado podía crearse un hardlink en otro sitio y sustituir su propia ruta por un symlink hacia ese hardlink: el traslado ponía en cuarentena solo el enlace, `os.stat()` reencontraba el inodo original y la verificación pasaba. El contenido infectado quedaba fuera y ejecutable, el índice registraba una cuarentena exitosa y "Eliminar definitivamente" eliminaba solo el enlace. La verificación ahora usa `os.lstat()` y los permisos se aplican con `fchmod()` sobre el descriptor ya verificado. Requiere ganar una carrera a un proceso del mismo usuario, pero es repetible en bucle.

- **Socket IPC single-instance en /tmp.** Hasta la 0.1.6 el socket era `/tmp/klamav_py_ipc`, en un directorio compartido por todos los usuarios. `UserAccessOption` impedía a otros conectarse al socket de la aplicación, pero no impedía a la aplicación conectarse al socket de otro: un segundo usuario local podía ocupar el nombre primero, recibir las rutas de los archivos enviados a escanear y, respondiendo a la conexión, hacer creer a cada arranque que ya había una instancia activa, impidiendo por completo el arranque de la GUI (nada de Tiempo real, nada de escaneos programados). Ahora el socket está en el directorio runtime del usuario, el cliente verifica el peer con `SO_PEERCRED` y el fallo de `listen()` se señala. No explotable en un escritorio con un solo usuario.

## Fix de seguridad en la 0.1.4

Esta versión resuelve una serie de problemas de seguridad surgidos de una auditoría del código. El modelo de amenaza de referencia es un sistema multi-usuario y, para la cuarentena, el propio archivo infectado tratado como contenido potencialmente hostil (no un atacante externo).

- **TOCTOU en cuarentena y restauración.** `quarantine_file()` abre el archivo con O_NOFOLLOW y verifica la identidad del inodo (dev+ino) tras el traslado, en lugar de confiar en un control is_file() separado de la operación de move: un archivo infectado podría intentar evadir el aislamiento sustituyéndose por un symlink en la ventana entre el control y el traslado. Los symlink (y los archivos especiales como las FIFO) se rechazan ahora explícitamente. `restore()` reclama el destino de forma atómica con O_CREAT|O_EXCL en lugar de un exists() seguido de move(), eliminando la ventana en la que otro proceso podía crear el archivo de destino en medio.

- **Socket IPC single-instance restringido y validado.** El QLocalServer usa UserAccessOption: sin él, en Linux los permisos del socket dependen del umask del proceso y podrían resultar accesibles a otros usuarios del sistema. La ruta recibida por el socket está ahora limitada en tamaño (evita payloads enormes pensados como DoS) y decodificada de forma robusta (un payload UTF-8 malformado se ignora, no propaga excepciones). Desde la 0.1.7 el socket ya no está en `/tmp`: ver la sección anterior.

- **Actualización de la base de datos delegada al servicio del sistema.** El botón "Actualizar Base de Datos" ejecuta solo `pkexec /usr/bin/systemctl restart clamav-freshclam.service`, con argv fijo y binarios verificados root-owned: freshclam descarga las firmas como usuario `clamav`, con el sandboxing de la unidad de la distribución, en lugar de correr como root. El resultado se verifica a partir de la versión del DB reportada por clamd; al arrancar la actualización solo parte si las firmas tienen más de 36 horas.

- **Regeneración de la cache de servicios KDE sin shell.** `os.system()` ha sido sustituido por `subprocess.run()` con argv explícito (sin shell).

- **Permisos de los archivos .desktop de la integración con Dolphin llevados de 0755 a 0644.**

  **ATENCIÓN** — este cambio era ERRÓNEO y se revocó en la 0.1.5. El razonamiento (los .desktop son archivos de configuración, el spec freedesktop no quiere el bit de ejecución) vale para los lanzadores en /usr/share/applications, NO para los servicemenu de KIO: desde KFrameworks 5.85 deben ser ejecutables, y con 0644 Dolphin responde "No estás autorizado a ejecutar este archivo" y la entrada de menú deja de funcionar. Las dos destinaciones tienen requisitos opuestos: el lanzador queda a 0644, el servicemenu vuelve a 0755. Se deja aquí documentado porque la justificación parece correcta leída por sí sola y es exactamente la forma en que la regresión puede reintroducirse.

Los fixes están cubiertos por 9 nuevos tests automáticos (rechazo de symlink y FIFO en cuarentena, verificación del reclamo atómico del destino en la restauración, validación del payload IPC).

## Nota sobre la 0.1.4-2 (corrección, no seguridad)

La raíz de cada escaneo se resuelve ahora (`Path.resolve()`) al inicio de `scan_stream`, en un único punto común a todos los modos de arrancar un escaneo (CLI, GUI manual, integración Dolphin/IPC, y en perspectiva Tiempo real y programado). No es un fix de seguridad: en el caso del IPC el socket ya está restringido al mismo usuario, por lo que no existe una frontera de privilegio que proteger y un symlink no da acceso a nada que el usuario no pueda ya leer. Es un fix de corrección: si la ruta a escanear es un symlink-directorio, `os.walk()` lo sigue de todos modos cuando es el punto de partida (followlinks=False bloquea solo los symlink internos al árbol, no la raíz), y sin resolverlo el informe de escaneo mostraría los resultados bajo la ruta del symlink en lugar de bajo la real efectivamente leída. Para un antivirus cuenta el contenido real controlado, no el alias por el que se llegó.

La resolución ocurre una sola vez sobre la raíz, no por cada archivo: `_iter_files` (el recorrido propiamente dicho) permanece inalterado, así que tanto el streaming a memoria constante como el pruning de los directorios excluidos (cuarentena incluida) siguen funcionando exactamente como antes. Los symlink internos al árbol siguen sin seguirse.

## Lo que falta respecto a KlamAV original (a propósito)

- **Sin escaneo on-access a nivel kernel.** La monitorización en Tiempo real se basa en QFileSystemWatcher (recursiva desde la 0.1.5, con unos segundos de latencia y un techo al número de carpetas observadas) — no es un sustituto de un verdadero on-access scanner. Para eso ClamAV ya proporciona clamonacc (basado en fanotify): no tiene sentido reimplementar un equivalente de Dazuko. Si se necesita on-access real, se configura mediante clamd.conf/clamav-clamonacc.service.
- **Sin integración mail client-side.** Si se necesita escaneo de correo, hoy tiene más sentido por el lado del servidor (milter) que por el lado del cliente KMail/Evolution como hacía klammail.

## Desarrollo y pruebas

```bash
cd klamav-py
python3 -m venv venv        # si no lo has creado ya
source venv/bin/activate
pip install -r requirements-dev.txt
python3 -m pytest tests/
```

Los tests cubren la lógica pura sin clamd real (parsing del protocolo clamd, gestión de cuarentena sobre filesystem temporal, CLI) y la GUI con instanciación offscreen de las páginas y simulación de señales/acciones (reanudación Escaneo↔Cuarentena, elisión de estado, portapapeles, pausa/reanudación con cliente falso inyectado mediante client_factory).

En distribuciones con Python "externally managed" (Debian/Ubuntu recientes, PEP 668), `pip install -r requirements-dev.txt` fuera de un venv falla con externally-managed-environment: comportamiento esperado del sistema, usa el venv.

## Licencia

KlamAV-Py se publica bajo licencia GPL-3.0-or-later (ver el archivo LICENSE). Código escrito desde cero: no contiene código proveniente de KlamAV 0.22, del que es solo un heredero espiritual de la idea.

## Empaquetado .deb

El esqueleto Debian está en debian/, probado con build real (dpkg-buildpackage) e instalación/desinstalación completas (dpkg -i / apt-get remove --purge).

Verifica antes de instalar: el esqueleto se validó en un entorno Ubuntu 24.04, donde python3-pyside6.qtcore y afines no existen en los repositorios (Ubuntu empaqueta solo PySide2). En Debian deberían existir, pero antes de instalar el .deb comprueba con `apt-cache search pyside6` que los paquetes listados en debian/control (python3-pyside6.qtcore, .qtgui, .qtwidgets, .qtnetwork) existan en tu versión, y corrige los nombres si difieren.

### Qué hace el esqueleto Debian

- Un único paquete binario klamav-py con CLI y GUI (sin split, mientras el proyecto es joven).
- `pyproject.toml` define dos entry point en /usr/bin/: klamav-py (CLI) y klamav-py-gui (GUI). La GUI los usa para auto-detectar el comando a escribir en los .desktop de autostart y Dolphin (`_gui_relaunch_command()`), en lugar de asumir un checkout local con venv.
- El campo license usa deliberadamente el viejo formato tabla (`{text = "..."}`) en lugar de la cadena SPDX: setuptools recientes deprecan el primero con un warning, pero setuptools viejos (verificado: 68.1.2, Ubuntu 24.04) rechazan el segundo con error fatal. Mejor un warning cosmético en todas partes que un build roto en algunos sistemas.
- Unidades systemd a nivel de USUARIO (systemd --user), no de sistema (por qué, ver abajo). Los archivos se llaman `debian/klamav-py.klamav-scan.user.service/.timer`: el segmento `.user.` en el nombre es obligatorio para que dh_installsystemduser los reconozca — descubierto solo probando empíricamente un build real. Acaban en /usr/lib/systemd/user/.
- postinst/postrm usan deb-systemd-helper --user: el symlink de habilitación va en /etc/systemd/user/timers.target.wants/ (vale para todos los usuarios) y el temporizador arranca en el próximo login de cada uno, sin comandos manuales.
- Una unidad de usuario no puede ordenarse de forma fiable respecto a una unidad de sistema como clamav-daemon.service (managers systemd separados, sin dependencias cross-manager): el .service no tiene After=/Requires= sobre clamd. En la práctica no es un problema: clamd arranca mucho antes del login del usuario.

## Por qué unidades de usuario y no de sistema

La primera versión usaba una unidad de sistema con DynamicUser=yes. Parecía limpia (nada de useradd manual), pero no aguanta el uso real:

- Las home nacen 0700/0750: un usuario dinámico no lee nada, y el escaneo de /home producía solo Permission denied en avalancha.
- La cuarentena acababa en /var/lib/klamav-py, propiedad del usuario dinámico: la GUI no podía ni verla ni gestionarla.

La unidad de usuario corre como el usuario, lee la home sin problemas y usa la misma cuarentena por defecto de la GUI: las rutas coinciden.

### Upgrade desde versiones anteriores (que tenían la unidad de sistema)

La eliminación del archivo de la vieja unidad no detiene una instancia ya activa — el symlink en /etc/systemd/system/timers.target.wants/ no está trazado por dpkg. Por eso postinst incluye una migración dedicada que detiene y deshabilita explícitamente la vieja unidad de sistema antes de habilitar la nueva de usuario (verificado leyendo el postinst del .deb compilado).

## Empaquetado Arch (AUR)

El PKGBUILD está en `arch/`, junto con el script `klamav-py.install` y `test-local.sh`.

El `source` apunta al tag de GitHub de la release. Atención a un detalle asimétrico: los tags del proyecto llevan el prefijo `v` (`v0.1.6`) pero el directorio dentro del archivo no (`KlamAV-Py-0.1.6`), de ahí el `v$pkgver` en la URL y el `$pkgver` en los `cd` de `build()` y `package()`. El archivo descargado se renombra a `$pkgname-$pkgver.tar.gz` para no colisionar en `SRCDEST` con archivos de otros paquetes.

`pkgver` debe actualizarse en cada release junto con `klamav_py/__init__.py`, `debian/changelog` y `CHANGELOG.md`; `tests/test_changelog.py` verifica el alineamiento y que los archivos instalados por `package()` existan de verdad. El `sha256sums` debe regenerarse en cada nuevo tag, con `updpkgsums` en una máquina Arch o a mano.

`arch/test-local.sh` permite validar el paquete en un contenedor Arch sin tener una máquina Arch:

```sh
arch/test-local.sh --tag      # build desde el tag de GitHub, PKGBUILD sin modificar
arch/test-local.sh            # build desde el árbol de trabajo actual
KLAMAV_TEST_GUI=1 arch/test-local.sh   # verifica también el arranque de la GUI
```

La modalidad `--tag` es la que debe usarse antes de publicar en AUR: es la única que verifica de verdad URL y checksum, porque las otras construyen desde un tarball local y parchean el PKGBUILD al vuelo.

## A revisar antes de una release estable

- **Versión:** `klamav_py/__init__.py` es la fuente de verdad. Desde la 0.1.5 pyproject.toml la deriva de `klamav_py.__version__` (`[tool.setuptools.dynamic]`) en lugar de redeclararla. Quedan por alinear a mano `debian/changelog` (con `dch`) y `CHANGELOG.md`, pero la coherencia se verifica con `tests/test_cli.py` y `tests/test_changelog.py`, que corren también en CI. El desalineamiento no era teórico, de dos maneras distintas: la versión en pyproject.toml se quedó fija en 0.1.4 mientras las otras fuentes ya estaban en 0.1.5, y `CHANGELOG.md` se quedó fijo en 0.1.3 durante tres releases, porque ningún paso del proceso de release lo tocaba.
- **Changelog:** `CHANGELOG.md` es la cronología para los usuarios, independiente de la distribución; `debian/changelog` contiene los metadatos del paquete Debian; `docs/CHANGELOG-archive.md` conserva el detalle extendido hasta la 0.1.3 y las rondas de auditoría. Tras `dch`, `tools/changelog-stub.sh` genera el borrador de la nueva entrada de `CHANGELOG.md` a partir de la recién escrita en `debian/changelog`, a acortar a mano.
- Ningún test automático verifica la instalación del .deb en sí (verificación manual): si el proyecto crece, vale un job CI con sbuild/pbuilder que lo reconstruya desde cero.

## Extensiones naturales

- **Circuit breaker sobre los resultados:** si clamd muere a mitad del escaneo, cada archivo restante produce un error "imposible abrir sesión" (el mecanismo detrás de un episodio observado de ~10.200 errores). Un contador de errores consecutivos por encima del umbral interrumpiría el escaneo con un fallo rápido y legible en lugar del flood.
- **Exclusiones configurables desde la UI** (hoy solo mediante `--exclude` CLI): cache, Trash, mounts de red — reduciría aún más el ruido y aceleraría los escaneos de toda la home.
- **Diferenciar el nombre del QLocalServer por sys.prefix:** en una máquina de desarrollo con checkout+venv y .deb instalados juntos, el instalado y el checkout no se disputarían la misma instancia single-instance.
- **Notificaciones de escritorio nativas** (notify-send o D-Bus directo) además de QSystemTrayIcon.showMessage, para escritorios con soporte de bandeja irregular.
- **Exportación del índice de cuarentena** en un formato legible por un frontend web/TUI (p. ej. para controlar la cuarentena desde el NAS).
- **Configurable desde la UI** si el Tiempo real debe poner en cuarentena automáticamente o solo señalar.

## Verificación de los releases

Desde la 0.1.10 los tags de los releases están firmados con la clave del proyecto:

    gradia (KlamAV-Py release signing) <gradia@disroot.org>
    Huella: EBEE 3E80 EFA3 8B42 B147  F1B9 9D7A A4F1 971F EAA9

```bash
gpg --keyserver hkps://keys.openpgp.org --recv-keys EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9
# o bien, si keys.openpgp.org no responde o no entrega la clave:
gpg --keyserver hkps://keyserver.ubuntu.com --recv-keys EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9
git clone https://github.com/gradia64/KlamAV-Py.git && cd KlamAV-Py
git tag -v v0.1.10
```

Sin keyserver, la clave pública está también en el repositorio:

```bash
gpg --import arch/klamav-py-release-key.asc
gpg --fingerprint EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9
```

En cualquier caso comprueba que la huella reportada por `gpg` coincida con
la indicada arriba: el keyserver (o el archivo en el repositorio) garantiza
solo entregar una clave, no que sea la correcta. Indica siempre el keyserver
con `--keyserver`: sin él, `gpg` usa el configurado, que puede no tener la
clave. Los tags anteriores a la 0.1.10 no están firmados.

Desde la 0.1.15 también los archivos adjuntos a la release (`.deb`, paquete Arch,
fuente) tienen una firma separada `.sig`, más `SHA256SUMS` firmado. Con la
clave importada como arriba:

```bash
gpg --verify SHA256SUMS.sig SHA256SUMS
sha256sum --check --ignore-missing SHA256SUMS
gpg --verify klamav-py_0.1.15-1_all.deb.sig klamav-py_0.1.15-1_all.deb
```

Los adjuntos están firmados por la CI con una subclave dedicada,
`F9F4 2835 8660 2F8D D724  C467 0888 61B0 4D8D 328D`; los tags los firma el
maintainer con `FDC2 2208 6F32 BADB 0403  4BAF 707F 1CD9 C887 FD2E`.
`gpg` atribuye ambas a la clave primaria indicada arriba. Si `gpg`
no reconoce la firma de un adjunto, tu copia de la clave es
anterior a la subclave de la CI: vuelve a descargarla del keyserver o del
repositorio como arriba.
