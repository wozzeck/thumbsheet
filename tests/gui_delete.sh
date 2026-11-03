#!/usr/bin/env bash
# Humo de la GUI en un Xvfb propio (:66): Eliminar con segundos de gracia para cancelar. Tres COPIAS de A (uno, dos, tres).
#  1. Supr + Intro sobre uno: se pasa a dos al momento, uno sale de la lista (fila escondida, recuento, panel) y abajo
#     sale el aviso «Borrando uno en 10 s» con Cancelar; el fichero sigue en disco y queda apuntado en
#     pending-delete.json. Cancelar: uno vuelve a la lista, el fichero sigue, el apunte desaparece, sin toast verde.
#  2. Supr + Intro sobre dos: tres a la vista al momento; a los 3 s el fichero sigue; a los ~10 s se borra (fichero
#     fuera, toast verde, apunte fuera).
#  3. Supr + Intro sobre tres y Ctrl+Q en seguida: diálogo «pendiente de borrar»; Esc vuelve (la app sigue, con su
#     aviso); Ctrl+Q + Intro cierra y el borrado se hace igualmente (fichero y apunte fuera).
#  4. Arranque con un apunte pendiente (cuatro, con una caché falsa) y cuatro en los argumentos: se borran fichero y
#     caché, toast verde, cuatro no se carga (cabecera: 1 archivo) y Ctrl+Q cierra sin preguntar.
# Uso: tests/gui_delete.sh <vídeo A corto> [out]
set -euo pipefail
VIDEO_A="$1"; OUT="${2:-/tmp/thumbsheet-gui-delete}"; rm -rf "$OUT"; mkdir -p "$OUT/video"
DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
export DISPLAY=:66
export XDG_CACHE_HOME="$OUT/cache" XDG_CONFIG_HOME="$OUT/config" THUMBSHEET_AUDIO=0 THUMBSHEET_DEBUG=1
EXT="${VIDEO_A##*.}"; K1="$OUT/video/uno.$EXT"; K2="$OUT/video/dos.$EXT"; K3="$OUT/video/tres.$EXT"; K4="$OUT/video/cuatro.$EXT"
PF="$OUT/config/thumbsheet/pending-delete.json"
for f in "$K1" "$K2" "$K3"; do cp -f "$VIDEO_A" "$f"; done
Xvfb :66 -screen 0 1400x900x24 -nolisten tcp >/dev/null 2>&1 &
XPID=$!
trap 'kill $APP 2>/dev/null || true; kill $XPID 2>/dev/null || true' EXIT
sleep 1
L="$OUT/app.log"
python3 "$DIR/thumbsheet.py" "$K1" "$K2" "$K3" >"$L" 2>&1 &
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
waitfor() { local i; for i in $(seq 1 $((${2:-20}*4))); do grep -q "$1" "$L" && return 0; sleep 0.25; done; return 1; }
stamp() { grep "$1" "$L" | tail -1 | sed 's/^\[\([0-9:]*\)\].*/\1/'; }
secs() { date -d "$1" +%s; }
nrows() { local G; G=$(grep "geometry:" "$L" | tail -1); echo "$G" | sed -E 's/.* rows=([^ ]*).*/\1/' | tr ';' '\n' | grep -c . || true; }
waitfor "fin: .*\[uno\.$EXT\]" 90 || { echo "no termina de generar uno.$EXT"; tail -5 "$L"; exit 1; }
waitfor "geometry:" 20 || true; sleep 0.5
[[ "$(shown)" == "uno.$EXT" ]] || { echo "a la vista debería estar uno.$EXT y está '$(shown)'"; exit 1; }
[[ ! -f "$PF" ]] && ok "al arrancar sin nada pendiente no hay pending-delete.json" || ko "pending-delete.json existe al arrancar: $(cat "$PF")"

# 1. Eliminar uno y cancelar desde el aviso
xdotool key Delete; sleep 0.8; xdotool key Return; sleep 0.8
grep -q "eliminar: programado uno.$EXT (en 10 s)" "$L" && ok "Supr + Intro: uno.$EXT programado para borrarse en 10 s" || ko "no se programó: $(grep 'eliminar' "$L" | tail -1)"
[[ "$(shown)" == "dos.$EXT" ]] && ok "se pasa a dos.$EXT al momento" || ko "a la vista: '$(shown)'"
waitfor "toast espera: cancelar=" 5 && ok "aviso abajo: $(last 'toast espera: Borrando')" || ko "sin aviso de borrado programado: $(grep 'toast' "$L" | tail -2 | tr '\n' ' ')"
[[ "$(last 'toast espera: Borrando')" == "toast espera: Borrando uno.$EXT en 10 s" ]] && ok "el aviso dice qué se borra y cuánto queda" || ko "texto del aviso: '$(last 'toast espera: Borrando')'"
[[ "$(last '\] panel:')" == *"-uno.$EXT, dos.$EXT, tres.$EXT"* ]] && ok "panel: uno escondido (-) y los otros dos" || ko "panel: '$(last '\] panel:')'"
[[ "$(last 'archivos:')" == "archivos: 2" ]] && ok "cabecera: 2 archivos" || ko "cabecera: '$(last 'archivos:')'"
[[ "$(nrows)" == 2 ]] && ok "geometría: 2 filas a la vista" || ko "filas a la vista: $(nrows)"
[[ -f "$PF" ]] && grep -q "uno.$EXT" "$PF" && ok "apuntado en pending-delete.json" || ko "sin apunte: $(cat "$PF" 2>/dev/null || echo 'no hay fichero')"
sleep 2; [[ -f "$K1" ]] && ok "a los 3 s el fichero sigue en disco" || ko "¡borrado antes de los 10 s!"
shot 1-aviso-cancelar 0.2
C=$(grep "toast espera: cancelar=" "$L" | tail -1 | sed 's/.*cancelar=//'); CX=${C%,*}; CY=${C#*,}
xdotool mousemove $((X+CX)) $((Y+CY)) click 1; sleep 1
grep -q "eliminar: cancelado uno.$EXT (Cancelar en el aviso)" "$L" && ok "Cancelar: borrado anulado" || ko "Cancelar no anuló: $(grep 'eliminar' "$L" | tail -1)"
[[ "$(last 'toast espera:')" == "toast espera: ninguno" ]] && ok "el aviso se retira" || ko "aviso: '$(last 'toast espera:')'"
[[ "$(last '\] panel:')" == *": uno.$EXT, dos.$EXT, tres.$EXT"* ]] && ok "uno vuelve a la lista en su sitio" || ko "panel tras cancelar: '$(last '\] panel:')'"
[[ "$(last 'archivos:')" == "archivos: 3" ]] && ok "cabecera: 3 archivos" || ko "cabecera tras cancelar: '$(last 'archivos:')'"
sleep 0.5; [[ "$(nrows)" == 3 ]] && ok "geometría: 3 filas a la vista" || ko "filas tras cancelar: $(nrows)"
[[ ! -f "$PF" ]] && ok "el apunte desaparece" || ko "pending-delete.json sigue: $(cat "$PF")"
sleep 9; [[ -f "$K1" && "$(n 'toast ok: Eliminado')" == 0 && "$(n 'borrado: ')" == 0 ]] && ok "pasados 10 s más nada se borra (uno sigue, sin toast verde)" || ko "tras cancelar: uno $([[ -f "$K1" ]] && echo sigue || echo BORRADO) · $(grep 'toast ok\|borrado:' "$L" | tail -1)"
[[ "$(shown)" == "dos.$EXT" ]] && ok "sigue dos.$EXT a la vista (cancelar no cambia de vídeo si hay uno)" || ko "a la vista tras cancelar: '$(shown)'"

# 2. Eliminar dos y dejar que pase el tiempo
xdotool key Delete; sleep 0.8; xdotool key Return; sleep 0.8
grep -q "eliminar: programado dos.$EXT" "$L" && [[ "$(shown)" == "tres.$EXT" ]] && ok "Supr + Intro sobre dos: programado y tres.$EXT a la vista al momento" || ko "dos: $(grep 'eliminar' "$L" | tail -1) · a la vista '$(shown)'"
sleep 2.2; [[ -f "$K2" ]] && ok "a los 3 s dos sigue en disco" || ko "dos borrado antes de tiempo"
[[ "$(last 'toast espera: Borrando')" == "toast espera: Borrando dos.$EXT en 10 s" ]] && ok "aviso para dos" || ko "aviso: '$(last 'toast espera: Borrando')'"
waitfor "borrado: dos.$EXT" 20 && ok "a los 10 s se borra en segundo plano" || ko "no se borró dos: $(grep 'borr\|elimin' "$L" | tail -2 | tr '\n' ' ')"
T0=$(secs "$(stamp "eliminar: programado dos.$EXT")"); T1=$(secs "$(stamp "borrado: dos.$EXT")"); DT=$((T1-T0))
[[ "$DT" -ge 9 && "$DT" -le 14 ]] && ok "entre programar y borrar pasaron $DT s" || ko "entre programar y borrar pasaron $DT s"
sleep 1; [[ ! -f "$K2" ]] && ok "dos.$EXT ya no está en disco" || ko "dos.$EXT sigue en disco"
waitfor "toast ok: Eliminado: dos.$EXT" 5 && ok "toast verde al terminar" || ko "sin toast verde: $(grep 'toast' "$L" | tail -1)"
[[ ! -f "$PF" ]] && ok "apunte retirado al terminar" || ko "pending-delete.json sigue: $(cat "$PF")"
[[ "$(last 'toast espera:')" == "toast espera: ninguno" ]] && ok "aviso retirado" || ko "aviso: '$(last 'toast espera:')'"
[[ "$(last '\] panel:')" == *": uno.$EXT, tres.$EXT"* ]] && ok "panel sin dos" || ko "panel: '$(last '\] panel:')'"
shot 2-tras-borrar 0.2

# 3. Eliminar tres y cerrar en seguida: aviso al cerrar
xdotool key Delete; sleep 0.8; xdotool key Return; sleep 0.8
grep -q "eliminar: programado tres.$EXT" "$L" && ok "Supr + Intro sobre tres: programado" || ko "tres: $(grep 'eliminar' "$L" | tail -1)"
[[ "$(shown)" == "uno.$EXT" ]] && ok "sin siguiente, se pasa al anterior (uno.$EXT)" || ko "a la vista: '$(shown)'"
xdotool key ctrl+q; sleep 1.2
grep -q "cerrar: 1 pendiente de borrar" "$L" && kill -0 $APP 2>/dev/null && ok "Ctrl+Q con un borrado pendiente: pregunta, la app sigue" || ko "Ctrl+Q: $(grep 'cerrar' "$L" | tail -1) · app $(kill -0 $APP 2>/dev/null && echo viva || echo CERRADA)"
shot 3-dialogo-cerrar 0.2
xdotool key Escape; sleep 1
grep -q "cerrar: se vuelve" "$L" && kill -0 $APP 2>/dev/null && [[ -f "$K3" ]] && ok "Esc: se vuelve, tres sigue en disco" || ko "tras Esc: $(grep 'cerrar' "$L" | tail -1) · tres $([[ -f "$K3" ]] && echo sigue || echo BORRADO)"
[[ "$(last 'toast espera: Borrando')" == *"Borrando tres.$EXT en"* && "$(last 'toast espera:')" != "toast espera: ninguno" ]] && ok "el aviso sigue ahí con su cuenta atrás ($(last 'toast espera: Borrando' | sed 's/.*en //'))" || ko "aviso tras volver: '$(last 'toast espera:')'"
xdotool key ctrl+q; sleep 1; xdotool key Return
for i in $(seq 1 60); do kill -0 $APP 2>/dev/null || break; sleep 0.25; done
! kill -0 $APP 2>/dev/null && ok "Ctrl+Q + Intro: la app se cierra" || { ko "la app no se cerró"; kill $APP; sleep 1; }
grep -q "cerrar: confirmado, 1 borrado en marcha" "$L" && ok "al cerrar se dispara el borrado" || ko "cerrar: $(grep 'cerrar' "$L" | tail -1)"
grep -q "borrado: tres.$EXT" "$L" && [[ ! -f "$K3" ]] && ok "tres.$EXT borrado aunque la ventana ya no estaba" || ko "tres: $([[ -f "$K3" ]] && echo sigue || echo fuera) · $(grep 'borrado:' "$L" | tail -1)"
[[ ! -f "$PF" ]] && ok "sin apuntes pendientes tras cerrar" || ko "pending-delete.json tras cerrar: $(cat "$PF")"
grep -q "Traceback" "$L" && ko "traceback en el log" || ok "sin tracebacks"

# 4. arranque con un apunte pendiente (de una sesión que murió sin borrar)
cp -f "$VIDEO_A" "$K4"; FC="$OUT/cache/thumbsheet/cache-falsa"; mkdir -p "$FC"; echo x > "$FC/1.jpg"; mkdir -p "$(dirname "$PF")"
printf '[{"path": "%s", "cache": "%s"}]\n' "$K4" "$FC" > "$PF"
L="$OUT/app2.log"
python3 "$DIR/thumbsheet.py" "$K1" "$K4" >"$L" 2>&1 &
APP=$!
waitfor "pendiente: borrado cuatro.$EXT" 30 && ok "al arrancar se borra lo pendiente: cuatro.$EXT" || ko "arranque: $(grep 'pendiente' "$L" | tail -2 | tr '\n' ' ')"
sleep 0.5; [[ ! -f "$K4" && ! -d "$FC" ]] && ok "fichero y caché fuera" || ko "cuatro $([[ -f "$K4" ]] && echo sigue || echo fuera), caché $([[ -d "$FC" ]] && echo sigue || echo fuera)"
[[ ! -f "$PF" ]] && ok "apunte retirado" || ko "pending-delete.json: $(cat "$PF")"
grep -q "pendiente: no se carga cuatro.$EXT" "$L" && ok "cuatro.$EXT venía en los argumentos y no se carga" || ko "sin traza de omitir cuatro: $(grep 'pendiente' "$L" | tr '\n' ' ')"
waitfor "toast ok: Borrado de la sesión anterior: cuatro.$EXT" 5 && ok "toast verde del borrado pendiente" || ko "sin toast: $(grep 'toast' "$L" | tail -1)"
waitfor "archivos: 1" 10 && ok "cabecera: 1 archivo (uno)" || ko "cabecera: $(grep -o 'archivos: [0-9]*' "$L" | tail -1)"
sleep 1; xdotool key ctrl+q
for i in $(seq 1 40); do kill -0 $APP 2>/dev/null || break; sleep 0.25; done
! kill -0 $APP 2>/dev/null && ! grep -q "cerrar:" "$L" && ok "Ctrl+Q sin nada pendiente cierra sin preguntar" || { ko "cierre: app $(kill -0 $APP 2>/dev/null && echo viva || echo cerrada) · $(grep 'cerrar' "$L" | tail -1)"; kill $APP 2>/dev/null || true; }
grep -q "Traceback" "$L" && ko "traceback en el segundo log" || ok "sin tracebacks (2)"
left=$(pgrep -c -x ffmpeg || true); [[ "$left" == 0 ]] && ok "sin ffmpeg tras cerrar" || ko "ffmpeg vivos tras cerrar: $left"
[[ -f "$VIDEO_A" ]] || { echo "¡¡el vídeo ORIGINAL ha desaparecido!!"; fail=1; }
[[ -f "$K1" ]] && ok "uno.$EXT (cancelado) sigue en disco al final" || ko "uno.$EXT desapareció"
[[ $fail == 0 ]] && echo "TODO OK" || { echo "HAY FALLOS"; exit 1; }
