#!/usr/bin/env bash
# Humo de la GUI en un Xvfb propio: abre la app, mueve los sliders con xdotool, hace scroll y deja
# capturas en el directorio de salida. No toca el DISPLAY real.
# Uso: tests/gui_smoke.sh <vídeo> [dir_salida]
set -euo pipefail
VIDEO="$1"; OUT="${2:-/tmp/thumbsheet-gui}"; mkdir -p "$OUT"
DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
export DISPLAY=:77
export XDG_CACHE_HOME="$OUT/cache" XDG_CONFIG_HOME="$OUT/config"   # no tocar la caché ni los ajustes reales
Xvfb :77 -screen 0 1400x900x24 -nolisten tcp >/dev/null 2>&1 &
XPID=$!
trap 'kill $APP 2>/dev/null; kill $XPID 2>/dev/null' EXIT
sleep 1
THUMBSHEET_DEBUG=1 python3 "$DIR/thumbsheet.py" "$VIDEO" >"$OUT/app.log" 2>&1 &
APP=$!
for i in $(seq 1 40); do WID=$(xdotool search --onlyvisible --classname thumbsheet 2>/dev/null | head -1 || true); [[ -n "$WID" ]] && break; sleep 0.25; done
[[ -n "${WID:-}" ]] || { echo "no aparece la ventana"; cat "$OUT/app.log"; exit 1; }
xdotool windowsize "$WID" 1400 900; xdotool windowmove "$WID" 0 0; sleep 0.3
eval "$(xdotool getwindowgeometry --shell "$WID")"   # X Y WIDTH HEIGHT
shot() { sleep "${2:-0.8}"; import -window root "$OUT/$1.png"; echo "captura $1"; }
shot 1-inicial 4
# barra: y≈22. Intervalo ocupa aprox. x∈[80,560], Tamaño x∈[680,1150] (ventana de 1400)
xdotool mousemove $((X+700)) $((Y+22)) click 1; shot 2-teselas-pequenas 1.2     # tesela ~mínima
xdotool mousemove $((X+1100)) $((Y+22)) click 1; shot 3-teselas-grandes 1.2     # tesela grande
xdotool mousemove $((X+900)) $((Y+22)) click 1; sleep 0.5
xdotool mousemove $((X+700)) $((Y+500)); for i in 1 2 3 4 5 6; do xdotool click 5; sleep 0.05; done; shot 4-scroll 1.2
xdotool mousemove $((X+90)) $((Y+22)) click 1; shot 5-intervalo-minimo 6       # intervalo 5 s → regenera
xdotool windowactivate --sync "$WID" 2>/dev/null || true; xdotool windowfocus --sync "$WID"; sleep 0.3; xdotool key Escape; sleep 1
if kill -0 $APP 2>/dev/null; then echo "FALLO: la app no cerró con Escape"; kill $APP; sleep 1; else echo "cierre con Escape OK"; fi
left=$(pgrep -c -f "ffmpeg -nostdin" || true); echo "ffmpeg vivos tras cerrar: $left"
# segunda ronda: arrancar con intervalo denso, matar con SIGKILL en plena generación y comprobar huérfanos
rm -rf "$OUT/cache"   # caché vacía: la segunda ronda tiene que generar de verdad
THUMBSHEET_DEBUG=1 python3 "$DIR/thumbsheet.py" "$VIDEO" >"$OUT/app2.log" 2>&1 &
APP=$!; sleep 2.5
busy=$(pgrep -c -f "ffmpeg -nostdin" || true); kill -9 $APP; sleep 0.7
left2=$(pgrep -c -f "ffmpeg -nostdin" || true); echo "SIGKILL en plena generación: ffmpeg antes=$busy después=$left2"
echo "--- log"; cat "$OUT/app.log"
echo "--- settings"; cat "$XDG_CONFIG_HOME/thumbsheet/settings.json" 2>/dev/null; echo
