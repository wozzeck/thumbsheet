# Tests

- `test_generator.py <vídeo> [intervalos]` — sin pantalla. Genera para cada intervalo (por defecto 60 y 5),
  comprueba que no falta ninguna captura ni cambia el tamaño, y que los modos *seek* y *tramos*
  producen el mismo fotograma. Imprime tiempos. Usa un directorio temporal, no toca la caché real.
- `test_llc.py` — sin pantalla, instantáneo: segmentos a partir de la selección, fichero `-proj.llc`
  y ajuste del intervalo a múltiplos de 5.
- `gui_smoke.sh <vídeo A> <vídeo B> [out]` — abre la app en un Xvfb propio (no toca tu pantalla) con
  una COPIA de A y con B, y con xdotool: rueda sobre el slider de intervalo, clic y arrastre de
  teselas, botón LLC (verifica el proyecto), Cortar, cambio de vídeo, Escape, Cortar y borrar original
  (doble pulsación: comprueba que una sola o una tardía no borran), Eliminar con confirmación, Ctrl+Q, y
  SIGKILL en plena generación. Deja capturas PNG en `out`. Requiere `Xvfb`,
  `xdotool`, `import` (ImageMagick). Caché y ajustes van aislados en `out`.
