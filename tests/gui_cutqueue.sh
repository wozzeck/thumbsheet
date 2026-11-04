#!/usr/bin/env bash
# Humo de la GUI en un Xvfb propio (:65): los cortes van a una cola en segundo plano. Cinco COPIAS de A (uno, dos,
# tres, cuatro, cinco).
#  1. C + flecha abajo (exacto) + Intro sobre uno: el diálogo se cierra al momento, se pasa a dos, uno sale de la lista
#     y abajo aparece el aviso «Cortando uno… (y luego se borra el original)» con Cancelar.
#  2. Con uno cortándose, C + Alt+T (sólo cortar) sobre dos: a la cola («en cola: dos»); dos no se puede eliminar
#     (Supr no abre nada) mientras su corte espera.
#  3. Cancelar en el aviso: se cancelan los dos, el ffmpeg muere en seguida, uno vuelve a la lista con su fichero y sin
#     restos (-cortado ni .part), toast «Corte cancelado», el aviso desaparece.
#  4. C + Intro (sin pérdida) sobre tres y Cancelar al instante (fase de keyframes o arranque de ffmpeg): también acaba
#     en seguida y tres vuelve a la lista, sin -cortado (antes, cancelar en esa fase dejaba el corte «cancelando» para
#     siempre; y un Cancelar que llegaba con el corte ya escrito seguía adelante y borraba el original).
#  5. Cola de verdad: C + Intro sobre uno y C + Alt+T sobre dos seguidos: se cortan los dos, en orden; el original de
#     uno se borra y el de dos se conserva; toasts verdes.
#  6. F = Full: abre su diálogo; Esc no toca nada; F + Intro renombra a -cortado.
#  7. Cerrar con trabajo pendiente: borrado programado de cinco y corte exacto de cuatro en marcha; Ctrl+Q avisa de
#     las dos cosas; Esc vuelve y el corte sigue; Ctrl+Q + Intro cierra: cinco se borra, el corte se cancela (cuatro
#     intacto, sin -cortado ni .part) y no queda ningún ffmpeg.
#  8. Sólo cortes: reabrir con cuatro, cortarlo y Ctrl+Q: el aviso habla sólo del corte; Intro («Cerrar») cierra y lo
#     cancela, sin restos.
# Uso: tests/gui_cutqueue.sh <vídeo A, de 5 minutos o más (a 30 s por tesela hacen falta 10; el corte exacto de
#      4 minutos tiene que durar lo bastante para encolar y cancelar)> [out]
set -euo pipefail
VIDEO_A="$1"; OUT="${2:-/tmp/thumbsheet-gui-cutqueue}"; rm -rf "$OUT"; mkdir -p "$OUT/video"
DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
export DISPLAY=:65
export XDG_CACHE_HOME="$OUT/cache" XDG_CONFIG_HOME="$OUT/config" THUMBSHEET_AUDIO=0 THUMBSHEET_DEBUG=1
EXT="${VIDEO_A##*.}"; K1="$OUT/video/uno.$EXT"; K2="$OUT/video/dos.$EXT"; K3="$OUT/video/tres.$EXT"; K4="$OUT/video/cuatro.$EXT"; K5="$OUT/video/cinco.$EXT"
for f in "$K1" "$K2" "$K3" "$K4" "$K5"; do cp -f "$VIDEO_A" "$f"; done
Xvfb :65 -screen 0 1400x900x24 -nolisten tcp >/dev/null 2>&1 &
XPID=$!
trap 'kill $APP 2>/dev/null || true; kill $XPID 2>/dev/null || true' EXIT
sleep 1
L="$OUT/app.log"
python3 "$DIR/thumbsheet.py" "$K1" "$K2" "$K3" "$K4" "$K5" >"$L" 2>&1 &
APP=$!
for i in $(seq 1 40); do WID=$(xdotool search --onlyvisible --classname thumbsheet 2>/dev/null | head -1 || true); [[ -n "$WID" ]] && break; sleep 0.25; done
[[ -n "${WID:-}" ]] || { echo "no aparece la ventana"; cat "$L"; exit 1; }
xdotool windowsize "$WID" 1400 900; xdotool windowmove "$WID" 0 0; sleep 0.3
eval "$(xdotool getwindowgeometry --shell "$WID")"
fail=0; ok() { echo "ok   $1"; }; ko() { echo "FAIL $1"; fail=1; }
shot() { sleep "${2:-0.5}"; import -window root "$OUT/$1.png"; }
n() { grep -c "$1" "$L" || true; }
last() { grep "$1" "$L" | tail -1 | sed 's/^\[[0-9:]*\] //'; }
shown() { grep "\] vídeo: " "$L" | tail -1 | sed 's/.*vídeo: //; s/ [0-9]*x[0-9]* .*//'; }
dlg() { xdotool search --onlyvisible --name "Cortar y unir" 2>/dev/null | head -1 || true; }
nwin() { xdotool search --onlyvisible --classname thumbsheet 2>/dev/null | wc -l; }
waitfor() { local i; for i in $(seq 1 $((${2:-20}*4))); do grep -q "$1" "$L" && return 0; sleep 0.25; done; return 1; }
waitn() { local i; for i in $(seq 1 $((${3:-20}*4))); do [[ "$(n "$1")" -ge "$2" ]] && return 0; sleep 0.25; done; return 1; }
ms() { python3 -c "import time; print(int(time.time()*1000))"; }
geom() { G=$(grep "geometry:" "$L" | tail -1); SX=$(gv sheet | cut -d, -f1); SY=$(gv sheet | cut -d, -f2); CW=$(gv cell | cut -dx -f1); CH=$(gv cell | cut -dx -f2); PAD=$(gv pad); GAP=$(gv gap); }
gv() { echo "$G" | sed -E "s/.* $1=([^ ]+).*/\1/"; }
tile() { local r=$1 c=$2; echo "$((X+SX+PAD+c*(CW+GAP)+CW/2)) $((Y+SY+PAD+r*(CH+GAP)+CH/2))"; }
open_cut() { xdotool key c; local i; for i in $(seq 1 20); do [[ -n "$(dlg)" ]] && return 0; sleep 0.1; done; return 1; }
rowc() { grep "geometry:" "$L" | tail -1 | sed -E 's/.* rows=([^ ]*).*/\1/' | cut -d';' -f"$1"; }
goto() { local c; c=$(rowc "$1"); xdotool mousemove $((X+${c%,*})) $((Y+${c#*,})) click 1; sleep 0.8; }   # n-ésima fila a la vista
cancel_cuts() { local C; C=$(grep "toast cortes: cancelar=" "$L" | tail -1 | sed 's/.*cancelar=//'); xdotool mousemove $((X+${C%,*})) $((Y+${C#*,})) click 1; }
waitfor "fin: .*\[uno\.$EXT\]" 120 || { echo "no termina de generar uno.$EXT"; tail -5 "$L"; exit 1; }
waitfor "geometry:" 20 || true; sleep 0.5; geom
[[ "$(shown)" == "uno.$EXT" ]] || { echo "a la vista debería estar uno.$EXT y está '$(shown)'"; exit 1; }

# 1. cortar y borrar uno (exacto, de la 3.ª tesela a la 10.ª: tarda bastantes segundos): el diálogo se va al momento
#    y se pasa a dos
xdotool mousemove $(tile 0 2) click 1; sleep 0.5
xdotool mousemove $(tile 1 3) keydown shift click 1 keyup shift; sleep 0.6
open_cut || ko "C no abrió el diálogo de corte"
xdotool key Down; sleep 0.3                    # «Exacto al fotograma»
T0=$(ms); xdotool key Return
for i in $(seq 1 100); do [[ -z "$(dlg)" ]] && break; sleep 0.02; done; T1=$(ms)
[[ -z "$(dlg)" && $((T1-T0)) -lt 1000 ]] && ok "Intro: el diálogo se cierra al momento ($((T1-T0)) ms; el corte va a la cola)" || ko "el diálogo sigue abierto a los $((T1-T0)) ms"
for i in $(seq 1 40); do [[ "$(shown)" == "dos.$EXT" ]] && break; sleep 0.05; done; T2=$(ms)
[[ "$(shown)" == "dos.$EXT" ]] && ok "se pasa a dos.$EXT en seguida ($((T2-T0)) ms)" || ko "a la vista: '$(shown)'"
grep -q "cortar: en cola uno.$EXT y borrar original (0 por delante)" "$L" && ok "uno a la cola, el primero" || ko "sin «en cola»: $(last 'cortar:')"
waitfor "toast cortes: cancelar=" 5 && ok "aviso abajo: $(last 'toast cortes: Cortando')" || ko "sin aviso de cortes: $(last 'toast cortes')"
[[ "$(last 'toast cortes: Cortando')" == "toast cortes: Cortando uno.$EXT (y luego se borra el original)" ]] && ok "el aviso dice qué se corta y que luego se borra" || ko "texto del aviso: '$(last 'toast cortes: Cortando')'"
[[ "$(last '\] panel:')" == *"-uno.$EXT, dos.$EXT, tres.$EXT"* ]] && ok "panel: uno fuera de la lista (-) mientras se corta" || ko "panel: '$(last '\] panel:')'"
[[ "$(last 'archivos:')" == "archivos: 4" ]] && ok "cabecera: 4 archivos" || ko "cabecera: '$(last 'archivos:')'"
shot 1-cortando 0.2

# 2. dos, sólo cortar, a la cola detrás de uno; mientras espera, Supr no hace nada
waitfor "fin: .*\[dos\.$EXT\]" 60 || true; sleep 0.3
xdotool mousemove $(tile 0 2) click 1; sleep 0.6
open_cut || ko "C no abrió el diálogo sobre dos"
xdotool key alt+t; sleep 0.6
grep -q "cortar: en cola dos.$EXT (1 por delante)" "$L" && ok "dos (sólo cortar) a la cola, detrás de uno" || ko "dos no entró en la cola: $(last 'cortar:')"
waitfor "toast cortes: Cortando uno.$EXT (y luego se borra el original) · en cola: dos.$EXT" 3 && ok "el aviso lo dice: «· en cola: dos.$EXT»" || ko "aviso: $(last 'toast cortes: ')"
[[ "$(shown)" == "dos.$EXT" ]] && ok "sólo cortar: se sigue en dos" || ko "a la vista: '$(shown)'"
ND=$(n "eliminar: diálogo"); xdotool key Delete; sleep 0.8
[[ "$(n 'eliminar: diálogo')" == "$ND" && "$(nwin)" == 1 ]] && ok "con su corte en la cola, Supr no abre Eliminar sobre dos" || { ko "Supr abrió Eliminar sobre dos"; xdotool key Escape; }
grep -q "cut: hecho\|cut: error" "$L" && ko "el corte exacto terminó antes de poder cancelarlo (A demasiado corto)" || ok "uno sigue cortándose"

# 3. Cancelar: los dos fuera, en seguida
T0=$(ms); cancel_cuts
waitfor "cut: cancelado" 10; T1=$(ms)
grep -q "cortar: cancelado en cola dos.$EXT" "$L" && ok "Cancelar quita dos de la cola" || ko "dos no salió de la cola: $(last 'cortar:')"
grep -q "cortar: cancelando uno.$EXT" "$L" && grep -q "cut: cancelado" "$L" && [[ $((T1-T0)) -lt 3000 ]] && ok "Cancelar detiene el corte de uno en $((T1-T0)) ms" || ko "el corte no se canceló a tiempo ($((T1-T0)) ms): $(last 'cut:')"
sleep 0.8
[[ "$(pgrep -x ffmpeg -P $APP | wc -l)" == 0 ]] && ok "ningún ffmpeg de corte vivo" || ko "quedan ffmpeg: $(pgrep -x ffmpeg -P $APP | tr '\n' ' ')"
[[ -f "$K1" && -z "$(ls "$OUT/video" | grep -- '-cortado\|\.part')" ]] && ok "uno sigue en disco y no hay restos (-cortado, .part)" || ko "ficheros: $(ls "$OUT/video" | tr '\n' ' ')"
[[ "$(last '\] panel:')" == *"uno.$EXT, dos.$EXT, tres.$EXT"* && "$(last '\] panel:')" != *"-uno"* ]] && ok "uno vuelve a la lista" || ko "panel: '$(last '\] panel:')'"
grep -q "toast ok: Corte cancelado" "$L" && ok "toast: «Corte cancelado»" || ko "sin toast de cancelado: $(last 'toast ')"
waitfor "toast cortes: ninguno" 3 && ok "el aviso de cortes desaparece" || ko "el aviso sigue: $(last 'toast cortes')"
ND=$(n "eliminar: diálogo"); xdotool key Delete; sleep 0.8
[[ "$(n 'eliminar: diálogo')" -gt "$ND" ]] && ok "sin corte pendiente, Supr vuelve a abrir Eliminar sobre dos" || ko "Supr no abre Eliminar sobre dos"
xdotool key Escape; sleep 0.5

# 4. cancelar al instante (keyframes / arranque de ffmpeg)
sleep 0.5; goto 3
[[ "$(shown)" == "tres.$EXT" ]] || ko "no se pasó a tres: '$(shown)'"
waitfor "fin: .*\[tres\.$EXT\]" 60 || true; sleep 0.3
xdotool mousemove $(tile 0 2) click 1; sleep 0.6
open_cut || ko "C no abrió el diálogo sobre tres"
xdotool key Up; sleep 0.2                      # «Sin pérdida»: busca keyframes primero
NC=$(n "cut: cancelado"); xdotool key Return; sleep 0.15
waitfor "toast cortes: cancelar=" 3 || true
T0=$(ms); cancel_cuts
waitn "cut: cancelado\|cut: hecho" $((NC+1)) 10; T1=$(ms)
if grep -q "cut: hecho tres" "$L"; then ok "(el corte sin pérdida terminó antes de poder cancelarlo: nada que comprobar)"
else
  [[ "$(n 'cut: cancelado')" -gt "$NC" && $((T1-T0)) -lt 3000 ]] && ok "cancelar al instante: acaba en $((T1-T0)) ms" || ko "cancelar al instante no acabó ($((T1-T0)) ms): $(last 'cut:\|cortar:')"
  sleep 0.5
  [[ -f "$K3" && "$(last '\] panel:')" != *"-tres"* ]] && ok "tres sigue en disco y vuelve a la lista" || ko "tres: fichero=$([[ -f "$K3" ]] && echo sí || echo no) panel='$(last '\] panel:')'"
  [[ -z "$(ls "$OUT/video" | grep '^tres-cortado')" ]] && ok "sin tres-cortado$(grep -q 'cut: cancelado tarde' "$L" && echo ': el Cancelar llegó con el corte ya escrito y se retiró')" || ko "queda $(ls "$OUT/video" | grep '^tres-cortado' | tr '\n' ' ')"
fi
waitfor "toast cortes: ninguno" 3 || true

# 5. cola de verdad: uno (cortar y borrar) y dos (sólo cortar), seguidos
sleep 0.5; goto 1
[[ "$(shown)" == "uno.$EXT" ]] || ko "no se volvió a uno: '$(shown)'"
waitfor "fin: .*\[uno\.$EXT\]" 60 || true; sleep 0.3
# los segmentos de antes siguen guardados (3.ª a 10.ª tesela): C directamente, en exacto (tarda) para que dos espere
open_cut || ko "C no abrió el diálogo sobre uno"
xdotool key Down; sleep 0.2; xdotool key Return; sleep 1
[[ "$(shown)" == "dos.$EXT" ]] || ko "tras cortar uno no se pasó a dos: '$(shown)'"
open_cut || ko "C no abrió el diálogo sobre dos"
xdotool key Up; sleep 0.2; xdotool key alt+t; sleep 0.5
grep -q "cortar: en cola dos.$EXT (1 por delante)" "$L" && ok "dos espera en la cola a que acabe uno" || ko "dos no esperó: $(last 'cortar: en cola')"
waitfor "cut: hecho dos-cortado" 180 && ok "se cortan los dos" || ko "no terminó la cola: $(grep 'cut:\|cortar:' "$L" | tail -3 | tr '\n' ' ')"
grep -q "cortar: empieza dos.$EXT" "$L" && [[ "$(grep -n "cut: hecho uno-cortado\|cortar: empieza dos" "$L" | tail -2 | cut -d: -f3- | tr '\n' '|')" == *"hecho uno-cortado"*"empieza dos"* ]] && ok "dos arranca solo al terminar uno" || ko "arranque de dos: $(grep 'cortar: empieza\|cut: hecho' "$L" | tail -3 | tr '\n' ' ')"
ord=$(grep -o "cut: hecho [a-z]*-cortado" "$L" | tail -2 | tr '\n' ' ')
[[ "$ord" == "cut: hecho uno-cortado cut: hecho dos-cortado " ]] && ok "en orden: uno y luego dos" || ko "orden: '$ord'"
waitfor "cut: original borrado uno.$EXT" 20 || true
[[ ! -f "$K1" && -f "$OUT/video/uno-cortado.$EXT" ]] && ok "uno: cortado y original borrado" || ko "uno: original=$([[ -f "$K1" ]] && echo sigue || echo borrado) cortado=$([[ -f "$OUT/video/uno-cortado.$EXT" ]] && echo sí || echo no)"
[[ -f "$K2" && -f "$OUT/video/dos-cortado.$EXT" ]] && ok "dos: cortado y original conservado" || ko "dos: original=$([[ -f "$K2" ]] && echo sí || echo no) cortado=$([[ -f "$OUT/video/dos-cortado.$EXT" ]] && echo sí || echo no)"
grep -q "toast ok: Cortado: uno-cortado.*original borrado" "$L" && grep -q "toast ok: Cortado: dos-cortado" "$L" && ok "toasts verdes de los dos" || ko "toasts: $(grep 'toast ok' "$L" | tail -2 | tr '\n' ' ')"
waitfor "toast cortes: ninguno" 3 || true
shot 5-cola-hecha 0.3

# 6. F = Full
sleep 0.5; [[ "$(shown)" == "dos.$EXT" ]] || ko "a la vista: '$(shown)' (se esperaba dos)"
xdotool key f; sleep 0.8
[[ "$(nwin)" == 2 ]] && ok "F abre el diálogo de Full" || ko "F no abrió el diálogo de Full"
xdotool key Escape; sleep 0.6
[[ "$(nwin)" == 1 && -f "$K2" ]] && ok "Esc lo cierra sin tocar nada" || ko "tras Esc: ventanas=$(nwin) dos=$([[ -f "$K2" ]] && echo sí || echo no)"
xdotool key f; sleep 0.8; xdotool key Return
waitfor "full: renombrado" 10 && ok "F + Intro renombra: $(last 'full: renombrado')" || ko "F + Intro no renombró: $(last 'full:')"
[[ ! -f "$K2" && -n "$(ls "$OUT/video" | grep "^dos-cortado-2\.$EXT$")" ]] && ok "dos.$EXT → dos-cortado-2.$EXT (ya había un dos-cortado)" || ko "ficheros: $(ls "$OUT/video" | tr '\n' ' ')"

# 7. cerrar con trabajo pendiente: borrado programado (cinco) y corte en marcha (cuatro, exacto, sólo cortar). Las dos
#    últimas filas: el paso 4 puede dejar tres o no (si el corte acabó antes de poder cancelarlo)
sleep 0.8   # la geometría de tras Full (una fila menos)
NR=$(grep "geometry:" "$L" | tail -1 | sed -E 's/.* rows=([^ ]*).*/\1/' | tr ';' '\n' | grep -c .)
goto "$NR"
[[ "$(shown)" == "cinco.$EXT" ]] || { ko "no se pasó a cinco: '$(shown)' (filas: $NR)"; echo "HAY FALLOS"; exit 1; }   # no borrar otro
xdotool key Delete; sleep 0.8; xdotool key Return; sleep 0.8
grep -q "eliminar: programado cinco.$EXT" "$L" && ok "cinco: borrado programado" || ko "cinco no se programó: $(last 'eliminar:')"
[[ "$(shown)" == "cuatro.$EXT" ]] && ok "se pasa a cuatro" || ko "tras programar cinco no se pasó a cuatro: '$(shown)'"
waitfor "fin: .*\[cuatro\.$EXT\]" 60 || true; sleep 0.3
xdotool mousemove $(tile 0 0) click 1; sleep 0.4
xdotool mousemove $(tile 1 3) keydown shift click 1 keyup shift; sleep 0.6
open_cut || ko "C no abrió el diálogo sobre cuatro"
xdotool key Down; sleep 0.2; xdotool key alt+t; sleep 1
grep -q "cortar: empieza cuatro.$EXT" "$L" && ok "cuatro cortándose (exacto, sólo cortar)" || ko "cuatro no empezó a cortarse: $(last 'cortar:')"
NCC=$(n "cut: cancelado")
xdotool key ctrl+q; sleep 0.8
[[ "$(nwin)" == 2 ]] && grep -q "cerrar: 1 pendiente de borrar" "$L" && grep -q "cerrar: 1 corte sin terminar" "$L" && ok "Ctrl+Q avisa de las dos cosas: un borrado y un corte" || ko "Ctrl+Q: ventanas=$(nwin) · $(grep 'cerrar' "$L" | tail -2 | tr '\n' ' ')"
shot 7-cerrar 0.2
xdotool key Escape; sleep 0.8
grep -q "cerrar: se vuelve" "$L" && kill -0 $APP 2>/dev/null && [[ "$(n 'cut: cancelado')" == "$NCC" ]] && ! grep -q "cut: hecho cuatro" "$L" && ok "Esc: se vuelve y el corte sigue" || ko "tras Esc: $(grep 'cerrar\|cut:' "$L" | tail -2 | tr '\n' ' ')"
xdotool key ctrl+q; sleep 0.8; xdotool key Return
for i in $(seq 1 40); do kill -0 $APP 2>/dev/null || break; sleep 0.25; done
! kill -0 $APP 2>/dev/null && ok "Ctrl+Q + Intro cierra la app" || ko "la app no se cerró"
grep -q "cerrar: confirmado, 1 borrado en marcha" "$L" && grep -q "cerrar: 1 corte cancelado" "$L" && ok "al cerrar se dispara el borrado y se cancela el corte" || ko "cerrar: $(grep 'cerrar' "$L" | tail -3 | tr '\n' ' ')"
[[ ! -f "$K5" ]] && ok "cinco borrado al cerrar" || ko "cinco sigue en disco"
[[ -f "$K4" && -z "$(ls "$OUT/video" | grep '^cuatro-cortado')" ]] && ok "cuatro intacto y sin restos del corte (ni -cortado ni .part)" || ko "ficheros: $(ls "$OUT/video" | tr '\n' ' ')"
left=$(pgrep -c -x ffmpeg || true); [[ "$left" == 0 ]] && ok "sin ffmpeg tras cerrar" || ko "ffmpeg vivos tras cerrar: $left"
# 8. sólo cortes: reabrir con cuatro (su selección se guardó), cortarlo en exacto y cerrar: el aviso habla sólo del corte
L="$OUT/app2.log"
python3 "$DIR/thumbsheet.py" "$K4" >"$L" 2>&1 &
APP=$!
for i in $(seq 1 40); do WID=$(xdotool search --onlyvisible --classname thumbsheet 2>/dev/null | head -1 || true); [[ -n "$WID" ]] && break; sleep 0.25; done
xdotool windowsize "$WID" 1400 900; xdotool windowmove "$WID" 0 0; sleep 0.3
waitfor "fin: .*\[cuatro\.$EXT\]" 60 || true; sleep 0.5
open_cut || ko "C no abrió el diálogo sobre cuatro (segunda sesión)"
xdotool key alt+t; sleep 1
grep -q "cortar: empieza cuatro.$EXT" "$L" && ok "segunda sesión: cuatro cortándose" || ko "cuatro no empezó a cortarse: $(last 'cortar:')"
xdotool key ctrl+q; sleep 0.8
[[ "$(nwin)" == 2 ]] && grep -q "cerrar: 1 corte sin terminar" "$L" && ! grep -q "pendiente de borrar" "$L" && ok "sólo cortes: el aviso habla sólo del corte" || ko "Ctrl+Q: ventanas=$(nwin) · $(grep 'cerrar' "$L" | tail -2 | tr '\n' ' ')"
shot 8-cerrar-solo-cortes 0.2
xdotool key Return
for i in $(seq 1 40); do kill -0 $APP 2>/dev/null || break; sleep 0.25; done
! kill -0 $APP 2>/dev/null && grep -q "cerrar: 1 corte cancelado" "$L" && ok "Intro («Cerrar») cierra y cancela el corte" || ko "cierre: app $(kill -0 $APP 2>/dev/null && echo viva || echo cerrada) · $(grep 'cerrar' "$L" | tail -2 | tr '\n' ' ')"
[[ -f "$K4" && -z "$(ls "$OUT/video" | grep '^cuatro-cortado')" ]] && ok "cuatro intacto y sin restos" || ko "ficheros: $(ls "$OUT/video" | tr '\n' ' ')"
left=$(pgrep -c -x ffmpeg || true); [[ "$left" == 0 ]] && ok "sin ffmpeg tras cerrar" || ko "ffmpeg vivos tras cerrar: $left"
echo "--- log"; grep "cerrar:\|cortar:\|cut: \(hecho\|cancelado\|error\|original\)\|toast cortes: [A-Z]" "$L" | tail -12 | cut -c1-150
[[ $fail == 0 ]] && echo "TODO OK" || echo "HAY FALLOS"
exit $fail
