#!/usr/bin/env bash
# Humo de la GUI en un Xvfb propio: abrir una CARPETA. Monta un árbol con copias de A (nombres y extensiones variados,
# en subcarpetas), ficheros que no son vídeo y uno oculto, arranca la app con la carpeta como único argumento y
# comprueba que entran sólo los soportados, en orden natural, y que se generan todos. Después arranca con una carpeta
# vacía: toast rojo «Sin vídeos soportados». Uso: tests/gui_folders.sh <vídeo A corto> [out]
set -euo pipefail
VIDEO_A="$1"; OUT="${2:-/tmp/thumbsheet-gui-folders}"; rm -rf "$OUT"; mkdir -p "$OUT/arbol/sub/hondo" "$OUT/arbol/.oculta" "$OUT/vacia"
DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
export DISPLAY=:72
export XDG_CACHE_HOME="$OUT/cache" XDG_CONFIG_HOME="$OUT/config" THUMBSHEET_AUDIO=0
for rel in peli10.mp4 peli2.MP4 sub/hondo/clip.mkv .oculto.mp4 .oculta/g.mp4; do cp -f "$VIDEO_A" "$OUT/arbol/$rel"; done
echo "no es un vídeo" > "$OUT/arbol/notas.txt"; echo "x" > "$OUT/arbol/sub/foto.jpg"
Xvfb :72 -screen 0 1400x900x24 -nolisten tcp >/dev/null 2>&1 &
XPID=$!
trap 'kill $APP 2>/dev/null || true; kill $XPID 2>/dev/null || true' EXIT
sleep 1
THUMBSHEET_DEBUG=1 python3 "$DIR/thumbsheet.py" "$OUT/arbol" >"$OUT/app.log" 2>&1 &
APP=$!
for i in $(seq 1 40); do WID=$(xdotool search --onlyvisible --classname thumbsheet 2>/dev/null | head -1 || true); [[ -n "$WID" ]] && break; sleep 0.25; done
[[ -n "${WID:-}" ]] || { echo "no aparece la ventana"; cat "$OUT/app.log"; exit 1; }
xdotool windowsize "$WID" 1400 900; xdotool windowmove "$WID" 0 0; sleep 0.3
fail=0; ok() { echo "ok   $1"; }; ko() { echo "FAIL $1"; fail=1; }
for i in $(seq 1 40); do grep -q "carpetas: [0-9]* vídeos" "$OUT/app.log" && break; sleep 0.25; done
C=$(grep "carpetas: [0-9]* vídeos" "$OUT/app.log" | tail -1 | sed 's/.*carpetas: //')
[[ "$C" == "3 vídeos"*": peli2.MP4, peli10.mp4, sub/hondo/clip.mkv" ]] && ok "carpeta: sólo los 3 soportados, sin ocultos, en orden natural ($C)" || ko "carpeta: '$C'"
sleep 0.5; grep -q "archivos: 3" "$OUT/app.log" && ok "cabecera del panel: 3 archivos" || ko "cabecera: $(grep -o 'archivos: [0-9]*' "$OUT/app.log" | tail -1)"
# («Buscando vídeos en la carpeta…» sólo llega a verse si el recorrido tarda más que el retardo de la barra: en local no)
grep -q "barra: Analizando [0-9] de 3 archivos" "$OUT/app.log" && ok "barra: «Analizando k de 3 archivos…» durante el sondeo ($(grep -o 'Analizando [0-9] de 3' "$OUT/app.log" | tr '\n' ' '))" || ko "barra sin «Analizando…»: $(grep 'barra:' "$OUT/app.log" | head -4 | tr '\n' ' ')"
for i in $(seq 1 120); do [[ "$(grep -c "fin: " "$OUT/app.log" || true)" -ge 3 ]] && break; sleep 0.5; done
[[ "$(grep -c "fin: " "$OUT/app.log" || true)" -ge 3 ]] && ok "los tres se generan (el actual y dos en segundo plano)" || ko "generados: $(grep -c 'fin: ' "$OUT/app.log" || true) de 3"
grep -q "Traceback" "$OUT/app.log" && ko "traceback en el log" || ok "sin tracebacks"
sleep 0.5; import -window root "$OUT/1-carpeta.png"
xdotool key ctrl+q; sleep 1
if kill -0 $APP 2>/dev/null; then ko "la app no cerró con Ctrl+Q"; kill $APP; sleep 1; else ok "cierre con Ctrl+Q"; fi
# carpeta sin vídeos: la app arranca vacía y avisa
THUMBSHEET_DEBUG=1 python3 "$DIR/thumbsheet.py" "$OUT/vacia" >"$OUT/app2.log" 2>&1 &
APP=$!
for i in $(seq 1 40); do grep -q "toast error\|carpetas: 0" "$OUT/app2.log" && break; sleep 0.25; done
sleep 0.5; grep -q "toast error: Sin vídeos soportados en vacia" "$OUT/app2.log" && ok "carpeta vacía: toast rojo «Sin vídeos soportados en vacia»" || ko "carpeta vacía: $(grep 'toast\|carpetas' "$OUT/app2.log" | tail -2 | tr '\n' ' ')"
grep -q "archivos: 0" "$OUT/app2.log" && ok "carpeta vacía: cabecera «Sin archivos»" || ko "carpeta vacía: cabecera $(grep -o 'archivos: [0-9]*' "$OUT/app2.log" | tail -1)"
xdotool key ctrl+q; sleep 1; kill $APP 2>/dev/null || true
left=$(pgrep -c -x ffmpeg || true); [[ "$left" == 0 ]] && ok "sin ffmpeg tras cerrar" || ko "ffmpeg vivos tras cerrar: $left"
[[ -f "$VIDEO_A" ]] || { echo "¡¡el vídeo ORIGINAL ha desaparecido!!"; fail=1; }
[[ $fail == 0 ]] && echo "TODO OK" || { echo "HAY FALLOS"; exit 1; }
