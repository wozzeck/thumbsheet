# Tests

- `test_generator.py <vídeo> [intervalos]` — sin pantalla. Genera para cada intervalo (por defecto 60 y 5),
  comprueba que no falta ninguna captura ni cambia el tamaño, y que los modos *seek* y *tramos*
  producen el mismo fotograma. Imprime tiempos. Usa un directorio temporal, no toca la caché real.
- `gui_smoke.sh <vídeo>` — abre la app en un Xvfb propio (no toca tu pantalla), mueve los sliders con
  xdotool y deja capturas en `/tmp/thumbsheet-gui/`. Requiere `xvfb-run`/`Xvfb`, `xdotool`, `import` (ImageMagick).
