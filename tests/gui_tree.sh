#!/usr/bin/env bash
# Humo de la GUI en un Xvfb propio: el panel anidado por carpetas, el ojo y la vigilancia. Monta un árbol con copias
# de A (dos vídeos y un -cortado en la raíz, uno en sub/, un -cortado solo en sub/done/ y una carpeta vacía), arranca la
# app con la carpeta y comprueba: el árbol del panel (carpetas vacías fuera), el ojo (oculta los -cortado, esconde la
# subcarpeta que se queda sin nada a la vista, el recuento), que al borrar se pasa al siguiente vídeo a la vista
# saltando la fila de carpeta y los ocultos, las flechas igual, la vigilancia (vídeo nuevo en la carpeta vacía, en la
# subcarpeta escondida —que reaparece— y una subcarpeta nueva movida con un vídeo dentro; todos se sondean y se
# generan en segundo plano; el -cortado que escribe la propia app NO se carga), que al desactivarla ya no entra nada,
# que ojo y vigilancia se guardan por carpeta y se restauran al reabrir. Uso: tests/gui_tree.sh <vídeo A corto> [out]
set -euo pipefail
VIDEO_A="$1"; OUT="${2:-/tmp/thumbsheet-gui-tree}"; rm -rf "$OUT"; mkdir -p "$OUT/arbol/sub/done" "$OUT/arbol/vacia" "$OUT/config/thumbsheet"
DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
export DISPLAY=:68
export XDG_CACHE_HOME="$OUT/cache" XDG_CONFIG_HOME="$OUT/config" THUMBSHEET_AUDIO=0
for rel in a1.mp4 a2.mp4 a2-cortado.mp4 sub/b1.mkv sub/done/c-cortado.mp4; do cp -f "$VIDEO_A" "$OUT/arbol/$rel"; done
echo '{"interval": 2, "cols": 6}' > "$OUT/config/thumbsheet/settings.json"   # intervalo corto: varias teselas para poder cortar
Xvfb :68 -screen 0 1400x900x24 -nolisten tcp >/dev/null 2>&1 &
XPID=$!
trap 'kill $APP 2>/dev/null || true; kill $XPID 2>/dev/null || true' EXIT
sleep 1
L="$OUT/app.log"
THUMBSHEET_DEBUG=1 python3 "$DIR/thumbsheet.py" "$OUT/arbol" >"$L" 2>&1 &
APP=$!
for i in $(seq 1 40); do WID=$(xdotool search --onlyvisible --classname thumbsheet 2>/dev/null | head -1 || true); [[ -n "$WID" ]] && break; sleep 0.25; done
[[ -n "${WID:-}" ]] || { echo "no aparece la ventana"; cat "$L"; exit 1; }
xdotool windowsize "$WID" 1400 900; xdotool windowmove "$WID" 0 0; sleep 0.3
eval "$(xdotool getwindowgeometry --shell "$WID")"   # X Y WIDTH HEIGHT
fail=0; ok() { echo "ok   $1"; }; ko() { echo "FAIL $1"; fail=1; }
waitfor() { local i; for i in $(seq 1 "${2:-40}"); do grep -q "$1" "$L" && return 0; sleep 0.25; done; return 1; }
last() { grep "\] $1" "$L" | tail -1 | sed 's/^\[[^]]*\] //'; }   # «] panel:» y no «segpanel:»
away() { xdotool mousemove $((X+700)) $((Y+500)); sleep 0.3; }   # el ratón fuera: el tooltip del botón taparía las filas
geom() { sleep 0.8; G=$(grep "geometry:" "$L" | tail -1 || true); }
gv() { echo "$G" | sed -E "s/.* $1=([^ ]+).*/\1/"; }
rowxy() { echo "$G" | sed -E 's/.*rows=//' | cut -d';' -f"$1"; }
click_row() { local R; R=$(rowxy "$1"); xdotool mousemove $((X+${R%,*})) $((Y+${R#*,})) click 1; sleep 0.6; }

# 1. el árbol: raíz con sus vídeos, luego las subcarpetas con vídeos; la vacía no sale
waitfor "carpetas: 5 vídeos" || ko "no termina el recorrido de la carpeta"
sleep 0.5
P=$(last "panel:")
[[ "$P" == "panel: arbol: a1.mp4, a2-cortado.mp4, a2.mp4 | sub: b1.mkv | sub/done: c-cortado.mp4" ]] && ok "panel anidado: raíz, sus vídeos, sub y sub/done; la carpeta vacía no sale" || ko "panel: '$P'"
grep -q "archivos: 5$" "$L" && ok "cabecera: 5 archivos, ninguno oculto" || ko "cabecera: $(last 'archivos:')"
waitfor "geometry:" 20 || ko "sin geometría"
geom
EX=$(gv eye); EY=${EX#*,}; EX=${EX%,*}; WX=$(gv watch); WY=${WX#*,}; WX=${WX%,*}
N=$(echo "$G" | sed -E 's/.*rows=//' | tr ';' '\n' | grep -c .)
[[ "$N" == 5 ]] && ok "geometría: 5 filas de vídeo a la vista (las de carpeta no cuentan)" || ko "filas en geometría: $N"
sleep 1; import -window root "$OUT/1-arbol.png"

# 2. el ojo: oculta los -cortado, esconde sub/done (sin nada a la vista) y lo dice la cabecera
xdotool mousemove $((X+EX)) $((Y+EY)) click 1; sleep 0.8; away
grep -q "ojo: arbol: ocultar cortados (2 ocultos)" "$L" && ok "ojo: 2 ocultos" || ko "ojo: $(last 'ojo:')"
P=$(last "panel:")
[[ "$P" == "panel: arbol: a1.mp4, ~a2-cortado.mp4, a2.mp4 | sub: b1.mkv | ~sub/done: ~c-cortado.mp4" ]] && ok "panel con ojo: a2-cortado oculto y sub/done escondida entera" || ko "panel con ojo: '$P'"
grep -q "archivos: 5 ocultos: 2" "$L" && ok "cabecera: 5 archivos · 2 ocultos" || ko "cabecera con ojo: $(last 'archivos:')"
geom; N=$(echo "$G" | sed -E 's/.*rows=//' | tr ';' '\n' | grep -c .)
[[ "$N" == 3 ]] && ok "geometría: 3 filas a la vista" || ko "filas a la vista con ojo: $N"
import -window root "$OUT/2-ojo.png"

# 3. flechas: de vídeo a la vista en vídeo a la vista, saltando la fila de carpeta y el oculto de un solo paso
click_row 2; [[ "$(last 'vídeo:')" == "vídeo: a2.mp4"* ]] && ok "clic en la 2.ª fila: a2.mp4" || ko "clic fila 2: $(last 'vídeo:')"
xdotool key Down; sleep 0.6; [[ "$(last 'vídeo:')" == "vídeo: b1.mkv"* ]] && ok "flecha abajo desde a2: b1.mkv (salta el oculto y la fila sub)" || ko "abajo: $(last 'vídeo:')"
xdotool key Up; sleep 0.6; [[ "$(last 'vídeo:')" == "vídeo: a2.mp4"* ]] && ok "flecha arriba desde b1: a2.mp4" || ko "arriba: $(last 'vídeo:')"
xdotool key Home; sleep 0.6; [[ "$(last 'vídeo:')" == "vídeo: a1.mp4"* ]] && ok "Inicio: a1.mp4" || ko "Inicio: $(last 'vídeo:')"
xdotool key End; sleep 0.6; [[ "$(last 'vídeo:')" == "vídeo: b1.mkv"* ]] && ok "Fin: b1.mkv" || ko "Fin: $(last 'vídeo:')"
# clic en la fila de carpeta: no selecciona nada
xdotool mousemove $((X+EX-120)) $((Y+EY)) click 1; sleep 0.6
[[ "$(last 'vídeo:')" == "vídeo: b1.mkv"* ]] && ok "clic en la fila de carpeta: sigue b1.mkv" || ko "clic en carpeta: $(last 'vídeo:')"

# 4. borrar a2 (último a la vista de la raíz): se pasa a b1.mkv, saltando el oculto y la fila sub
click_row 2; sleep 0.3
xdotool key Delete; sleep 0.8; xdotool key Return
waitfor "eliminado: a2.mp4" 40 && ok "Supr + Intro: a2.mp4 borrado" || ko "no se borró a2: $(grep 'eliminar\|eliminando\|eliminado' "$L" | tail -2 | tr '\n' ' ')"
sleep 0.5
[[ "$(last 'vídeo:')" == "vídeo: b1.mkv"* ]] && ok "tras borrar a2: a la vista b1.mkv" || ko "tras borrar: $(last 'vídeo:')"
P=$(last "panel:")
[[ "$P" == "panel: arbol: a1.mp4, ~a2-cortado.mp4 | sub: b1.mkv | ~sub/done: ~c-cortado.mp4" ]] && ok "panel tras borrar" || ko "panel tras borrar: '$P'"

# 5. vigilancia: al activarla entra lo que ya existía sin cargar; después, vídeo nuevo en la carpeta vacía, en la
#    subcarpeta escondida (reaparece) y una subcarpeta nueva movida con un vídeo dentro
cp "$VIDEO_A" "$OUT/arbol/sub/pre.mp4"   # existe antes de activar y no está cargado
xdotool mousemove $((X+WX)) $((Y+WY)) click 1; sleep 0.8; away
grep -q "vigilar: arbol: activada (4 carpetas)" "$L" && ok "vigilancia activada: 4 carpetas (raíz, sub, sub/done, vacia)" || ko "vigilar: $(last 'vigilar:')"
waitfor "vigilar: ya estaba sub/pre.mp4" 40 && ok "al activar: pre.mp4 (existía sin cargar) entra" || ko "pre.mp4 no entró al activar: $(grep 'vigilar' "$L" | tail -2 | tr '\n' ' ')"
sleep 0.5; P=$(last "panel:")
[[ "$P" == *"| sub: b1.mkv, pre.mp4 |"* ]] && ok "panel: pre.mp4 detrás de b1 en sub" || ko "panel con pre: '$P'"
cp "$VIDEO_A" "$OUT/arbol/vacia/n1.mp4"
waitfor "vigilar: nuevo vídeo vacia/n1.mp4" 40 && ok "vigilancia: n1.mp4 aparece en vacia/ y se carga" || ko "n1 no se cargó: $(grep 'vigilar' "$L" | tail -2 | tr '\n' ' ')"
sleep 0.5; P=$(last "panel:")
[[ "$P" == *"| vacia: n1.mp4" ]] && ok "panel: la carpeta vacia ya sale, con n1" || ko "panel con n1: '$P'"
waitfor "segundo plano: n1.mp4" 60 && ok "n1 se sondea y se genera en segundo plano" || ko "n1 sin segundo plano: $(grep 'segundo plano\|sondeo' "$L" | tail -3 | tr '\n' ' ')"
cp "$VIDEO_A" "$OUT/arbol/sub/done/z.mp4"
waitfor "vigilar: nuevo vídeo sub/done/z.mp4" 40 && ok "vigilancia: z.mp4 en sub/done/" || ko "z no se cargó"
sleep 0.5; P=$(last "panel:")
[[ "$P" == *"| sub/done: ~c-cortado.mp4, z.mp4 |"* ]] && ok "panel: sub/done reaparece con z (c-cortado sigue oculto)" || ko "panel con z: '$P'"
mkdir "$OUT/nueva"; cp "$VIDEO_A" "$OUT/nueva/m.mp4"; mv "$OUT/nueva" "$OUT/arbol/nueva"
waitfor "vigilar: arbol: carpeta nueva nueva (5 carpetas)" 40 && ok "subcarpeta nueva movida: se vigila (5 carpetas)" || ko "carpeta nueva: $(grep 'vigilar' "$L" | tail -2 | tr '\n' ' ')"
waitfor "vigilar: nuevo vídeo nueva/m.mp4" 40 && ok "y su vídeo m.mp4 se carga" || ko "m no se cargó"
for i in $(seq 1 120); do [[ "$(grep -c "fin: .*\[\(pre\|n1\|z\|m\).mp4\]" "$L" || true)" -ge 4 ]] && break; sleep 0.5; done
[[ "$(grep -c "fin: .*\[\(pre\|n1\|z\|m\).mp4\]" "$L" || true)" -ge 4 ]] && ok "los cuatro que entran por la vigilancia se generan en segundo plano" || ko "generados de los nuevos: $(grep -o 'fin: .*\[\(pre\|n1\|z\|m\).mp4\]' "$L" | tr '\n' ' ')"
import -window root "$OUT/3-vigilancia.png"

# 6. el -cortado que escribe la propia app no se carga aunque la carpeta esté vigilada
geom; click_row 1; [[ "$(last 'vídeo:')" == "vídeo: a1.mp4"* ]] && ok "a la vista a1.mp4" || ko "fila 1: $(last 'vídeo:')"
SX=$(gv sheet); SY=${SX#*,}; SX=${SX%,*}; CW=$(gv cell); CH=${CW#*x}; CW=${CW%x*}; PAD=$(gv pad)
xdotool mousemove $((X+SX+PAD+CW/2)) $((Y+SY+PAD+CH/2)) click 1; sleep 0.6   # primera tesela: un segmento
xdotool key c; sleep 1; xdotool key Return
waitfor "cut: original borrado a1.mp4" 60 && ok "C + Intro: a1 cortado y borrado" || ko "corte: $(grep 'cut:' "$L" | tail -2 | tr '\n' ' ')"
sleep 4
ls "$OUT/arbol"/a1-cortado.* >/dev/null 2>&1 && ok "a1-cortado existe en la carpeta vigilada" || ko "no hay a1-cortado"
grep -q "vigilar: nuevo vídeo a1-cortado" "$L" && ko "la vigilancia cargó el -cortado de la propia app" || ok "la vigilancia no carga el -cortado de la propia app"
P=$(last "panel:"); [[ "$P" != *"a1-cortado"* ]] && ok "panel sin a1-cortado" || ko "panel: '$P'"
[[ "$(last 'vídeo:')" == "vídeo: m.mp4"* ]] && ok "tras el corte: a la vista m.mp4 (el siguiente a la vista: nueva/ va antes que sub/)" || ko "tras el corte: $(last 'vídeo:')"

# 7. vigilancia desactivada: ya no entra nada
xdotool mousemove $((X+WX)) $((Y+WY)) click 1; sleep 0.8; away
grep -q "vigilar: arbol: desactivada (5 carpetas)" "$L" && ok "vigilancia desactivada" || ko "desactivar: $(last 'vigilar:')"
cp "$VIDEO_A" "$OUT/arbol/n2.mp4"; sleep 4
grep -q "vigilar: nuevo vídeo n2.mp4" "$L" && ko "sin vigilancia se cargó n2" || ok "sin vigilancia no entra n2"
grep -q "Traceback" "$L" && ko "traceback en el log" || ok "sin tracebacks"
xdotool key ctrl+q; sleep 1.5
if kill -0 $APP 2>/dev/null; then ko "la app no cerró con Ctrl+Q"; kill $APP; sleep 1; else ok "cierre con Ctrl+Q"; fi
python3 - "$OUT/config/thumbsheet/settings.json" "$OUT/arbol" <<'EOF' && ok "settings: ojo guardado para la carpeta, vigilancia no (está apagada)" || ko "settings: $(cat "$OUT/config/thumbsheet/settings.json" | tr -d '\n ')"
import json, sys, os
s = json.load(open(sys.argv[1])); f = s.get("folders", {}).get(os.path.realpath(sys.argv[2]))
sys.exit(0 if f == {"hide_done": True, "watch": False} else 1)
EOF

# 8. reabrir: el ojo se restaura (a1-cortado, a2-cortado y c-cortado ocultos; n2 entra)
THUMBSHEET_DEBUG=1 python3 "$DIR/thumbsheet.py" "$OUT/arbol" >"$OUT/app2.log" 2>&1 &
APP=$!
L="$OUT/app2.log"
waitfor "carpetas: [0-9]* vídeos" 40 || ko "segunda sesión: no termina el recorrido"
sleep 1
P=$(last "panel:")
[[ "$P" == "panel: arbol: ~a1-cortado.mp4, ~a2-cortado.mp4, n2.mp4 | nueva: m.mp4 | sub: b1.mkv, pre.mp4 | sub/done: ~c-cortado.mp4, z.mp4 | vacia: n1.mp4" ]] && ok "al reabrir: ojo restaurado, 3 ocultos, el resto a la vista" || ko "al reabrir: '$P'"
grep -q "archivos: 9 ocultos: 3" "$L" && ok "cabecera al reabrir: 9 archivos · 3 ocultos" || ko "cabecera al reabrir: $(last 'archivos:')"
[[ "$(last 'vídeo:')" == "vídeo: n2.mp4"* ]] && ok "al reabrir se abre el primero a la vista (n2), no un oculto" || ko "al reabrir: $(last 'vídeo:')"
grep -q "Traceback" "$L" && ko "traceback en el log 2" || ok "sin tracebacks al reabrir"
xdotool key ctrl+q; sleep 1; kill $APP 2>/dev/null || true
left=$(pgrep -c -x ffmpeg || true); [[ "$left" == 0 ]] && ok "sin ffmpeg tras cerrar" || ko "ffmpeg vivos tras cerrar: $left"
[[ -f "$VIDEO_A" ]] || { echo "¡¡el vídeo ORIGINAL ha desaparecido!!"; fail=1; }
echo "--- log"; grep "ojo:\|vigilar:\|panel:" "$OUT/app.log" | tail -6 | cut -c1-170
[[ $fail == 0 ]] && echo "TODO OK" || { echo "HAY FALLOS"; exit 1; }
