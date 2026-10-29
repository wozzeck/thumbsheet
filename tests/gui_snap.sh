#!/usr/bin/env bash
# Humo de la GUI en un Xvfb propio (:69): toggle «Snap scroll» (y la barra sin LLC). Abre una COPIA de A con
# intervalo de 10 s, para que haya recorrido de scroll.
#  1. Sin snap, la rueda desplaza píxeles sueltos (la primera fila queda a medias).
#  2. Al activar el toggle, el visor salta a la parada más cercana: una fila entera arriba, en el mismo sitio que la
#     primera con el visor a cero (desplazamiento múltiplo de alto+hueco); el contenido gana el relleno justo para
#     que la última parada sea el tope del scroll (menos de una fila).
#  3. Con snap, la rueda baja y sube filas enteras; arrastrando la barra de scroll sólo se para en filas enteras; al
#     final se ve la última fila entera y la rejilla sigue alineada.
#  4. Cambiar el tamaño (Ctrl+rueda) recentra y deja el visor en una parada de la rejilla nueva.
#  5. El toggle se guarda: al reabrir sigue activo; al desactivarlo, la rueda vuelve a ir por píxeles.
# Uso: tests/gui_snap.sh <vídeo A, de unos minutos o más> [out]
set -euo pipefail
VIDEO_A="$1"; OUT="${2:-/tmp/thumbsheet-gui-snap}"; rm -rf "$OUT"; mkdir -p "$OUT/video" "$OUT/config/thumbsheet"
DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
export DISPLAY=:69
export XDG_CACHE_HOME="$OUT/cache" XDG_CONFIG_HOME="$OUT/config" THUMBSHEET_AUDIO=0
V="$OUT/video/snap-$(basename "$VIDEO_A")"; cp -f "$VIDEO_A" "$V"
echo '{"interval": 10, "cols": 6}' > "$OUT/config/thumbsheet/settings.json"
Xvfb :69 -screen 0 1400x900x24 -nolisten tcp >/dev/null 2>&1 &
XPID=$!
trap 'kill $APP 2>/dev/null || true; kill $XPID 2>/dev/null || true' EXIT
sleep 1
fail=0; ok() { echo "ok   $1"; }; ko() { echo "FAIL $1"; fail=1; }
LOG=app.log
waitfor() { local i; for i in $(seq 1 $((${2:-20}*4))); do grep -q "$1" "$OUT/$LOG" && return 0; sleep 0.25; done; return 1; }
start() {
  THUMBSHEET_DEBUG=1 python3 "$DIR/thumbsheet.py" "$V" >"$OUT/$LOG" 2>&1 &
  APP=$!
  for i in $(seq 1 40); do WID=$(xdotool search --onlyvisible --classname thumbsheet 2>/dev/null | head -1 || true); [[ -n "$WID" ]] && break; sleep 0.25; done
  [[ -n "${WID:-}" ]] || { echo "no aparece la ventana"; cat "$OUT/$LOG"; exit 1; }
  xdotool windowsize "$WID" 1400 900; xdotool windowmove "$WID" 0 0; sleep 0.3
  eval "$(xdotool getwindowgeometry --shell "$WID")"
  waitfor "fin: " 90 || { echo "no termina de generar"; tail -5 "$OUT/$LOG"; exit 1; }
  waitfor "geometry:" 20 || true; sleep 0.8
  G=$(grep "geometry:" "$OUT/$LOG" | tail -1 || true); [[ -n "$G" ]] || { echo "la app no publicó su geometría"; exit 1; }
}
gv() { echo "$G" | sed -E "s/.* $1=([^ ]+).*/\1/"; }
# último desplazamiento: «valor fila paso» (fila y paso = '-' sin snap)
vs() { python3 - "$OUT/$LOG" <<'PY'
import re, sys
lines = [l for l in open(sys.argv[1], encoding="utf-8", errors="replace") if "vscroll: " in l]
if not lines:
    print("0 - -"); sys.exit()
m = re.search(r"vscroll: ([0-9.]+)(?: -> ([0-9]+) \(snap, fila ([0-9]+), paso ([0-9a-z]+)\))?", lines[-1])
raw, snapped, row, paso = m.groups()
print(snapped if snapped is not None else raw, row or "-", paso or "-")
PY
}
val() { vs | cut -d' ' -f1; }
paso() { vs | cut -d' ' -f3; }
mult() { python3 -c "import sys; v=float(sys.argv[1]); p=float(sys.argv[2]); r=v % p; sys.exit(0 if min(r, p-r) < 0.5 else 1)" "$1" "$2"; }
alto() { grep -o "snap: alto [0-9]* (contenido [0-9]*, visor [0-9]*)" "$OUT/$LOG" | tail -1 | tr -dc '0-9 ' | xargs; }   # «H C P»

start
SX=$(gv sheet | cut -d, -f1); SY=$(gv sheet | cut -d, -f2); CH=$(gv cell | cut -dx -f2); GAP=$(gv gap); PITCH=$((CH+GAP))
SNX=$(gv snap | cut -d, -f1); SNY=$(gv snap | cut -d, -f2)
VBX=$(gv vbar | cut -d, -f1); VBY=$(gv vbar | cut -d, -f2); VBH=$(gv vbar | cut -d, -f3)
echo "alto de fila + hueco: $PITCH px; barra de scroll en x=$VBX, y=$VBY, alto $VBH"
[[ "$G" != *" llc="* && -n "$SNX" ]] && ok "la barra ya no tiene LLC y sí el toggle Snap scroll" || ko "geometría inesperada: $G"
import -window root -crop 1400x60+0+0 "$OUT/0-barra.png"
grep -q "snap: activado" "$OUT/$LOG" && ko "el snap arranca activado sin haberlo pedido" || ok "el snap arranca desactivado"

# 1. sin snap: la rueda va por píxeles
xdotool mousemove $((X+SX+400)) $((Y+SY+300)); sleep 0.3
for i in 1 2 3; do xdotool click 5; sleep 0.15; done; sleep 0.6
V0=$(val); mult "$V0" "$PITCH" && ko "sin snap la rueda ya deja filas enteras (v=$V0): la prueba no distingue" || ok "sin snap, la rueda desplaza píxeles: v=$V0, no múltiplo de $PITCH"
shot0() { import -window root "$OUT/$1.png"; }
shot0 1-sin-snap

# 2. activar el toggle: parada más cercana
xdotool mousemove $((X+SNX)) $((Y+SNY)) click 1; sleep 1
waitfor "snap: activado" 5 && ok "el toggle activa el snap" || ko "el toggle no activó el snap"
V1=$(val); EXP=$(python3 -c "import sys; v=float(sys.argv[1]); p=int(sys.argv[2]); print(int(round(v/p))*p)" "$V0" "$PITCH")
[[ "$V1" == "$EXP" && "$(paso)" == "$PITCH" ]] && ok "al activarlo salta a la fila entera más cercana: $V0 → $V1 (fila $(vs | cut -d' ' -f2))" || ko "al activar: v=$V1 (esperado $EXP), paso $(paso)"
read -r H C P <<< "$(alto)"
[[ -n "${H:-}" ]] && python3 -c "import sys; H,C,P,p=map(int, sys.argv[1:]); sys.exit(0 if H>=C and H-C<p and (H-P)%p==0 else 1)" "$H" "$C" "$P" "$PITCH" \
  && ok "relleno final de $((H-C)) px (menos de una fila): el tope del scroll ($((H-P))) es una parada" || ko "alto con snap: '$(alto)' (paso $PITCH)"
shot0 2-snap-activado

# 3. rueda con snap: filas enteras
xdotool mousemove $((X+SX+400)) $((Y+SY+300)); sleep 0.3
bad=""; prev=$(val); seq_v=""
for i in 1 2 3 4; do xdotool click 5; sleep 0.4; v=$(val); seq_v="$seq_v $v"; mult "$v" "$PITCH" && [[ "$v" -gt "$prev" ]] || bad="$bad $v"; prev=$v; done
[[ -z "$bad" ]] && ok "con snap, cada paso de rueda baja filas enteras:$seq_v" || ko "rueda abajo con snap:$seq_v (mal:$bad)"
bad=""; seq_v=""
for i in 1 2; do xdotool click 4; sleep 0.4; v=$(val); seq_v="$seq_v $v"; mult "$v" "$PITCH" && [[ "$v" -lt "$prev" ]] || bad="$bad $v"; prev=$v; done
[[ -z "$bad" ]] && ok "y sube filas enteras:$seq_v" || ko "rueda arriba con snap:$seq_v (mal:$bad)"
shot0 3-snap-rueda

# barra de scroll: arrastrar el asa
NV=$(grep -c "vscroll: " "$OUT/$LOG" || true)
xdotool mousemove $((X+VBX)) $((Y+VBY+30)); sleep 0.3; xdotool mousedown 1; sleep 0.3
bad=""; seen=""
for i in $(seq 1 8); do xdotool mousemove $((X+VBX)) $((Y+VBY+30+i*25)); sleep 0.25; v=$(val); seen="$seen $v"; mult "$v" "$PITCH" || bad="$bad $v"; done
xdotool mouseup 1; sleep 0.5
moved=$(( $(grep -c "vscroll: " "$OUT/$LOG" || true) - NV ))
[[ -z "$bad" && "$moved" -gt 0 && "$(echo $seen | tr ' ' '\n' | sort -u | wc -l)" -gt 1 ]] && ok "arrastrando la barra sólo se para en filas enteras:$seen" || ko "arrastre de la barra:$seen (mal:$bad, eventos $moved)"
shot0 4-snap-barra

# final del contenido
xdotool mousemove $((X+SX+400)) $((Y+SY+300)); sleep 0.2
for i in $(seq 1 40); do xdotool click 5; done; sleep 0.8
read -r H C P <<< "$(alto)"; VE=$(val)
[[ -n "${H:-}" && "$VE" == "$((H-P))" ]] && mult "$VE" "$PITCH" && [[ $((VE+P)) -ge "$C" ]] \
  && ok "al final: v=$VE es el tope, múltiplo de $PITCH, y se ve el final del contenido ($((VE+P)) ≥ $C)" || ko "al final: v=$VE, alto '$(alto)', paso $PITCH"
shot0 5-snap-final

# 4. cambiar el tamaño con snap: recentra en una parada de la rejilla nueva
NC=$(grep -c "cols: " "$OUT/$LOG" || true)
for i in $(seq 1 10); do xdotool click 4; done; sleep 0.5   # a media altura
xdotool keydown ctrl click 5 keyup ctrl
for i in $(seq 1 20); do [[ "$(grep -c "cols: " "$OUT/$LOG" || true)" -gt "$NC" ]] && break; sleep 0.25; done; sleep 1.5
P2=$(paso); V2=$(val)
[[ "$P2" =~ ^[0-9]+$ && "$P2" != "$PITCH" ]] && mult "$V2" "$P2" && ok "Ctrl+rueda (más por fila): recentra en una parada de la rejilla nueva (v=$V2, paso $P2)" || ko "tras cambiar el tamaño: v=$V2, paso $P2 (antes $PITCH)"

# 5. se guarda; al desactivarlo vuelve la rueda por píxeles
xdotool key ctrl+q; sleep 1.2; kill $APP 2>/dev/null || true
python3 -c "import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get('snap') is True else 1)" "$OUT/config/thumbsheet/settings.json" && ok "el toggle se guarda en los ajustes" || ko "ajustes: $(cat "$OUT/config/thumbsheet/settings.json")"
LOG=app2.log; start
grep -q "snap: activado" "$OUT/$LOG" && ok "al reabrir, Snap scroll sigue activo" || ko "al reabrir no está activo"
SNX=$(gv snap | cut -d, -f1); SNY=$(gv snap | cut -d, -f2); CH=$(gv cell | cut -dx -f2); PITCH=$((CH+GAP))
xdotool mousemove $((X+SNX)) $((Y+SNY)) click 1; sleep 0.8
waitfor "snap: desactivado" 5 && ok "el toggle lo desactiva" || ko "el toggle no lo desactivó"
xdotool mousemove $((X+SX+400)) $((Y+SY+300)); sleep 0.3
for i in 1 2 3; do xdotool click 5; sleep 0.15; done; sleep 0.6
V3=$(val); mult "$V3" "$PITCH" && ko "desactivado, la rueda sigue dejando filas enteras (v=$V3)" || ok "desactivado, la rueda vuelve a ir por píxeles: v=$V3"

xdotool key ctrl+q; sleep 1; kill $APP 2>/dev/null || true
grep -q "Traceback" "$OUT/app.log" "$OUT/app2.log" && ko "traceback en el log" || ok "sin tracebacks"
left=$(pgrep -c -x ffmpeg || true); [[ "$left" == 0 ]] && ok "sin ffmpeg tras cerrar" || ko "ffmpeg vivos tras cerrar: $left"
[[ -f "$VIDEO_A" ]] || { echo "¡¡el vídeo ORIGINAL ha desaparecido!!"; fail=1; }
[[ $fail == 0 ]] && echo "TODO OK" || { echo "HAY FALLOS"; exit 1; }
