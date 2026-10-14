#!/usr/bin/env bash
# Humo de la GUI en un Xvfb propio: vídeos que no se pueden abrir o no dan fotogramas no paran el segundo plano.
# Abre, en este orden: copia de A (válido, se genera a la vista), una copia de L con los datos (mdat) a ceros
# (ffprobe la acepta, ffmpeg no saca nada), un fichero de texto con extensión .mp4, y una segunda copia de A.
# Comprueba: aspa roja (estado → error) en los dos rotos, el segundo plano pasa de largo y genera la copia del
# final, no quedan marcas .fail, y al pinchar en el vídeo a ceros se reintenta, vuelve a quedar en error con toast
# rojo y la cola sigue. Capturas PNG en `out`. Uso: tests/gui_errors.sh <vídeo A corto> <vídeo L mp4 faststart> [out]
set -euo pipefail
VIDEO_A="$1"; VIDEO_L="$2"; OUT="${3:-/tmp/thumbsheet-gui-errors}"; rm -rf "$OUT"; mkdir -p "$OUT/video"
DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
export DISPLAY=:76
export XDG_CACHE_HOME="$OUT/cache" XDG_CONFIG_HOME="$OUT/config" THUMBSHEET_AUDIO=0 THUMBSHEET_WORKERS=4   # tope de ffmpeg fallidos reproducible
A1="$OUT/video/copia-$(basename "$VIDEO_A")"; A2="$OUT/video/final-$(basename "$VIDEO_A")"; cp -f "$VIDEO_A" "$A1"; cp -f "$VIDEO_A" "$A2"
ZERO="$OUT/video/ceros.mp4"; TEXT="$OUT/video/texto.mp4"
python3 - "$VIDEO_L" "$ZERO" <<'PY'
import struct, sys, pathlib
data = bytearray(pathlib.Path(sys.argv[1]).read_bytes()); off = 0
while off + 8 <= len(data):
    size, typ = struct.unpack(">I4s", data[off:off + 8])
    if size == 1: size = struct.unpack(">Q", data[off + 8:off + 16])[0]
    if size == 0: size = len(data) - off
    if typ == b"mdat": data[off + 8:off + size] = b"\0" * (size - 8); break
    off += size
else: sys.exit("sin mdat")
pathlib.Path(sys.argv[2]).write_bytes(data)
PY
printf 'esto no es un vídeo\n%.0s' $(seq 1 100) >"$TEXT"
Xvfb :76 -screen 0 1400x900x24 -nolisten tcp >/dev/null 2>&1 &
XPID=$!
trap 'kill $APP 2>/dev/null || true; kill $XPID 2>/dev/null || true' EXIT
sleep 1
THUMBSHEET_DEBUG=1 python3 "$DIR/thumbsheet.py" "$A1" "$ZERO" "$TEXT" "$A2" >"$OUT/app.log" 2>&1 &
APP=$!
for i in $(seq 1 40); do WID=$(xdotool search --onlyvisible --classname thumbsheet 2>/dev/null | head -1 || true); [[ -n "$WID" ]] && break; sleep 0.25; done
[[ -n "${WID:-}" ]] || { echo "no aparece la ventana"; cat "$OUT/app.log"; exit 1; }
xdotool windowsize "$WID" 1400 900; xdotool windowmove "$WID" 0 0; sleep 0.3
eval "$(xdotool getwindowgeometry --shell "$WID")"
shot() { sleep "${2:-0.8}"; import -window root "$OUT/$1.png"; echo "captura $1"; }
fail=0; ok() { echo "ok   $1"; }; ko() { echo "FAIL $1"; fail=1; }
L="$OUT/app.log"; Z=$(basename "$ZERO"); TX=$(basename "$TEXT"); F=$(basename "$A2")

# 1. el segundo plano llega hasta la última copia pasando por encima de los dos rotos
for i in $(seq 1 240); do grep -q "fin: .*\[$F\]" "$L" && break; sleep 0.5; done
shot 1-panel 1
grep -q "sondeo: $TX: no se pudo abrir" "$L" && ok "texto.mp4: el sondeo lo deja en error ($(grep "sondeo: $TX" "$L" | sed 's/.*abrir: //' | head -1))" || ko "texto.mp4 sin error de sondeo"
grep -q "estado: $TX → error" "$L" && ok "texto.mp4: icono de estado → error (aspa)" || ko "texto.mp4: sin estado error: $(grep "estado: $TX" "$L" | tail -1)"
grep -q "fin: sin fotogramas, se desiste.*\[$Z\]" "$L" && ok "ceros.mp4: el generador desiste ($(grep "se desiste.*\[$Z\]" "$L" | head -1 | sed 's/.*desiste tras //; s/\[.*//'))" || ko "ceros.mp4: no desistió: $(grep "$Z" "$L" | tail -2 | tr '\n' ';')"
grep -q "estado: $Z → error" "$L" && ok "ceros.mp4: icono de estado → error (aspa)" || ko "ceros.mp4: sin estado error: $(grep "estado: $Z" "$L" | tail -1)"
grep -q "segundo plano: $F" "$L" && grep -q "fin: .*\[$F\]" "$L" && ok "el segundo plano siguió con el siguiente y lo terminó ($F)" || ko "el segundo plano no llegó a $F: $(grep 'segundo plano' "$L" | tail -3 | tr '\n' ';')"
NF=$(find "$OUT/cache" -name "*.fail" | wc -l); [[ "$NF" == 0 ]] && ok "sin marcas .fail en la caché" || ko "quedan $NF marcas .fail"
NR=$(grep -c "ffmpeg rc=" "$L" || true); [[ "$NR" -le 16 ]] && ok "pocos ffmpeg fallidos antes de desistir ($NR)" || ko "demasiados ffmpeg fallidos: $NR"
grep -q "archivos: 4" "$L" && ok "cabecera del panel: 4 archivos" || ko "cabecera: $(grep -o 'archivos: [0-9]*' "$L" | tail -1)"

# 2. pinchar en el vídeo a ceros: se reintenta a la vista, vuelve a error (toast rojo, aspas) y el resto sigue
G=$(grep "geometry:" "$L" | tail -1); ROW2=$(echo "$G" | sed -E 's/.*rows=//' | cut -d';' -f2)
xdotool mousemove $((X+${ROW2%,*})) $((Y+${ROW2#*,})) click 1
for i in $(seq 1 120); do [[ "$(grep -c "se desiste.*\[$Z\]" "$L" || true)" -ge 2 ]] && break; sleep 0.5; done
shot 2-ceros-a-la-vista 1.5
grep -q "vídeo: $Z" "$L" && ok "ceros.mp4 abierto a la vista" || ko "no se abrió ceros.mp4: $(grep 'vídeo:' "$L" | tail -1)"
[[ "$(grep -c "se desiste.*\[$Z\]" "$L" || true)" -ge 2 ]] && ok "a la vista se reintenta y vuelve a desistir" || ko "no se reintentó: $(grep -c "se desiste.*\[$Z\]" "$L" || true) veces"
grep -q "toast error: No se pudieron sacar fotogramas" "$L" && ok "toast rojo con el motivo" || ko "sin toast de error: $(grep 'toast' "$L" | tail -1)"
[[ "$(grep -c "estado: $Z → error" "$L" || true)" -ge 2 ]] && ok "icono: generando → error otra vez" || ko "icono tras reintentar: $(grep "estado: $Z" "$L" | tail -2 | tr '\n' ';')"
NF=$(find "$OUT/cache" -name "*.fail" | wc -l); [[ "$NF" == 0 ]] && ok "sigue sin marcas .fail" || ko "quedan $NF marcas .fail tras reintentar"
sleep 1; grep -q "segundo plano: espera" "$L" && tail -3 "$L" | grep -q "segundo plano: espera" && ko "el segundo plano sigue esperando: $(tail -1 "$L")" || ok "el segundo plano no se queda esperando por el vídeo en error"

# 3. cierre
xdotool key ctrl+q; sleep 1
if kill -0 $APP 2>/dev/null; then ko "la app no cerró con Ctrl+Q"; kill $APP; sleep 1; else ok "cierre con Ctrl+Q"; fi
left=$(pgrep -c -x ffmpeg || true); [[ "$left" == 0 ]] && ok "sin ffmpeg tras cerrar" || ko "ffmpeg vivos tras cerrar: $left"
echo "--- log"; grep -v "geometry" "$L" | grep -i "error\|desiste\|sondeo\|segundo plano:" | tail -12 | cut -c1-170
echo "RESULTADO: $([[ $fail == 0 ]] && echo OK || echo FALLO)"; exit $fail
