# thumbsheet

Sábana de miniaturas de un vídeo para Linux (GTK3). Abres un vídeo y aparece un mosaico con una
captura cada N segundos. Dos controles: **Intervalo** (5–300 s) y **Tamaño** de las teselas.

```
thumbsheet vídeo.mp4        # o doble clic / "Abrir con" desde el gestor de archivos
thumbsheet                  # sin argumento: diálogo para elegir el vídeo
```

Atajos: `Ctrl+rueda` o `Ctrl +/-` cambian el tamaño de tesela; `Esc` / `Ctrl+Q` cierran.

## Instalación (Ubuntu / Mint / Debian)

```
sudo apt install ffmpeg python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0
git clone https://github.com/wozzeck/thumbsheet.git ~/ws/thumbsheet
~/ws/thumbsheet/install.sh      # enlaza ~/.local/bin/thumbsheet y registra la entrada de menú (sin sudo)
```

Actualizar: `git -C ~/ws/thumbsheet pull` (el symlink y la entrada de menú apuntan a la carpeta, no hay más).

Opcional, para decodificar por GPU (se usa sola si funciona): `intel-media-va-driver` (Intel
Broadwell+), `i965-va-driver` (Intel más antiguos) o `mesa-va-drivers` (AMD).

## Cómo va de rápido (y por qué no atasca el equipo)

- **ffmpeg hace el trabajo** en procesos con `nice 10` + `ionice` best-effort: aprovecha toda la CPU
  libre pero cede ante el escritorio y cualquier otra cosa interactiva. Al cerrar la ventana o mover
  el slider de intervalo se matan los ffmpeg en marcha.
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

Medido en un i7-13700H con una grabación de Teams de 24 min (1080p, 16 fps, GOP 6 s):
intervalo 60 s → 24 capturas en 0,4 s; intervalo 5 s → 287 capturas en ~10 s.

## Variables de entorno

| Variable | Efecto |
|---|---|
| `THUMBSHEET_HWACCEL=0` | no usar GPU |
| `THUMBSHEET_THUMB_PX=320` | lado mayor de la miniatura guardada (por defecto 480; cambia la huella de caché) |
| `THUMBSHEET_PIX_MB=32` | presupuesto de la caché de miniaturas decodificadas en memoria |
| `THUMBSHEET_DEBUG=1` | traza en stderr: plan elegido, GPU, tiempos |

Ajustes (intervalo, tamaño, ventana) se guardan en `~/.config/thumbsheet/settings.json`.

## Estructura

- `thumbsheet.py` — todo: sondeo (`VideoInfo`), planificador y workers (`Generator`), GPU (`Gpu`),
  caché/cargador (`PixCache`, `Loader`), mosaico (`Sheet`), ventana (`ThumbSheet`).
- `thumbsheet` — lanzador; `thumbsheet.desktop` + `icon.svg` — entrada de menú; `install.sh`.
- `tests/` — comprobaciones sin pantalla del generador (ver `tests/README.md`).
