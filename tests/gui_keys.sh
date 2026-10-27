#!/usr/bin/env bash
# Humo de la GUI en un Xvfb propio (:70): zoom con la rueda y atajos de teclado. Abre tres COPIAS de A.
#  1. Ctrl+rueda sobre el mosaico cambia las teselas por fila y no el intervalo; Ctrl+Mayús+rueda cambia el
#     intervalo (arriba acerca: más corto; abajo aleja) y no las teselas por fila; la rueda sola sólo desplaza.
#  2. C sin segmentos no abre nada; con un segmento abre el diálogo de corte; Esc lo cierra sin tocar nada;
#     C + Intro corta y borra el original y pasa al siguiente. Se corta en modo exacto (flecha abajo en el
#     diálogo), que tarda unos segundos, para pulsar un segundo Intro con el corte en marcha: no debe cancelarlo
#     (antes Cancelar era el botón por defecto en ese momento). Ctrl+C no abre el diálogo.
#  3. Supr abre el diálogo de Eliminar; Esc no borra; Supr + Intro borra y pasa al siguiente.
# Uso: tests/gui_keys.sh <vídeo A, de unos minutos o más> [out]
set -euo pipefail
VIDEO_A="$1"; OUT="${2:-/tmp/thumbsheet-gui-keys}"; rm -rf "$OUT"; mkdir -p "$OUT/video"
DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
export DISPLAY=:70
export XDG_CACHE_HOME="$OUT/cache" XDG_CONFIG_HOME="$OUT/config" THUMBSHEET_AUDIO=0
EXT="${VIDEO_A##*.}"; K1="$OUT/video/uno.$EXT"; K2="$OUT/video/dos.$EXT"; K3="$OUT/video/tres.$EXT"
for f in "$K1" "$K2" "$K3"; do cp -f "$VIDEO_A" "$f"; done
Xvfb :70 -screen 0 1400x900x24 -nolisten tcp >/dev/null 2>&1 &
XPID=$!
trap 'kill $APP 2>/dev/null || true; kill $XPID 2>/dev/null || true' EXIT
sleep 1
THUMBSHEET_DEBUG=1 python3 "$DIR/thumbsheet.py" "$K1" "$K2" "$K3" >"$OUT/app.log" 2>&1 &
APP=$!
for i in $(seq 1 40); do WID=$(xdotool search --onlyvisible --classname thumbsheet 2>/dev/null | head -1 || true); [[ -n "$WID" ]] && break; sleep 0.25; done
[[ -n "${WID:-}" ]] || { echo "no aparece la ventana"; cat "$OUT/app.log"; exit 1; }
xdotool windowsize "$WID" 1400 900; xdotool windowmove "$WID" 0 0; sleep 0.3
eval "$(xdotool getwindowgeometry --shell "$WID")"
fail=0; ok() { echo "ok   $1"; }; ko() { echo "FAIL $1"; fail=1; }
shot() { sleep "${2:-0.5}"; import -window root "$OUT/$1.png"; }
n() { grep -c "$1" "$OUT/app.log" || true; }
shown() { grep "\] vídeo: " "$OUT/app.log" | tail -1 | sed 's/.*vídeo: //; s/ [0-9]*x[0-9]* .*//'; }
S() { grep -o "plan: S=[0-9]*" "$OUT/app.log" | tail -1 | cut -d= -f2; }
ncols() { { grep -o "cols: [0-9]* -> [0-9]*" "$OUT/app.log" || true; } | tail -1 | sed 's/.*-> //'; }
dlg() { xdotool search --onlyvisible --name "Cortar y unir" 2>/dev/null | head -1 || true; }
waitfor() { local i; for i in $(seq 1 $((${2:-20}*4))); do grep -q "$1" "$OUT/app.log" && return 0; sleep 0.25; done; return 1; }
untilS() { local i; for i in $(seq 1 40); do [[ "$(S)" == "$1" ]] && return 0; sleep 0.25; done; return 1; }
waitfor "fin: .*\[uno\.$EXT\]" 90 || { echo "no termina de generar uno.$EXT"; tail -5 "$OUT/app.log"; exit 1; }
waitfor "geometry:" 20 || true; sleep 0.5
G=$(grep "geometry:" "$OUT/app.log" | tail -1 || true); [[ -n "$G" ]] || { echo "la app no publicó su geometría"; exit 1; }
gv() { echo "$G" | sed -E "s/.* $1=([^ ]+).*/\1/"; }
SX=$(gv sheet | cut -d, -f1); SY=$(gv sheet | cut -d, -f2); COLS0=$(gv cols); CW=$(gv cell | cut -dx -f1); CH=$(gv cell | cut -dx -f2); PAD=$(gv pad); GAP=$(gv gap)
tile() { local r=$1 c=$2; echo "$((X+SX+PAD+c*(CW+GAP)+CW/2)) $((Y+SY+PAD+r*(CH+GAP)+CH/2))"; }
S0=$(S); INTS=(1 2 5 10 20 30 60 300 600); I0=-1
for i in "${!INTS[@]}"; do [[ "${INTS[$i]}" == "$S0" ]] && I0=$i; done
[[ "$I0" -ge 1 && "$I0" -le 7 ]] || { echo "intervalo inicial inesperado: $S0"; exit 1; }
UP=${INTS[$((I0-1))]}; DOWN=${INTS[$((I0+1))]}
echo "intervalo $S0 s, $COLS0 por fila"

# 1. zoom con la rueda sobre el mosaico
xdotool windowfocus --sync "$WID" 2>/dev/null || true
xdotool mousemove $(tile 1 1); sleep 0.3
NC=$(n "cols: ")
xdotool keydown ctrl click 5 keyup ctrl
for i in $(seq 1 20); do [[ "$(n 'cols: ')" -gt "$NC" ]] && break; sleep 0.25; done; sleep 1   # el intervalo se aplicaría a los 350 ms
[[ "$(ncols)" == "$((COLS0+1))" && "$(S)" == "$S0" ]] && ok "Ctrl+rueda abajo: $COLS0 → $(ncols) teselas por fila (más pequeñas); el intervalo sigue en $(S) s" || ko "Ctrl+rueda abajo: cols=$(ncols) (esperado $((COLS0+1))), S=$(S) (esperado $S0)"
xdotool keydown ctrl click 4 keyup ctrl; sleep 1.2
[[ "$(ncols)" == "$COLS0" && "$(S)" == "$S0" ]] && ok "Ctrl+rueda arriba: vuelve a $COLS0 por fila (más grandes); el intervalo sigue en $(S) s" || ko "Ctrl+rueda arriba: cols=$(ncols) S=$(S)"
NC=$(n "cols: ")
xdotool keydown ctrl keydown shift click 4 keyup shift keyup ctrl
untilS "$UP" || true
[[ "$(S)" == "$UP" && "$(n 'cols: ')" == "$NC" ]] && ok "Ctrl+Mayús+rueda arriba acerca: intervalo $S0 → $(S) s; teselas por fila sin tocar" || ko "Ctrl+Mayús+rueda arriba: S=$(S) (esperado $UP), cambios de columnas: $(( $(n 'cols: ') - NC ))"
shot 1-intervalo-acercado
xdotool keydown ctrl keydown shift click 5 click 5 keyup shift keyup ctrl
untilS "$DOWN" || true
[[ "$(S)" == "$DOWN" && "$(n 'cols: ')" == "$NC" ]] && ok "Ctrl+Mayús+rueda abajo, dos pasos, aleja: $UP → $(S) s" || ko "Ctrl+Mayús+rueda abajo: S=$(S) (esperado $DOWN)"
xdotool keydown ctrl keydown shift click 4 keyup shift keyup ctrl
untilS "$S0" || true
[[ "$(S)" == "$S0" ]] && ok "Ctrl+Mayús+rueda arriba: de vuelta a $S0 s" || ko "vuelta al intervalo inicial: S=$(S)"
sleep 1; NP=$(n "plan: S="); NC=$(n "cols: ")
for i in $(seq 1 30); do xdotool click 4; done; sleep 1   # rueda sola: sólo desplaza (y deja el mosaico arriba del todo)
[[ "$(n 'plan: S=')" == "$NP" && "$(n 'cols: ')" == "$NC" ]] && ok "la rueda sola no cambia ni el intervalo ni el tamaño" || ko "la rueda sola cambió algo: planes $NP → $(n 'plan: S='), columnas $NC → $(n 'cols: ')"

# 2. C = Cortar
xdotool key c; sleep 1
[[ -z "$(dlg)" && "$(n 'cortar: diálogo')" == 0 ]] && ok "C sin segmentos no abre nada" || ko "C sin segmentos abrió el diálogo de corte"
xdotool mousemove $(tile 0 1) click 1; sleep 0.6
SELJ=$(cat "$OUT/cache/thumbsheet"/*/selection.json 2>/dev/null | head -1 || true)
[[ -n "$SELJ" ]] && ok "una tesela seleccionada: $SELJ" || ko "no se seleccionó ninguna tesela"
xdotool key c
for i in $(seq 1 20); do [[ -n "$(dlg)" ]] && break; sleep 0.25; done
[[ -n "$(dlg)" ]] && ok "C abre el diálogo de corte" || ko "C no abrió el diálogo de corte"
shot 2-dialogo-cortar
xdotool key Escape; sleep 0.8
[[ -z "$(dlg)" && -f "$K1" && "$(n 'cut: ')" == 0 ]] && ok "Esc cierra el diálogo sin cortar ni borrar" || ko "tras Esc: diálogo='$(dlg)' original=$([[ -f "$K1" ]] && echo sí || echo no) cortes=$(n 'cut: ')"
xdotool key c
for i in $(seq 1 20); do [[ -n "$(dlg)" ]] && break; sleep 0.25; done
xdotool key Down; sleep 0.3                    # «Exacto al fotograma»: recodifica, tarda unos segundos
xdotool key Return; sleep 0.6; xdotool key Return   # el segundo Intro llega con el corte en marcha
waitfor "cut: original borrado\|cut: error\|cut: cancelado\|No se pudo eliminar" 120 || true
sleep 0.5
CUT=$(ls "$OUT/video"/uno-cortado.* 2>/dev/null | head -1 || true)
[[ "$(n 'cortar: Intro = cortar y borrar original')" -ge 1 ]] && ok "Intro en el diálogo = cortar y borrar original" || ko "Intro no confirmó «cortar y borrar»"
[[ ! -f "$K1" && -n "$CUT" ]] && ok "C + Intro: corta ($(basename "$CUT")) y borra el original" || ko "C + Intro: original=$([[ -f "$K1" ]] && echo sigue || echo borrado) cortado='$CUT' · $(grep 'cut:' "$OUT/app.log" | tail -1)"
grep -q "cut: .*libx264" "$OUT/app.log" && ok "flecha abajo en el diálogo elige el modo exacto" || ko "no se cortó en modo exacto"
[[ "$(n 'cut: cancelado')" == 0 ]] && ok "el segundo Intro, ya cortando, no cancela el corte" || ko "el segundo Intro canceló el corte"
grep -q "toast ok: Cortado.*original borrado" "$OUT/app.log" && ok "toast verde: cortado y original borrado" || ko "sin toast de cortado+borrado: $(grep 'toast' "$OUT/app.log" | tail -1)"
for i in $(seq 1 20); do [[ "$(shown)" == "dos.$EXT" ]] && break; sleep 0.25; done
[[ "$(shown)" == "dos.$EXT" ]] && ok "pasa al siguiente de la lista (dos.$EXT)" || ko "a la vista: '$(shown)'"
sleep 1; xdotool mousemove $(tile 0 1) click 1; sleep 0.6
ND=$(n "cortar: diálogo"); xdotool key ctrl+c; sleep 1
[[ "$(n 'cortar: diálogo')" == "$ND" && -z "$(dlg)" ]] && ok "Ctrl+C no abre el diálogo de corte (sólo la C a secas)" || ko "Ctrl+C abrió el diálogo de corte"
xdotool key Escape; sleep 0.5

# 3. Supr = Eliminar
NE=$(n "eliminar: diálogo")
xdotool key Delete
for i in $(seq 1 20); do [[ "$(n 'eliminar: diálogo')" -gt "$NE" ]] && break; sleep 0.25; done
grep -q "eliminar: diálogo dos.$EXT" "$OUT/app.log" && ok "Supr abre el diálogo de Eliminar (dos.$EXT)" || ko "Supr no abrió el diálogo de Eliminar"
shot 3-dialogo-eliminar
xdotool key Escape; sleep 0.8
[[ -f "$K2" ]] && grep -q "eliminar: cancelado" "$OUT/app.log" && ok "Esc en el diálogo de Eliminar: no se borra nada" || ko "tras Esc: dos.$EXT $([[ -f "$K2" ]] && echo sigue || echo BORRADO)"
xdotool key Delete; sleep 1; xdotool key Return
waitfor "toast ok: Eliminado\|No se pudo eliminar" 30 || true
[[ ! -f "$K2" ]] && grep -q "toast ok: Eliminado: dos.$EXT" "$OUT/app.log" && ok "Supr + Intro: dos.$EXT eliminado, toast verde" || ko "Supr + Intro: dos.$EXT $([[ -f "$K2" ]] && echo sigue || echo borrado) · $(grep 'toast' "$OUT/app.log" | tail -1)"
for i in $(seq 1 20); do [[ "$(shown)" == "tres.$EXT" ]] && break; sleep 0.25; done
[[ "$(shown)" == "tres.$EXT" ]] && ok "pasa al siguiente (tres.$EXT)" || ko "a la vista: '$(shown)'"

xdotool key ctrl+q; sleep 1; kill $APP 2>/dev/null || true
grep -q "Traceback" "$OUT/app.log" && ko "traceback en el log" || ok "sin tracebacks"
left=$(pgrep -c -x ffmpeg || true); [[ "$left" == 0 ]] && ok "sin ffmpeg tras cerrar" || ko "ffmpeg vivos tras cerrar: $left"
[[ -f "$VIDEO_A" ]] || { echo "¡¡el vídeo ORIGINAL ha desaparecido!!"; fail=1; }
[[ $fail == 0 ]] && echo "TODO OK" || { echo "HAY FALLOS"; exit 1; }
