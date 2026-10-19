#!/usr/bin/env bash
# Banco (no es pasa/falla): cuánto tarda en completarse la pantalla tras saltos grandes de scroll, con una sábana de
# miles de teselas ya en caché. Abre el vídeo con intervalo 1 s y 16 columnas en un Xvfb propio (:74); la primera
# vez genera la caché (tarda), las siguientes la reutiliza (queda en `out/cache`). Hace siete saltos por el canal
# de la barra y una ráfaga de 60 muescas de rueda, y saca las trazas DEBUG `salto:` (teselas visibles sin cargar
# al llegar) y `lleno:` (ms hasta que no queda ninguna). Uso: tests/bench_scrollfill.sh <vídeo largo> [out]
set -u
VIDEO="$1"; OUT="${2:-/tmp/thumbsheet-bench}"; mkdir -p "$OUT/config/thumbsheet"
DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
echo '{"interval": 1, "cols": 16, "segpanel": false, "panel_w": 240}' > "$OUT/config/thumbsheet/settings.json"
Xvfb :74 -screen 0 1400x900x24 -nolisten tcp >/dev/null 2>&1 &
XP=$!; sleep 1; export DISPLAY=:74
LOG="$OUT/app.log"
XDG_CACHE_HOME="$OUT/cache" XDG_CONFIG_HOME="$OUT/config" THUMBSHEET_AUDIO=0 THUMBSHEET_DEBUG=1 python3 "$DIR/thumbsheet.py" "$VIDEO" >"$LOG" 2>&1 &
AP=$!
trap 'kill $AP 2>/dev/null || true; kill $XP 2>/dev/null || true' EXIT
for i in $(seq 1 40); do WID=$(xdotool search --onlyvisible --classname thumbsheet 2>/dev/null | head -1 || true); [[ -n "$WID" ]] && break; sleep 0.25; done
[[ -n "${WID:-}" ]] || { echo "no aparece la ventana"; cat "$LOG"; exit 1; }
xdotool windowsize "$WID" 1400 900; xdotool windowmove "$WID" 0 0; sleep 2
for i in $(seq 1 2400); do grep -q "fin: " "$LOG" && break; sleep 0.5; done   # primera vez: generación completa
sleep 3
G=$(grep "geometry:" "$LOG" | tail -1); gv() { echo "$G" | sed -E "s/.* $1=([^ ]+).*/\1/"; }
SY=$(gv sheet | cut -d, -f2); TX=$((1400-8))
for f in 50 10 90 30 70 20 60; do Y=$((SY+20 + (860-SY-40)*f/100)); xdotool mousemove $TX $Y click 1; sleep 4; done
xdotool mousemove 700 500; for i in $(seq 1 60); do xdotool click 5; sleep 0.04; done; sleep 6
xdotool key ctrl+q; sleep 1
python3 - "$LOG" <<'PY'
import re, sys, statistics
lines = open(sys.argv[1], encoding="utf-8", errors="replace").read().splitlines()
ll = [int(m.group(1)) for l in lines for m in [re.search(r"lleno: (\d+) ms", l)] if m]
huecos = [int(m.group(1)) for l in lines for m in [re.search(r"salto: .*?, (\d+) huecos", l)] if m]
print("saltos (8 esperados: 7 clics + rueda): pantallas completadas=%d · huecos por salto hasta %d" % (len(ll), max(huecos) if huecos else 0))
print("lleno (ms): %s · mediana %s · máximo %s" % (ll, statistics.median(ll) if ll else "-", max(ll) if ll else "-"))
d = [l[11:] for l in lines if "draw:" in l][-2:]
print("\n".join(d))
PY
