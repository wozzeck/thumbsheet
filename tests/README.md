# Tests

- `test_generator.py <vídeo> [intervalos]` — sin pantalla. Genera para cada intervalo (por defecto 60 y 5),
  comprueba que no falta ninguna captura ni cambia el tamaño, y que los modos *seek* y *tramos*
  producen el mismo fotograma. Imprime tiempos. Usa un directorio temporal, no toca la caché real.
- `test_cut_order.py` — sin pantalla, unos segundos: el corte respeta el orden del panel de segmentos. Construye un
  vídeo de 6 s con un color por segundo y keyframe cada segundo, comprueba que `expand_to_keyframes` mantiene el
  orden pedido (fundiendo sólo tramos consecutivos que se solapan en el tiempo) y corta de verdad en los dos modos
  (copia de streams y exacto) mirando el color de cada tramo del resultado.
- `test_snap.py` — sin pantalla, instantáneo: paradas del «Snap scroll» (funciones `snap_*`): la parada más cercana,
  la última necesaria para ver el final y el relleno que la convierte en tope, los pasos de rueda, filas de altura
  propia (vídeos que cambian de orientación) y filas más altas que el visor (el final también es parada).
- `test_llc.py` — sin pantalla, instantáneo: segmentos a partir de la selección, fichero `-proj.llc`
  y ajuste del intervalo a múltiplos de 5.
- `test_damaged.py <vídeo mp4> [fracción]` — sin pantalla. Trunca una copia del vídeo (por defecto a la
  mitad) y comprueba, en modo seek y en modo tramos, que las capturas imposibles quedan marcadas
  (`<t>.fail`), forman un sufijo coherente con el corte, el documento cuenta como completo y una
  segunda pasada no las reintenta. También prueba `eta_text`. El vídeo debe tener la cabecera `moov`
  al principio (faststart), como los de WhatsApp o Teams.
- `test_orientation.py <clip horizontal>` — sin pantalla. Construye un vídeo que pasa de horizontal a
  vertical y vuelve (concat de un clip y su transpuesto) y comprueba, en modo seek y en modo tramos, que
  cada miniatura sale con su proporción real sin bandas, que el generador avisa de las verticales
  (`on_aspect`) y que quedan apuntadas en `aspects.json`.
- `test_delete.py <vídeo>` — sin ventana: `remove_with_retry` reintenta ante «ocupado» y `Generator.quiesce`
  deja sin ffmpeg vivos a un vídeo cancelado antes de borrarlo (carpetas remotas: sftp por gvfs, sshfs).
- `test_errors.py <vídeo mp4 faststart>` — sin ventana. Vídeos irreproducibles: un fichero de texto con
  extensión .mp4 (mensaje de una línea), `ensure_info` que no deja escapar excepciones, una copia con los
  datos a ceros (ffprobe la acepta; el generador desiste en segundos sin dejar marcas, en seek y en tramos,
  y reintenta en la siguiente pasada), un vídeo dañado sólo al principio (no desiste) y la carrera con la
  GPU (fallo en los últimos tramos con la CPU ya terminada: el generador tiene que acabar igualmente).
- `test_folders.py` — sin ventana, instantáneo: `find_videos` recorre carpetas y subcarpetas y devuelve sólo los
  ficheros soportados (por extensión, mayúsculas incluidas), sin ocultos, sin seguir enlaces a carpetas, en
  orden natural por ruta; varias carpetas a la vez; carpeta inexistente.
- `gui_folders.sh <vídeo A corto> [out]` — Xvfb propio (`:72`). Arranca la app con una CARPETA como único argumento
  (copias de A con distintos nombres y extensiones en subcarpetas, más ficheros que no son vídeo y uno oculto) y
  comprueba que entran sólo los soportados, en orden natural, y que se generan.
- `test_loader.py` — sin ventana, instantáneo: el cargador de miniaturas atiende lo visible antes que la precarga
  (y lo visible de arriba abajo), descarta lo que ya no está a la vista ni en el margen, y una petición visible
  saca la tesela de la cola de precarga.
- `bench_scrollfill.sh <vídeo largo> [out]` — banco, no pasa/falla (Xvfb `:74`). Con una sábana de miles de
  teselas en caché (la primera vez la genera, intervalo 1 s), salta por la barra y mide cuánto tarda en
  completarse la pantalla (trazas `salto:` / `lleno:`).
- `gui_keys.sh <vídeo A de unos minutos> [out]` — Xvfb propio (`:70`), tres copias de A. Zoom con la rueda:
  `Ctrl+rueda` cambia las teselas por fila y no el intervalo, `Ctrl+Mayús+rueda` cambia el intervalo (arriba
  acerca, abajo aleja) y no el tamaño, la rueda sola no cambia ninguno. Teclas: `C` sin segmentos no abre nada;
  con uno abre el diálogo de corte, `Esc` lo cierra sin tocar nada y `C` + `Intro` corta y borra el original (en
  modo exacto, elegido con la flecha abajo, para que un segundo `Intro` llegue con el corte en marcha y se compruebe
  que no lo cancela); `Ctrl+C` no abre el diálogo; `Supr` abre Eliminar, `Esc` no borra y `Supr` + `Intro` borra.
- `gui_snap.sh <vídeo A de unos minutos> [out]` — Xvfb propio (`:69`), una copia de A a 10 s. La barra ya no tiene LLC
  y sí el toggle; sin snap la rueda va por píxeles; al activarlo salta a la fila entera más cercana y gana el relleno
  final (menos de una fila); con snap, la rueda y el arrastre de la barra de scroll sólo se paran en filas enteras,
  al final se ve la última fila y la rejilla sigue alineada, y Ctrl+rueda recentra en una parada de la rejilla nueva;
  el toggle se guarda y al desactivarlo la rueda vuelve a ir por píxeles.
- `gui_full.sh <vídeo A corto> [out]` — Xvfb propio (`:71`). Botón Full con dos copias de A: Esc en el diálogo no
  toca nada; Intro renombra a `-cortado.<ext>` sin cambiar el contenido (md5), quita el vídeo de la lista, pasa al
  siguiente, toast verde, mueve la caché a la clave del nombre nuevo y al reabrir el renombrado no regenera.
- `gui_errors.sh <vídeo A corto> <vídeo L mp4 faststart> [out]` — Xvfb propio (`:76`). Abre A, una copia de
  L con los datos a ceros, un texto con extensión .mp4 y otra copia de A: aspa roja en los dos rotos, el
  segundo plano pasa de largo y genera la última copia, sin marcas `.fail`; al pinchar en el vídeo a ceros
  se reintenta, vuelve a error con toast rojo y la cola no se queda esperando.
- `gui_segdrag.sh <vídeo A corto> [out]` — Xvfb propio (`:75`). Reorden en vivo del panel de segmentos: tres
  tramos sueltos; arrastrar la 3.ª tarjeta hasta la 1.ª (a medio arrastre ya está colocada delante, el icono
  es la tarjeta entera, las demás se han deslizado con animación y no se ha guardado nada), soltar; arrastrar
  la 1.ª al hueco bajo la última; arrastrar y cancelar con Esc; clic en una tarjeta (ir al instante) y ×.
  También comprueba que la fila del panel de vídeos acaba con el tamaño del fichero.
- `gui_smoke.sh <vídeo A> <vídeo B> [out]` — abre la app en un Xvfb propio (no toca tu pantalla) con
  COPIAS de A y de B (las dos se borran durante la prueba; los originales no se tocan), y con xdotool: rueda sobre el slider de intervalo, clic y arrastre de
  teselas, un corte que falla a propósito (toast rojo; Esc lo cierra), Cortar, cambio de vídeo, Escape, Cortar y borrar original
  (doble pulsación: comprueba que una sola o una tardía no borran), Eliminar con confirmación, Ctrl+Q, y
  SIGKILL en plena generación. Deja capturas PNG en `out`. Requiere `Xvfb`,
  `xdotool`, `import` (ImageMagick). Caché y ajustes van aislados en `out`.
