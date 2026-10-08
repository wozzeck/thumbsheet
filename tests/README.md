# Tests

- `test_generator.py <vídeo> [intervalos]` — sin pantalla. Genera para cada intervalo (por defecto 60 y 5),
  comprueba que no falta ninguna captura ni cambia el tamaño, y que los modos *seek* y *tramos*
  producen el mismo fotograma. Imprime tiempos. Usa un directorio temporal, no toca la caché real.
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
- `gui_smoke.sh <vídeo A> <vídeo B> [out]` — abre la app en un Xvfb propio (no toca tu pantalla) con
  COPIAS de A y de B (las dos se borran durante la prueba; los originales no se tocan), y con xdotool: rueda sobre el slider de intervalo, clic y arrastre de
  teselas, botón LLC (verifica el proyecto), Cortar, cambio de vídeo, Escape, Cortar y borrar original
  (doble pulsación: comprueba que una sola o una tardía no borran), Eliminar con confirmación, Ctrl+Q, y
  SIGKILL en plena generación. Deja capturas PNG en `out`. Requiere `Xvfb`,
  `xdotool`, `import` (ImageMagick). Caché y ajustes van aislados en `out`.
