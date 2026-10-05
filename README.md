# thumbsheet

Sábana de miniaturas de vídeo para Linux (GTK3). Abres uno o varios vídeos y aparece un mosaico con
una captura cada N segundos. Controles: **Intervalo** (5–300 s), **Tamaño** de las teselas, y a la
derecha del contador los botones **LLC** y **Eliminar**.

```
thumbsheet vídeo.mp4 [otro.mkv ...]   # o "Abrir con" desde el gestor de archivos (admite varios)
thumbsheet                            # sin argumentos: diálogo para elegir vídeos
```

- **Varios vídeos**: se listan en el panel izquierdo (nombre, duración y resolución); clic para ver
  cada sábana. Se añaden más con `Ctrl+O` o arrastrándolos a la ventana.
- **Selección**: clic = alternar una tesela; clic y arrastrar = aplicar a un rango contiguo. Las
  seleccionadas se recuadran en rojo. `Esc` deselecciona todo. Lo que se guarda son **segmentos de
  tiempo**, no teselas: una tesela está marcada si su tramo `[t, t+intervalo)` cae dentro de un
  segmento. Por eso al cambiar el intervalo la selección no se trocea: al afinarlo (10 → 5 s) las
  teselas intermedias aparecen marcadas; al engrosarlo (5 → 10 s) se ven marcadas las teselas que tocan
  el segmento (15–25 s muestra 10 y 20), pero el segmento guardado no cambia y al volver a la rejilla
  fina recupera sus bordes. El LLC exporta siempre los segmentos guardados, los precisos. Marcar o
  desmarcar una tesela suma o resta su tramo. Se recuerda por vídeo entre sesiones (en la caché).
- **Clic derecho** sobre una tesela: vista ampliada a toda la ventana. Sale al instante la miniatura
  ampliada y en una fracción de segundo la sustituye el fotograma a resolución nativa, que ffmpeg
  extrae aparte y queda en la caché (`full/`). Flechas `←`/`→` pasan al fotograma anterior/siguiente
  (los vecinos se precargan), `Inicio`/`Fin` van al primero/último. Se cierra con clic o `Esc`.
- **LLC**: guarda un proyecto de LosslessCut `<vídeo>-proj.llc` junto al vídeo, con un segmento por
  cada racha de teselas seleccionadas (del instante de la primera al de la última más el intervalo).
  Al abrir el vídeo en LosslessCut, los segmentos aparecen ya cargados. El fichero es JSON, válido
  para el LosslessCut actual (JSON5, esquema v2) y para versiones antiguas (YAML).
- **Eliminar**: borra el archivo de vídeo del disco directamente, sin papelera, tras confirmar.

El intervalo va **de 5 en 5** (5, 10, 15 … 300). Así todas las capturas caen en segundos múltiplos
de 5 y se conservan al cambiar el intervalo: pasar de 30 s a 60 s no genera nada, y de 30 s a 10 s
sólo genera los dos tercios que faltan. Lo ya generado no se tira nunca (vive en la caché de disco).

Atajos: la rueda sobre el slider de intervalo mueve ±5 s y sobre el de tamaño ±16 px; `Ctrl+rueda`
sobre el mosaico o `Ctrl +/-` cambian el tamaño de tesela; `Esc` deselecciona; `Ctrl+Q` cierra.

## Instalación (Ubuntu / Mint / Debian)

```
sudo apt install ffmpeg python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0
git clone https://github.com/wozzeck/thumbsheet.git ~/ws/thumbsheet
~/ws/thumbsheet/install.sh      # enlaza ~/.local/bin/thumbsheet y registra la entrada de menú (sin sudo)
```

Actualizar: `git -C ~/ws/thumbsheet pull`; si ha cambiado `thumbsheet.desktop`, vuelve a ejecutar
`install.sh` (la entrada de menú instalada es una copia con las rutas resueltas).

Opcional, para decodificar por GPU (se usa sola si funciona): `intel-media-va-driver` (Intel
Broadwell+), `i965-va-driver` (Intel más antiguos) o `mesa-va-drivers` (AMD).

## Cómo va de rápido (y por qué no atasca el equipo)

- **ffmpeg hace el trabajo** en procesos con `nice 10` + `ionice` best-effort: aprovecha toda la CPU
  libre pero cede ante el escritorio y cualquier otra cosa interactiva. Al cerrar la ventana o mover
  el slider de intervalo se matan los ffmpeg en marcha (y mueren con la app aunque la maten a ella).
- **Un worker por núcleo físico menos uno**, con tope por memoria disponible (~70 MB por ffmpeg).
  Medido: con 4 workers el paralelo ya satura la memoria/caché de la máquina; 19 workers sólo rascan
  un 15 % más de velocidad a cambio del triple de CPU y RAM. `THUMBSHEET_WORKERS=n` lo fuerza.
- **`OMP_NUM_THREADS=1` para cada ffmpeg.** El ffmpeg de Ubuntu arrastra un pool OpenMP de un hilo
  por núcleo que gira en `sched_yield` mientras el proceso vive, aunque se pida `-threads 1`:
  una captura de 0,1 s de trabajo real costaba 1,5 s de CPU (medido con `strace -c`: 20.000
  `sched_yield`). Con la variable, 0,13 s. No cambia el tiempo de reloj de una captura suelta, pero
  deja de calentar el equipo y de pisar a los demás workers.
- La **resolución de la miniatura no influye** en el tiempo (480 px y 160 px cuestan lo mismo):
  manda decodificar el vídeo a su resolución nativa, que no se puede evitar.
- **Dos estrategias según el vídeo**, elegidas midiendo el intervalo entre keyframes (GOP) con
  ffprobe sin decodificar nada:
  - *seek*: una búsqueda exacta por captura (`-ss` antes de `-i`), en paralelo, con `-threads 1` por
    proceso. Decodifica sólo desde el keyframe anterior hasta el instante pedido. Es la opción
    cuando el intervalo es grande respecto al GOP (lo normal con 30–300 s).
  - *tramos*: decodificación continua por trozos con `fps=1/N:round=up` cuando el intervalo es tan
    pequeño que el seek repetiría trabajo. Cada captura se publica en cuanto ffmpeg la escribe.
    En este modo se suman dos workers **VAAPI** si hay GPU, tras un autotest que compara un
    fotograma GPU/CPU (si el driver miente, se descarta sin ruido).
- Decodificador software con `-skip_frame noref -skip_loop_filter all`: sin B-frames ni desbloqueo,
  invisible a tamaño de miniatura y un 20–30 % más barato.
- **Caché en disco** en `~/.cache/thumbsheet/<huella>/<segundo>.jpg` (miniaturas de 480 px de lado
  mayor, ~25 KB). Cambiar el intervalo reutiliza las capturas que coincidan (de 30 s a 10 s, un tercio
  ya está) y volver a abrir el vídeo es instantáneo. La huella es ruta+tamaño+mtime.
- **Mosaico virtual** (DrawingArea + cairo): sólo se decodifican las miniaturas visibles (más una
  pantalla por delante y por detrás), en un hilo aparte, al tamaño exacto de la tesela (libjpeg
  escala por DCT, es casi gratis) y con una caché LRU acotada (64 MB por defecto). Mover el slider
  de tamaño reescala con cairo lo que ya está en memoria y, al soltarlo, redecodifica al tamaño
  final. Sin WebKit ni proceso web.

Medido en un i7-13700H (portátil) con una grabación de Teams de 24 min (1080p, 16 fps, GOP 6 s):
intervalo 60 s → 24 capturas en 0,4 s; intervalo 5 s → 287 capturas en 11–12 s con 4 a 13 workers.
Una captura suelta cuesta 0,12 s de reloj y 0,13 s de CPU; la GPU por captura cuesta 0,23 s (la
inicialización de VAAPI), por eso sólo se usa en el modo tramos.

## Variables de entorno

| Variable | Efecto |
|---|---|
| `THUMBSHEET_HWACCEL=0` | no usar GPU |
| `THUMBSHEET_WORKERS=4` | número de ffmpeg en paralelo (por defecto: núcleos físicos − 1, con tope por RAM) |
| `THUMBSHEET_THUMB_PX=320` | lado mayor de la miniatura guardada (por defecto 480; cambia la huella de caché) |
| `THUMBSHEET_PIX_MB=32` | presupuesto de la caché de miniaturas decodificadas en memoria |
| `THUMBSHEET_DEBUG=1` | traza en stderr: plan elegido, GPU, tiempos |

Ajustes (intervalo, tamaño, ventana) se guardan en `~/.config/thumbsheet/settings.json`.

## Estructura

- `thumbsheet.py` — todo: sondeo (`VideoInfo`), planificador y workers (`Generator`), GPU (`Gpu`),
  caché/cargador (`PixCache`, `Loader`), mosaico y selección (`Sheet`), segmentos y proyecto LLC
  (`selection_segments`, `write_llc_project`), vídeo abierto (`Document`), ventana (`ThumbSheet`).
- `thumbsheet` — lanzador; `thumbsheet.desktop` + `icon.svg` — entrada de menú; `install.sh`.
- `tests/` — comprobaciones sin pantalla del generador (ver `tests/README.md`).
