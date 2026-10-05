#!/usr/bin/env bash
# Humo de la GUI en un Xvfb propio (no toca el DISPLAY real): abre dos vídeos, mueve sliders (clic y rueda),
# selecciona teselas (clic y arrastre), guarda el proyecto LLC, cambia de vídeo, Escape, Eliminar con
# confirmación, Ctrl+Q; y comprueba que no quedan ffmpeg ni tras un SIGKILL en plena generación.
# Uso: tests/gui_smoke.sh <vídeo A (se COPIA y la copia se borra)> <vídeo B (sólo lectura)> [dir_salida]
set -euo pipefail
VIDEO_A="$1"; VIDEO_B="$2"; OUT="${3:-/tmp/thumbsheet-gui}"; mkdir -p "$OUT/video"
DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
export DISPLAY=:77
export XDG_CACHE_HOME="$OUT/cache" XDG_CONFIG_HOME="$OUT/config"   # no tocar la caché ni los ajustes reales
export THUMBSHEET_AUDIO=0    # la prueba de reproducción no debe sonar por los altavoces
rm -rf "$OUT/cache" "$OUT/config"
COPY="$OUT/video/copia-$(basename "$VIDEO_A")"; cp -f "$VIDEO_A" "$COPY"
Xvfb :77 -screen 0 1400x900x24 -nolisten tcp >/dev/null 2>&1 &
XPID=$!
trap 'kill $APP 2>/dev/null; kill $XPID 2>/dev/null' EXIT
sleep 1
THUMBSHEET_DEBUG=1 python3 "$DIR/thumbsheet.py" "$COPY" "$VIDEO_B" >"$OUT/app.log" 2>&1 &
APP=$!
for i in $(seq 1 40); do WID=$(xdotool search --onlyvisible --classname thumbsheet 2>/dev/null | head -1 || true); [[ -n "$WID" ]] && break; sleep 0.25; done
[[ -n "${WID:-}" ]] || { echo "no aparece la ventana"; cat "$OUT/app.log"; exit 1; }
xdotool windowsize "$WID" 1400 900; xdotool windowmove "$WID" 0 0; sleep 0.3
eval "$(xdotool getwindowgeometry --shell "$WID")"   # X Y WIDTH HEIGHT
shot() { sleep "${2:-0.8}"; import -window root "$OUT/$1.png"; echo "captura $1"; }
fail=0; ok() { echo "ok   $1"; }; ko() { echo "FAIL $1"; fail=1; }
shot 1-inicial 4
# geometría publicada por la app (coordenadas relativas a la ventana)
G=$(grep "geometry:" "$OUT/app.log" | tail -1 || true)
[[ -n "$G" ]] || { echo "la app no publicó su geometría:"; cat "$OUT/app.log"; exit 1; }
echo "$G"
gv() { echo "$G" | sed -E "s/.* $1=([^ ]+).*/\1/"; }
SX=$(gv sheet | cut -d, -f1); SY=$(gv sheet | cut -d, -f2); COLS=$(gv cols); CW=$(gv cell | cut -dx -f1); CH=$(gv cell | cut -dx -f2); PAD=$(gv pad); GAP=$(gv gap)
tile() { local r=$1 c=$2; echo "$((X+SX+PAD+c*(CW+GAP)+CW/2)) $((Y+SY+PAD+r*(CH+GAP)+CH/2))"; }
IX=$(gv interval | cut -d, -f1); IY=$(gv interval | cut -d, -f2); TX=$(gv tile | cut -d, -f1); TY=$(gv tile | cut -d, -f2)
LX=$(gv llc | cut -d, -f1); LY=$(gv llc | cut -d, -f2); DX=$(gv del | cut -d, -f1); DY=$(gv del | cut -d, -f2)
CX=$(gv cut | cut -d, -f1); CY=$(gv cut | cut -d, -f2)
ROW1=$(gv rows | cut -d';' -f1); ROW2=$(gv rows | cut -d';' -f2)
# rueda sobre el slider de intervalo: +5 s por paso
xdotool mousemove $((X+IX)) $((Y+IY)) click 4 click 4; sleep 1.5
grep -q "plan: S=40" "$OUT/app.log" && ok "rueda sobre intervalo: 30 → 40 s (dos pasos)" || ko "rueda sobre intervalo (log sin plan S=40)"
# clic = toggle; arrastre = rango
xdotool mousemove $(tile 0 1) click 1; shot 2-toggle 1
xdotool mousemove $(tile 1 0) mousedown 1; sleep 0.1; for c in 1 2 3; do xdotool mousemove $(tile 1 $c); sleep 0.08; done; xdotool mouseup 1; shot 3-arrastre 1
SEL="$OUT/cache/thumbsheet"; S1=$(cat "$SEL"/*/selection.json 2>/dev/null | head -1); echo "selección guardada: $S1"
[[ "$S1" == '{"segments": [[40, 80], [200, 360]]}' ]] && ok "segmentos = tesela (0,1) → 40–80 s, fila 1 cols 0-3 → 200–360 s (S=40, 5 columnas)" || ko "selección inesperada: $S1"
xdotool mousemove $(tile 0 1) click 1; sleep 0.5; S2=$(cat "$SEL"/*/selection.json 2>/dev/null | head -1)
[[ "$S2" == '{"segments": [[200, 360]]}' ]] && ok "segundo clic deselecciona (queda 200–360)" || ko "toggle off falló: $S2"
# LLC
xdotool mousemove $((X+LX)) $((Y+LY)) click 1; sleep 1
LLC="$OUT/video/copia-$(basename "${VIDEO_A%.*}")-proj.llc"
if [[ -f "$LLC" ]]; then ok "proyecto LLC creado: $(basename "$LLC")"; cat "$LLC"; python3 -c "
import json,sys; d=json.load(open(sys.argv[1])); assert d['version']==2 and d['cutSegments']==[{'start':200,'end':360,'name':''}], d" "$LLC" && ok "segmento 200–360 (4 teselas de 40 s)" || ko "contenido LLC inesperado"; else ko "no existe $LLC"; fi
shot 4-llc 0.5
# Cortar (sin pérdida): 200–360 se amplía a keyframes (GOP 6 s → 198–360) y se une en <copia>-cortado.mp4
xdotool mousemove $((X+CX)) $((Y+CY)) click 1; shot 4a-dialogo-cortar 1; xdotool key Return
for i in $(seq 1 60); do grep -q "cut: hecho\|cut: error" "$OUT/app.log" && break; sleep 0.5; done
CUT="$OUT/video/copia-$(basename "${VIDEO_A%.*}")-cortado.mp4"
if [[ -f "$CUT" ]]; then CD=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$CUT"); python3 -c "import sys; d=float(sys.argv[1]); sys.exit(0 if 160 <= d <= 166 else 1)" "$CD" && ok "corte sin pérdida: $(basename "$CUT") dura ${CD%.*} s (198–360 por keyframes)" || ko "duración del corte inesperada: $CD"; else ko "no existe $CUT: $(grep 'cut:' "$OUT/app.log" | tail -1)"; fi
grep -q "cut: hecho" "$OUT/app.log" && ok "la app reporta el corte terminado y lo añade al panel" || ko "sin 'cut: hecho' en el log"
shot 4a2-tras-cortar 0.8
# cambiar el intervalo reajusta los segmentos hacia fuera a la nueva rejilla (y no los trocea)
xdotool mousemove $((X+IX)) $((Y+IY)) click 5 click 5; sleep 1.5     # 40 → 30 s
S3=$(cat "$SEL"/*/selection.json 2>/dev/null | head -1)
[[ "$S3" == '{"segments": [[200, 360]]}' ]] && ok "40→30 s: el segmento guardado sigue siendo 200–360 (se ven 180..330)" || ko "a 30 s el segmento cambió: $S3"
shot 4b-intervalo-30 0.8
xdotool mousemove $((X+IX)) $((Y+IY)) click 4 click 4; sleep 1.5     # 30 → 40 s
S4=$(cat "$SEL"/*/selection.json 2>/dev/null | head -1)
[[ "$S4" == '{"segments": [[200, 360]]}' ]] && ok "30→40 s: sigue 200–360 (bordes originales)" || ko "a 40 s el segmento cambió: $S4"
# clic derecho = vista ampliada; se cierra con clic o con Esc (sin tocar la selección)
xdotool mousemove $(tile 0 2) click 3; shot 4c-vista-ampliada 1.5
FULL=$(ls "$SEL"/*/full/80.jpg 2>/dev/null | head -1)
[[ -n "$FULL" ]] && ok "fotograma completo extraído: $(python3 -c "import gi; gi.require_version('GdkPixbuf','2.0'); from gi.repository import GdkPixbuf; p=GdkPixbuf.Pixbuf.new_from_file('$FULL'); print(p.get_width(),'x',p.get_height())")" || ko "no se extrajo full/80.jpg"
# flechas: siguiente / anterior fotograma (vecinos precargados)
xdotool windowfocus --sync "$WID"; xdotool key Right; sleep 0.9; shot 4c2-flecha-derecha 0.2
[[ -f "$SEL"/*/full/120.jpg ]] 2>/dev/null || ls "$SEL"/*/full/120.jpg >/dev/null 2>&1 && ok "flecha derecha: fotograma 2:00 (120 s) extraído" || ko "flecha derecha: falta full/120.jpg"
xdotool key Left key Left; sleep 0.9
ls "$SEL"/*/full/40.jpg >/dev/null 2>&1 && ok "dos flechas izquierda: fotograma 0:40 extraído" || ko "flecha izquierda: falta full/40.jpg"
# reproducción GStreamer: espacio arranca desde el fotograma (0:40), 2,5 s después pausa; flecha derecha salta +S
xdotool key space; sleep 3; shot 4e-reproduciendo 0.2; xdotool key space; sleep 0.6
P=$(grep -o "player: pausa en [0-9.]*" "$OUT/app.log" | tail -1 | awk '{print $4}')
python3 -c "import sys; p=float(sys.argv[1]); sys.exit(0 if 41.5 <= p <= 46 else 1)" "${P:-0}" && ok "play desde 0:40 y pausa ~3 s después (pos=$P)" || ko "posición tras reproducir inesperada: '$P'"
xdotool key Right; sleep 0.8
grep -q "player: seek a 8[0-9]\." "$OUT/app.log" && ok "flecha derecha con vídeo: seek +40 s (~1:20)" || ko "no hubo seek a ~80 s: $(grep 'player: seek' "$OUT/app.log" | tail -1)"
shot 4f-pausado-tras-seek 0.8
xdotool key Escape; sleep 0.5
xdotool mousemove $(tile 0 2) click 3; sleep 0.8     # reabrir la vista en modo fotograma para el resto de pasos
xdotool mousemove $((X+700)) $((Y+450)) click 1; sleep 0.5; shot 4d-vista-cerrada 0.3
xdotool mousemove $(tile 0 2) click 3; sleep 0.8; xdotool windowfocus --sync "$WID"; xdotool key Escape; sleep 0.5
S5=$(cat "$SEL"/*/selection.json 2>/dev/null | head -1)
[[ "$S5" == '{"segments": [[200, 360]]}' ]] && ok "Esc cierra la vista ampliada sin tocar la selección" || ko "Esc sobre la vista alteró la selección: $S5"
# Shift+clic: selecciona desde la última tesela pulsada (0,1)=40 s hasta (0,3)=120 s → 40–160
xdotool mousemove $(tile 0 3) keydown shift click 1 keyup shift; sleep 0.5
S6=$(cat "$SEL"/*/selection.json 2>/dev/null | head -1)
[[ "$S6" == '{"segments": [[40, 160], [200, 360]]}' ]] && ok "Shift+clic selecciona el rango 40–160 (desde la última pulsada)" || ko "Shift+clic inesperado: $S6"
# doble clic sobre una seleccionada: quita todo su tramo contiguo (200–360)
xdotool mousemove $(tile 1 2) click --repeat 2 --delay 90 1; sleep 0.6
S7=$(cat "$SEL"/*/selection.json 2>/dev/null | head -1)
[[ "$S7" == '{"segments": [[40, 160]]}' ]] && ok "doble clic deselecciona el tramo contiguo 200–360" || ko "doble clic inesperado: $S7"
# doble clic sobre una NO seleccionada: queda seleccionada (como un clic simple)
xdotool mousemove $(tile 3 0) click --repeat 2 --delay 90 1; sleep 0.6
S8=$(cat "$SEL"/*/selection.json 2>/dev/null | head -1)
[[ "$S8" == '{"segments": [[40, 160], [600, 640]]}' ]] && ok "doble clic sobre no seleccionada: la deja seleccionada" || ko "doble clic (no sel.) inesperado: $S8"
# segundo vídeo y vuelta
xdotool mousemove $((X+${ROW2%,*})) $((Y+${ROW2#*,})) click 1; shot 5-segundo-video 3
xdotool mousemove $((X+${ROW1%,*})) $((Y+${ROW1#*,})) click 1; shot 6-vuelta 1.5
# Escape limpia la selección
xdotool windowfocus --sync "$WID"; sleep 0.2; xdotool key Escape; sleep 0.6
[[ ! -f "$SEL"/*/selection.json ]] 2>/dev/null && ok "Escape limpia la selección" || { ls "$SEL"/*/selection.json 2>/dev/null | grep -q . && ko "Escape no limpió" || ok "Escape limpia la selección"; }
# Eliminar con confirmación (Alt+E en el diálogo)
xdotool mousemove $((X+DX)) $((Y+DY)) click 1; shot 7-dialogo-eliminar 1
xdotool key alt+e; sleep 1.2
[[ ! -f "$COPY" ]] && ok "archivo eliminado del disco" || ko "el archivo sigue existiendo"
shot 8-tras-eliminar 1
xdotool key ctrl+q; sleep 1
if kill -0 $APP 2>/dev/null; then ko "la app no cerró con Ctrl+Q"; kill $APP; sleep 1; else ok "cierre con Ctrl+Q"; fi
left=$(pgrep -c -x ffmpeg || true); [[ "$left" == 0 ]] && ok "sin ffmpeg tras cerrar" || ko "ffmpeg vivos tras cerrar: $left"
# SIGKILL en plena generación → sin huérfanos
rm -rf "$OUT/cache"; cp -f "$VIDEO_A" "$COPY"
python3 - "$OUT/config/thumbsheet/settings.json" <<'PY'
import json, sys, pathlib; f = pathlib.Path(sys.argv[1]); d = json.loads(f.read_text()) if f.exists() else {}; d["interval"] = 5; f.parent.mkdir(parents=True, exist_ok=True); f.write_text(json.dumps(d))
PY
THUMBSHEET_DEBUG=1 python3 "$DIR/thumbsheet.py" "$COPY" >"$OUT/app2.log" 2>&1 &
APP=$!; sleep 3
busy=$(pgrep -c -x ffmpeg || true); kill -9 $APP 2>/dev/null; sleep 1.5   # SIGTERM por PDEATHSIG: ffmpeg termina limpio, no instantáneo
left2=$(pgrep -c -x ffmpeg || true); [[ "$busy" -gt 0 && "$left2" == 0 ]] && ok "SIGKILL en plena generación: $busy ffmpeg → 0" || ko "SIGKILL: antes=$busy después=$left2"
echo "--- log"; grep -v "^\[.*\] geometry" "$OUT/app.log" | tail -12
echo "RESULTADO: $([[ $fail == 0 ]] && echo OK || echo FALLO)"; exit $fail
