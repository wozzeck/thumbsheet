#!/usr/bin/env bash
# Humo de la GUI en un Xvfb propio: reorden en vivo del panel de segmentos. Abre una copia de A, marca tres
# teselas sueltas (tres tramos) y con xdotool: arrastra la 3.ª tarjeta hasta la 1.ª (a medio arrastre ya tiene que
# estar colocada delante), la suelta; arrastra la 1.ª al hueco bajo la última y la suelta; arrastra y cancela con
# Esc (el orden vuelve al de partida). Comprueba también que la fila del panel de vídeos lleva el tamaño.
# Capturas PNG en `out`. Uso: tests/gui_segdrag.sh <vídeo A corto> [out]
set -euo pipefail
VIDEO_A="$1"; OUT="${2:-/tmp/thumbsheet-gui-segdrag}"; rm -rf "$OUT"; mkdir -p "$OUT/video"
DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
export DISPLAY=:75
export XDG_CACHE_HOME="$OUT/cache" XDG_CONFIG_HOME="$OUT/config" THUMBSHEET_AUDIO=0
COPY="$OUT/video/copia-$(basename "$VIDEO_A")"; cp -f "$VIDEO_A" "$COPY"
Xvfb :75 -screen 0 1400x900x24 -nolisten tcp >/dev/null 2>&1 &
XPID=$!
trap 'kill $APP 2>/dev/null || true; kill $XPID 2>/dev/null || true' EXIT
sleep 1
THUMBSHEET_DEBUG=1 python3 "$DIR/thumbsheet.py" "$COPY" >"$OUT/app.log" 2>&1 &
APP=$!
for i in $(seq 1 40); do WID=$(xdotool search --onlyvisible --classname thumbsheet 2>/dev/null | head -1 || true); [[ -n "$WID" ]] && break; sleep 0.25; done
[[ -n "${WID:-}" ]] || { echo "no aparece la ventana"; cat "$OUT/app.log"; exit 1; }
xdotool windowsize "$WID" 1400 900; xdotool windowmove "$WID" 0 0; sleep 0.3
eval "$(xdotool getwindowgeometry --shell "$WID")"   # X Y WIDTH HEIGHT
shot() { sleep "${2:-0.8}"; import -window root "$OUT/$1.png"; echo "captura $1"; }
fail=0; ok() { echo "ok   $1"; }; ko() { echo "FAIL $1"; fail=1; }
for i in $(seq 1 60); do grep -q "^fin: \|fin: " "$OUT/app.log" && break; sleep 0.5; done
shot 1-inicial 1
G=$(grep "geometry:" "$OUT/app.log" | tail -1 || true)
[[ -n "$G" ]] || { echo "la app no publicó su geometría:"; cat "$OUT/app.log"; exit 1; }
gv() { echo "$G" | sed -E "s/.* $1=([^ ]+).*/\1/"; }
SX=$(gv sheet | cut -d, -f1); SY=$(gv sheet | cut -d, -f2); CW=$(gv cell | cut -dx -f1); CH=$(gv cell | cut -dx -f2); PAD=$(gv pad); GAP=$(gv gap)
tile() { local r=$1 c=$2; echo "$((X+SX+PAD+c*(CW+GAP)+CW/2)) $((Y+SY+PAD+r*(CH+GAP)+CH/2))"; }
S0=$(grep -o "plan: S=[0-9]*" "$OUT/app.log" | tail -1 | cut -d= -f2)
sel() { cat "$OUT/cache/thumbsheet"/*/selection.json 2>/dev/null | head -1 || true; }
order() { python3 -c "import json,sys; d=json.loads(sys.argv[1] or '{}'); print(d.get('order') or [s[0] for s in d.get('segments', [])])" "$(sel)"; }
rows() { grep "segpanel:" "$OUT/app.log" | tail -1 | sed -E 's/.*rows=([^ ]+).*/\1/'; }
rowxy() { echo "$(rows)" | cut -d';' -f"$1"; }
nplace() { grep -c "segdrag:" "$OUT/app.log" || true; }

# 1. tamaño en la fila del panel de vídeos (misma cifra que human_size del fichero)
SIZE=$(python3 -c "
import sys; sys.argv=['x']; sys.path.insert(0, '$DIR'); import importlib.util
spec = importlib.util.spec_from_file_location('ts', '$DIR/thumbsheet.py'); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
import os; print(m.human_size(os.path.getsize('$COPY')))")
SUB=$(python3 -c "
import sys; sys.argv=['x']; sys.path.insert(0, '$DIR'); import importlib.util
spec = importlib.util.spec_from_file_location('ts', '$DIR/thumbsheet.py'); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.CACHE_ROOT = type(m.CACHE_ROOT)('$OUT/cache/thumbsheet'); d = m.Document('$COPY'); d.ensure_info(); print(d.subtitle)")
[[ "$SUB" == *" · $SIZE" ]] && ok "fila del panel: '$SUB' termina con el tamaño ($SIZE)" || ko "subtítulo de la fila sin tamaño: '$SUB' (esperado … · $SIZE)"

# 2. tres tramos sueltos: teselas 0, 2 y 4 de la primera fila
for c in 0 2 4; do xdotool mousemove $(tile 0 $c) click 1; sleep 0.4; done; sleep 0.8
SP=$(grep "segpanel:" "$OUT/app.log" | tail -1)
[[ "$SP" == *"3 segmentos"* ]] && ok "panel: 3 segmentos" || ko "panel: '$SP'"
[[ "$(order)" == "[0, $((2*S0)), $((4*S0))]" ]] && ok "orden inicial [0, $((2*S0)), $((4*S0))]" || ko "orden inicial: $(order)"
R1=$(rowxy 1); R2=$(rowxy 2); R3=$(rowxy 3)
drag_to() {   # desde (x,y) del asa hasta (x2,y2), en pasos, sin soltar
  xdotool mousemove "$1" "$2" mousedown 1; sleep 0.25
  for d in 3 7 12 18 25; do xdotool mousemove $(($1+d)) $(($2-d)); sleep 0.08; done
  xdotool mousemove "$3" "$4"; sleep 0.3; xdotool mousemove $(($3+2)) "$4"; sleep 0.3
}

# 3. la 3.ª tarjeta hasta la mitad superior de la 1.ª: a medio arrastre ya está colocada la primera; al soltar, se guarda
N0=$(nplace)
drag_to $((X+${R3%,*})) $((Y+${R3#*,})) $((X+${R1%,*}+60)) $((Y+${R1#*,}-10))
shot 2-arrastrando-3a-a-1a 0.3
[[ "$(nplace)" -gt "$N0" ]] && ok "a medio arrastre la tarjeta ya cambia de sitio ($(grep 'segdrag:' "$OUT/app.log" | tail -1 | sed 's/.*segdrag: //'))" || ko "sin reorden en vivo durante el arrastre"
[[ "$(order)" == "[0, $((2*S0)), $((4*S0))]" ]] && ok "mientras se arrastra no se guarda nada" || ko "orden guardado a medio arrastre: $(order)"
IC=$(grep -o "segdrag: icono [0-9]*x[0-9]*" "$OUT/app.log" | tail -1); [[ "$IC" == *"icono 280x"* ]] && ok "icono de arrastre = la tarjeta entera ($IC)" || ko "icono de arrastre: '$IC' (esperado 280 de ancho)"
AN=$(grep -o "seganim: [0-9]* fotogramas en [0-9]* ms" "$OUT/app.log" | tail -1); F=$(echo "$AN" | awk '{print $2}')
[[ -n "$F" && "$F" -ge 5 ]] && ok "las demás tarjetas se deslizan con animación ($AN)" || ko "sin animación de deslizamiento: '$AN'"
xdotool mouseup 1; sleep 0.9
[[ "$(order)" == "[$((4*S0)), 0, $((2*S0))]" ]] && ok "soltar sobre la 1.ª: orden [$((4*S0)), 0, $((2*S0))]" || ko "orden tras soltar: $(order)"
shot 3-tras-soltar 0.3
SP=$(grep "segpanel:" "$OUT/app.log" | tail -1); [[ "$SP" == *"3 segmentos"* ]] && ok "panel reconstruido con 3 segmentos" || ko "panel tras soltar: '$SP'"

# 4. la 1.ª tarjeta al hueco bajo la última (fondo del panel): va al final
R1=$(rowxy 1); R3=$(rowxy 3); N0=$(nplace)
drag_to $((X+${R1%,*})) $((Y+${R1#*,})) $((X+${R3%,*}+60)) $((Y+${R3#*,}+70))
shot 4-arrastrando-al-fondo 0.3
[[ "$(nplace)" -gt "$N0" ]] && ok "sobre el hueco del fondo la tarjeta pasa al final" || ko "sin reorden al pasar por el fondo"
xdotool mouseup 1; sleep 0.9
[[ "$(order)" == "[0, $((2*S0)), $((4*S0))]" ]] && ok "soltar en el fondo: orden [0, $((2*S0)), $((4*S0))]" || ko "orden tras soltar en el fondo: $(order)"

# 5. arrastrar la 2.ª hasta arriba y cancelar con Esc: la lista vuelve al orden de partida y no se guarda nada
R1=$(rowxy 1); R2=$(rowxy 2); N0=$(nplace)
drag_to $((X+${R2%,*})) $((Y+${R2#*,})) $((X+${R1%,*}+60)) $((Y+${R1#*,}-10))
[[ "$(nplace)" -gt "$N0" ]] && ok "cancelar: a medio arrastre estaba colocada arriba" || ko "cancelar: sin reorden en vivo"
xdotool key Escape; sleep 0.5; xdotool mouseup 1; sleep 0.9
shot 5-tras-cancelar 0.3
[[ "$(order)" == "[0, $((2*S0)), $((4*S0))]" ]] && ok "Esc cancela: orden intacto [0, $((2*S0)), $((4*S0))]" || ko "orden tras cancelar: $(order)"
SP=$(grep "segpanel:" "$OUT/app.log" | tail -1); [[ "$SP" == *"3 segmentos"* ]] && ok "panel sigue con 3 segmentos" || ko "panel tras cancelar: '$SP'"
# 5b. clic (sin arrastrar) en la 2.ª tarjeta lleva el mosaico a su instante; la × de la 1.ª quita el tramo
# (mover, esperar y pulsar: si el puntero llevaba un rato quieto sobre otra tarjeta hay un tooltip encima)
R2=$(rowxy 2); xdotool mousemove $((X+${R2%,*})) $((Y+${R2#*,})); sleep 0.4; xdotool click 1; sleep 0.5
grep -q "seggoto: ir a $(python3 -c "import sys; sys.path.insert(0,'$DIR'); print(__import__('thumbsheet').fmt_time($((2*S0))))")" "$OUT/app.log" && ok "clic en la 2.ª tarjeta: ir a $((2*S0)) s" || ko "el clic en la tarjeta no lleva al instante: $(grep 'seggoto:' "$OUT/app.log" | tail -1)"
D1=$(grep "segpanel: 3" "$OUT/app.log" | tail -1 | sed -E 's/.*dels=([^ ]+).*/\1/' | cut -d';' -f1)
xdotool mousemove $((X+${D1%,*})) $((Y+${D1#*,})); sleep 0.4; xdotool click 1; sleep 0.9
[[ "$(order)" == "[$((2*S0)), $((4*S0))]" ]] && ok "× de la 1.ª tarjeta quita el tramo 0–$S0" || ko "orden tras ×: $(order)"
grep -q "Traceback" "$OUT/app.log" && ko "traceback en el log" || ok "sin tracebacks"

# 6. cierre
xdotool key ctrl+q; sleep 1
if kill -0 $APP 2>/dev/null; then ko "la app no cerró con Ctrl+Q"; kill $APP; sleep 1; else ok "cierre con Ctrl+Q"; fi
left=$(pgrep -c -x ffmpeg || true); [[ "$left" == 0 ]] && ok "sin ffmpeg tras cerrar" || ko "ffmpeg vivos tras cerrar: $left"
[[ -f "$VIDEO_A" ]] || { echo "¡¡el vídeo ORIGINAL ha desaparecido!!"; fail=1; }
[[ $fail == 0 ]] && echo "TODO OK" || { echo "HAY FALLOS"; exit 1; }
