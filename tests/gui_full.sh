#!/usr/bin/env bash
# Humo de la GUI en un Xvfb propio: botón Full. Abre dos COPIAS de A; en la primera pulsa Full: Esc en el diálogo no
# toca nada; Intro renombra el fichero a <nombre>-cortado.<ext> sin cambiar el contenido, lo quita de la lista, pasa
# al siguiente, mueve la caché de miniaturas a la clave del nombre nuevo (reabrirlo no regenera) y avisa con toast
# verde. Uso: tests/gui_full.sh <vídeo A corto> [out]
set -euo pipefail
VIDEO_A="$1"; OUT="${2:-/tmp/thumbsheet-gui-full}"; rm -rf "$OUT"; mkdir -p "$OUT/video"
DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
export DISPLAY=:71
export XDG_CACHE_HOME="$OUT/cache" XDG_CONFIG_HOME="$OUT/config" THUMBSHEET_AUDIO=0
A1="$OUT/video/copia-$(basename "$VIDEO_A")"; A2="$OUT/video/otra-$(basename "$VIDEO_A")"; cp -f "$VIDEO_A" "$A1"; cp -f "$VIDEO_A" "$A2"
EXT="${A1##*.}"; A1CUT="${A1%.*}-cortado.$EXT"
Xvfb :71 -screen 0 1400x900x24 -nolisten tcp >/dev/null 2>&1 &
XPID=$!
trap 'kill $APP 2>/dev/null || true; kill $XPID 2>/dev/null || true' EXIT
sleep 1
THUMBSHEET_DEBUG=1 python3 "$DIR/thumbsheet.py" "$A1" "$A2" >"$OUT/app.log" 2>&1 &
APP=$!
for i in $(seq 1 40); do WID=$(xdotool search --onlyvisible --classname thumbsheet 2>/dev/null | head -1 || true); [[ -n "$WID" ]] && break; sleep 0.25; done
[[ -n "${WID:-}" ]] || { echo "no aparece la ventana"; cat "$OUT/app.log"; exit 1; }
xdotool windowsize "$WID" 1400 900; xdotool windowmove "$WID" 0 0; sleep 0.3
eval "$(xdotool getwindowgeometry --shell "$WID")"
fail=0; ok() { echo "ok   $1"; }; ko() { echo "FAIL $1"; fail=1; }
shown() { grep "\] vídeo: " "$OUT/app.log" | tail -1 | sed 's/.*vídeo: //; s/ [0-9]*x[0-9]* .*//'; }
for i in $(seq 1 60); do [[ "$(grep -c "fin: " "$OUT/app.log" || true)" -ge 2 ]] && break; sleep 0.5; done   # los dos en caché
sleep 0.5
G=$(grep "geometry:" "$OUT/app.log" | tail -1); gv() { echo "$G" | sed -E "s/.* $1=([^ ]+).*/\1/"; }
FX=$(gv full | cut -d, -f1); FY=$(gv full | cut -d, -f2); CX=$(gv cut | cut -d, -f1); LX=$(gv llc | cut -d, -f1)
[[ "$FX" -gt "$CX" && "$FX" -lt "$LX" ]] && ok "botón Full entre Cortar y LLC (x: $CX < $FX < $LX)" || ko "posición de Full: cut=$CX full=$FX llc=$LX"
KEY1=$(ls "$OUT/cache/thumbsheet" | head -1); N1=$(ls "$OUT/cache/thumbsheet" | wc -l)
# 1. Esc cancela
xdotool mousemove $((X+FX)) $((Y+FY)) click 1; sleep 1; import -window root "$OUT/1-dialogo.png"
xdotool key Escape; sleep 0.8
[[ -f "$A1" && ! -f "$A1CUT" ]] && ok "Esc en el diálogo: no se renombra nada" || ko "tras Esc: original=$([[ -f "$A1" ]] && echo sí || echo no) cortado=$([[ -f "$A1CUT" ]] && echo sí || echo no)"
# 2. Intro renombra, quita de la lista y pasa al siguiente
S1=$(md5sum "$A1" | cut -d' ' -f1)
xdotool mousemove $((X+FX)) $((Y+FY)) click 1; sleep 1; xdotool key Return
for i in $(seq 1 40); do grep -q "full: renombrado\|No se pudo renombrar" "$OUT/app.log" && break; sleep 0.25; done; sleep 0.8
[[ ! -f "$A1" && -f "$A1CUT" ]] && ok "Intro: $(basename "$A1") → $(basename "$A1CUT")" || ko "tras Intro: original=$([[ -f "$A1" ]] && echo sigue || echo no) cortado=$([[ -f "$A1CUT" ]] && echo sí || echo no) · $(grep 'full:\|toast' "$OUT/app.log" | tail -2 | tr '\n' ' ')"
[[ -f "$A1CUT" && "$(md5sum "$A1CUT" | cut -d' ' -f1)" == "$S1" ]] && ok "el contenido no cambia (mismo md5)" || ko "el contenido ha cambiado"
grep -q "toast ok: Renombrado: $(basename "$A1CUT")" "$OUT/app.log" && ok "toast verde «Renombrado: …»" || ko "sin toast verde: $(grep 'toast' "$OUT/app.log" | tail -1)"
grep -q "archivos: 1" "$OUT/app.log" && ok "sale de la lista: 1 archivo" || ko "cabecera: $(grep -o 'archivos: [0-9]*' "$OUT/app.log" | tail -1)"
[[ "$(shown)" == "$(basename "$A2")" ]] && ok "pasa al siguiente de la lista ($(shown))" || ko "a la vista: '$(shown)'"
grep -q "full: caché movida" "$OUT/app.log" && [[ "$(ls "$OUT/cache/thumbsheet" | wc -l)" == "$N1" && ! -d "$OUT/cache/thumbsheet/$KEY1" || "$(ls "$OUT/cache/thumbsheet" | wc -l)" == "$N1" ]] && ok "la caché se mueve a la clave del nombre nuevo (siguen $N1 directorios)" || ko "caché: $(grep 'full: caché\|full: no se pudo' "$OUT/app.log" | tail -1) · dirs=$(ls "$OUT/cache/thumbsheet" | wc -l)"
# 3. reabrir el renombrado: ya está en caché (no se generan capturas nuevas)
NF=$(grep -c "fin: " "$OUT/app.log" || true)
xdotool key ctrl+q; sleep 1
THUMBSHEET_DEBUG=1 python3 "$DIR/thumbsheet.py" "$A1CUT" >"$OUT/app2.log" 2>&1 &
APP=$!
for i in $(seq 1 40); do grep -q "fin: " "$OUT/app2.log" && break; sleep 0.25; done
ST=$(grep -o "start: .*" "$OUT/app2.log" | head -1); [[ "$ST" == *"0 por hacer"* ]] && ok "reabrir el renombrado: todo en caché ($ST)" || ko "reabrir el renombrado regenera: '$ST'"
xdotool key ctrl+q; sleep 1; kill $APP 2>/dev/null || true
grep -q "Traceback" "$OUT/app.log" "$OUT/app2.log" && ko "traceback en el log" || ok "sin tracebacks"
left=$(pgrep -c -x ffmpeg || true); [[ "$left" == 0 ]] && ok "sin ffmpeg tras cerrar" || ko "ffmpeg vivos tras cerrar: $left"
[[ -f "$VIDEO_A" ]] || { echo "¡¡el vídeo ORIGINAL ha desaparecido!!"; fail=1; }
[[ $fail == 0 ]] && echo "TODO OK" || { echo "HAY FALLOS"; exit 1; }
