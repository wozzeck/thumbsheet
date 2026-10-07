#!/usr/bin/env bash
# Humo de la GUI en un Xvfb propio (no toca el DISPLAY real). Abre COPIAS de los vídeos A y B (ambas se
# borran durante la prueba; los originales no se tocan), y con
# xdotool recorre: rueda sobre el slider de intervalo, clic/arrastre/Shift+clic/doble clic de teselas, LLC
# (y un error forzado → toast rojo), Cortar sin pérdida, cambio de intervalo sin alterar segmentos, vista
# ampliada (flechas, reproducción muda, seek), cambio de vídeo, centrado al cambiar columnas, Escape,
# Cortar y borrar original (doble pulsación, desarme a los 5 s), Eliminar con confirmación, Ctrl+Q, y SIGKILL
# en plena generación sin huérfanos. Capturas PNG en `out`.
# Las expectativas se calculan a partir del intervalo (S) y las columnas (COLS) reales. El vídeo A debe
# tener keyframes cada 6 s (grabación de Teams) para la comprobación del corte.
# Uso: tests/gui_smoke.sh <vídeo A> <vídeo B> [dir_salida]
set -euo pipefail
VIDEO_A="$1"; VIDEO_B_ORIG="$2"; OUT="${3:-/tmp/thumbsheet-gui}"; mkdir -p "$OUT/video"
VIDEO_B="$OUT/video/copia-$(basename "$VIDEO_B_ORIG")"; cp -f "$VIDEO_B_ORIG" "$VIDEO_B"
DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
export DISPLAY=:77
export XDG_CACHE_HOME="$OUT/cache" XDG_CONFIG_HOME="$OUT/config"   # no tocar la caché ni los ajustes reales
export THUMBSHEET_AUDIO=0    # la prueba de reproducción no debe sonar por los altavoces
rm -rf "$OUT/cache" "$OUT/config"
COPY="$OUT/video/copia-$(basename "$VIDEO_A")"; cp -f "$VIDEO_A" "$COPY"
Xvfb :77 -screen 0 1400x900x24 -nolisten tcp >/dev/null 2>&1 &
XPID=$!
trap 'kill $APP 2>/dev/null || true; kill $XPID 2>/dev/null || true' EXIT
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
G=$(grep "geometry:" "$OUT/app.log" | tail -1 || true)
[[ -n "$G" ]] || { echo "la app no publicó su geometría:"; cat "$OUT/app.log"; exit 1; }
echo "$G"
gv() { echo "$G" | sed -E "s/.* $1=([^ ]+).*/\1/"; }
SX=$(gv sheet | cut -d, -f1); SY=$(gv sheet | cut -d, -f2); COLS=$(gv cols); CW=$(gv cell | cut -dx -f1); CH=$(gv cell | cut -dx -f2); PAD=$(gv pad); GAP=$(gv gap)
tile() { local r=$1 c=$2; echo "$((X+SX+PAD+c*(CW+GAP)+CW/2)) $((Y+SY+PAD+r*(CH+GAP)+CH/2))"; }
IX=$(gv interval | cut -d, -f1); IY=$(gv interval | cut -d, -f2); TX=$(gv tile | cut -d, -f1); TY=$(gv tile | cut -d, -f2); TW=$(gv tilew)
LX=$(gv llc | cut -d, -f1); LY=$(gv llc | cut -d, -f2); CX=$(gv cut | cut -d, -f1); CY=$(gv cut | cut -d, -f2); DX=$(gv del | cut -d, -f1); DY=$(gv del | cut -d, -f2)
ROW1=$(gv rows | cut -d';' -f1); ROW2=$(gv rows | cut -d';' -f2)
SEL="$OUT/cache/thumbsheet"
S() { grep -o "plan: S=[0-9]*" "$OUT/app.log" | tail -1 | cut -d= -f2; }
sel() { cat "$SEL"/*/selection.json 2>/dev/null | head -1 || true; }
seg() { python3 -c 'import json,sys; a=[int(x) for x in sys.argv[1:]]; print(json.dumps({"segments": [[a[i], a[i+1]] for i in range(0, len(a), 2)]}))' "$@"; }
chk() { [[ "$2" == "$3" ]] && ok "$1" || ko "$1 → $2 (esperado $3)"; }

# 0. al terminar el vídeo actual, el otro se genera en segundo plano al mismo intervalo
for i in $(seq 1 20); do grep -q "fin: .*\[$(basename "$VIDEO_B")\]" "$OUT/app.log" && break; sleep 0.5; done
grep -q "segundo plano: $(basename "$VIDEO_B")" "$OUT/app.log" && grep -q "fin: .*\[$(basename "$VIDEO_B")\]" "$OUT/app.log" && ok "el segundo vídeo se generó en segundo plano al terminar el primero" || ko "sin generación en segundo plano del segundo vídeo"
grep -q "archivos: 2" "$OUT/app.log" && ok "cabecera del panel: 2 archivos" || ko "cabecera del panel: $(grep -o 'archivos: [0-9]*' "$OUT/app.log" | tail -1)"
NB=$(ls "$SEL"/*/ -d | wc -l); [[ "$NB" -ge 2 ]] && ok "hay caché para los dos vídeos ($NB directorios)" || ko "directorios de caché: $NB"

# 1. rueda sobre el slider de intervalo: un paso = siguiente valor de la lista (30 s → 1 min)
xdotool mousemove $((X+IX)) $((Y+IY)) click 4; sleep 1.5
S0=$(S); chk "rueda sobre intervalo: 30 s → 1 min (siguiente valor de la lista)" "$S0" "60"
A=$((COLS*S0)); B=$(((COLS+4)*S0))

# 2. clic = alternar; arrastre = rango contiguo
xdotool mousemove $(tile 0 1) click 1; shot 2-toggle 1
xdotool mousemove $(tile 1 0) mousedown 1; sleep 0.1; for c in 1 2 3; do xdotool mousemove $(tile 1 $c); sleep 0.08; done; xdotool mouseup 1; shot 3-arrastre 1
chk "segmentos = tesela (0,1) → $S0–$((2*S0)) s, fila 1 cols 0-3 → $A–$B s (S=$S0, $COLS columnas)" "$(sel)" "$(seg $S0 $((2*S0)) $A $B)"
xdotool mousemove $(tile 0 1) click 1; sleep 0.5
E2=$(seg $A $B); chk "segundo clic deselecciona (queda $A–$B)" "$(sel)" "$E2"

# 3. LLC: proyecto correcto, toast verde; error forzado (fichero de sólo lectura) → toast rojo, Esc lo cierra
xdotool mousemove $((X+LX)) $((Y+LY)) click 1; sleep 1
LLC="$OUT/video/copia-$(basename "${VIDEO_A%.*}")-proj.llc"
if [[ -f "$LLC" ]]; then python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d['version']==2 and d['cutSegments']==[{'start':int(sys.argv[2]),'end':int(sys.argv[3]),'name':''}], d" "$LLC" "$A" "$B" && ok "proyecto LLC con el segmento $A–$B" || ko "contenido LLC inesperado: $(tr -d '\n' < "$LLC")"; else ko "no existe $LLC"; fi
shot 4-llc 0.5
grep -q "toast ok: Proyecto LLC guardado" "$OUT/app.log" && ok "toast verde al guardar el LLC" || ko "sin toast ok del LLC"
chmod a-w "$LLC"; xdotool mousemove $((X+LX)) $((Y+LY)) click 1; sleep 0.8; xdotool key alt+s; shot 4-llc-error 1
grep -q "toast error: No se pudo guardar el proyecto LLC" "$OUT/app.log" && ok "toast rojo al fallar el LLC (permiso denegado)" || ko "sin toast de error del LLC"
xdotool windowfocus --sync "$WID"; xdotool key Escape; sleep 0.4; chmod u+w "$LLC"
chk "Esc cierra el toast de error sin tocar la selección" "$(sel)" "$E2"

# 4. Cortar sin pérdida: bordes a keyframes (cada 6 s en el vídeo de prueba)
xdotool mousemove $((X+CX)) $((Y+CY)) click 1; shot 4a-dialogo-cortar 1; xdotool key Return
for i in $(seq 1 60); do grep -q "cut: hecho\|cut: error" "$OUT/app.log" && break; sleep 0.5; done
CUT="$OUT/video/copia-$(basename "${VIDEO_A%.*}")-cortado.mp4"
if [[ -f "$CUT" ]]; then CD=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$CUT"); python3 -c "import sys; d=float(sys.argv[1]); a=int(sys.argv[2]); b=int(sys.argv[3]); ea=a-a%6; eb=b+(-b)%6; sys.exit(0 if abs(d-(eb-ea))<=4 else 1)" "$CD" "$A" "$B" && ok "corte sin pérdida: $(basename "$CUT") dura ${CD%.*} s (keyframes alrededor de $A–$B)" || ko "duración del corte inesperada: $CD"; else ko "no existe $CUT: $(grep 'cut:' "$OUT/app.log" | tail -1)"; fi
grep -q "toast ok: Cortado" "$OUT/app.log" && ok "toast verde al terminar el corte" || ko "sin toast ok del corte"
shot 4a2-tras-cortar 0.8

# 5. cambiar de intervalo no toca los segmentos guardados
xdotool mousemove $((X+IX)) $((Y+IY)) click 5; sleep 1.5
chk "1 min → 30 s: el segmento guardado sigue siendo $A–$B (S=$(S))" "$(sel)" "$E2"
shot 4b-intervalo-30 0.8
xdotool click 4; sleep 1.5
chk "30 s → 1 min: sigue $A–$B (S=$(S))" "$(sel)" "$E2"

# 6. vista ampliada: clic derecho, flechas, reproducción muda, seek, cierre con clic y con Esc
xdotool mousemove $(tile 0 2) click 3; shot 4c-vista-ampliada 1.5
FULL=$(ls "$SEL"/*/full/$((2*S0)).jpg 2>/dev/null | head -1 || true)
[[ -n "$FULL" ]] && ok "fotograma completo extraído ($((2*S0)) s): $(python3 -c "import gi; gi.require_version('GdkPixbuf','2.0'); from gi.repository import GdkPixbuf; p=GdkPixbuf.Pixbuf.new_from_file('$FULL'); print(p.get_width(),'x',p.get_height())")" || ko "no se extrajo full/$((2*S0)).jpg"
xdotool windowfocus --sync "$WID"; xdotool key Right; sleep 0.9; shot 4c2-flecha-derecha 0.2
ls "$SEL"/*/full/$((3*S0)).jpg >/dev/null 2>&1 && ok "flecha derecha: fotograma $((3*S0)) s extraído" || ko "flecha derecha: falta full/$((3*S0)).jpg"
xdotool key Left key Left; sleep 0.9
ls "$SEL"/*/full/$S0.jpg >/dev/null 2>&1 && ok "dos flechas izquierda: fotograma $S0 s extraído" || ko "flecha izquierda: falta full/$S0.jpg"
xdotool key space; sleep 3; shot 4e-reproduciendo 0.2; xdotool key space; sleep 0.6
P=$(grep -o "player: pausa en [0-9.]*" "$OUT/app.log" | tail -1 | awk '{print $4}')
python3 -c "import sys; p=float(sys.argv[1]); s=float(sys.argv[2]); sys.exit(0 if s+1.0 <= p <= s+6 else 1)" "${P:-0}" "$S0" && ok "play desde $S0 s y pausa ~3 s después (pos=$P)" || ko "posición tras reproducir inesperada: '$P'"
xdotool key Right; sleep 0.8
SK=$(grep -o "player: seek a [0-9.]*" "$OUT/app.log" | tail -1 | awk '{print $4}')
python3 -c "import sys; k=float(sys.argv[1]); p=float(sys.argv[2]); s=float(sys.argv[3]); sys.exit(0 if abs(k-(p+s))<=1.0 else 1)" "${SK:-0}" "${P:-0}" "$S0" && ok "flecha derecha con vídeo: seek +$S0 s (a $SK)" || ko "seek inesperado: '$SK' (pos $P + $S0)"
shot 4f-pausado-tras-seek 0.8
xdotool key Escape; sleep 0.5
xdotool mousemove $(tile 0 2) click 3; sleep 0.8; xdotool mousemove $((X+700)) $((Y+450)) click 1; sleep 0.5; shot 4d-vista-cerrada 0.3
xdotool mousemove $(tile 0 2) click 3; sleep 0.8; xdotool windowfocus --sync "$WID"; xdotool key Escape; sleep 0.5
chk "Esc cierra la vista ampliada sin tocar la selección" "$(sel)" "$E2"

# 7. Shift+clic (rango desde la última pulsada) y doble clic (quitar tramo / dejar seleccionada)
xdotool mousemove $(tile 0 3) keydown shift click 1 keyup shift; sleep 0.5
chk "Shift+clic selecciona $S0–$((4*S0)) desde la última pulsada" "$(sel)" "$(seg $S0 $((4*S0)) $A $B)"
xdotool mousemove $(tile 1 2) click --repeat 2 --delay 90 1; sleep 0.6
chk "doble clic deselecciona el tramo contiguo $A–$B" "$(sel)" "$(seg $S0 $((4*S0)))"
T30=$((3*COLS*S0)); xdotool mousemove $(tile 3 0) click --repeat 2 --delay 90 1; sleep 0.6
chk "doble clic sobre no seleccionada: la deja seleccionada ($T30–$((T30+S0)))" "$(sel)" "$(seg $S0 $((4*S0)) $T30 $((T30+S0)))"

# 8. segundo vídeo y vuelta
xdotool mousemove $((X+${ROW2%,*})) $((Y+${ROW2#*,})) click 1; shot 5-segundo-video 3
xdotool mousemove $((X+${ROW1%,*})) $((Y+${ROW1#*,})) click 1; shot 6-vuelta 1.5

# 9. columnas: con intervalo denso y el scroll a media altura, la tesela central sigue centrada al cambiar
#    columnas (medido de verdad: centro antes vs centro real tras recentrar, tolerancia de una fila)
xdotool mousemove $((X+IX)) $((Y+IY)) click 5 click 5 click 5 click 5; sleep 1.5     # 1 min → 5 s (conserva el centro)
chk "intervalo a 5 s para tener scroll" "$(S)" "5"
xdotool mousemove $((X+700)) $((Y+500)); for i in $(seq 1 12); do xdotool click 4; sleep 0.04; done; sleep 0.5   # a media altura del mosaico
# prioridad por pantalla: tras el scroll, la cola se reordena y la primera captura pendiente cae en lo visible
ORD=$(grep "orden: foco=[0-9]" "$OUT/app.log" | tail -1)
python3 -c "import re,sys; m=re.search(r'foco=(\d+)-(\d+) .*primera=(\d+)', sys.argv[1]); sys.exit(0 if m and int(m.group(1)) <= int(m.group(3)) <= int(m.group(2)) else 1)" "$ORD" && ok "prioridad por pantalla: ${ORD#*] }" || ko "sin reorden por el scroll: '$ORD'"
shot 7-scroll 0.5
centro() { { grep -o "cols: [0-9]* -> [0-9]*, centro t=[0-9]*" "$OUT/app.log" || true; } | tail -1 | sed 's/.*t=//'; }
ncols() { { grep -o "cols: [0-9]* -> [0-9]*" "$OUT/app.log" || true; } | tail -1 | sed 's/.*-> //'; }
ahora() { { grep -o "centro real ahora t=[0-9]*" "$OUT/app.log" || true; } | tail -1 | sed 's/.*t=//'; }
centrado_ok() { python3 -c "import sys; a=int(sys.argv[1]); b=int(sys.argv[2]); tol=int(sys.argv[3])*int(sys.argv[4]); sys.exit(0 if abs(a-b) <= tol else 1)" "$1" "$2" "$3" "$4"; }
# columnas exactas con el teclado: clic en el slider (toma el foco) y flechas hasta el valor deseado
to_cols() { local want=$1 n; xdotool mousemove $((X+TX)) $((Y+TY)) click 1; sleep 0.4
  for i in $(seq 1 24); do n=$(ncols); [[ -z "$n" ]] && n=$COLS; [[ "$n" == "$want" ]] && break
    if (( n < want )); then xdotool key Left; else xdotool key Right; fi; sleep 0.25; done; sleep 0.8; }   # slider invertido
grep -q "eta: · faltan" "$OUT/app.log" && ok "la barra de progreso muestra el tiempo estimado ($(grep -o 'eta: · faltan ~[0-9:]*' "$OUT/app.log" | head -1 | sed 's/eta: · //'))" || ko "sin tiempo estimado en la barra (eta:)"
B=$(basename "$VIDEO_B"); grep -q "estado: $B → generando" "$OUT/app.log" && grep -q "estado: $B → listas" "$OUT/app.log" && ok "icono de estado del panel: $B pasó por generando → listas" || ko "icono de estado: $(grep 'estado:' "$OUT/app.log" | tail -3 | tr '\n' ';')"
# 9a. arrastrar un slider no aplica nada hasta soltar el botón
NC0=$(grep -c "cols: " "$OUT/app.log" || true)
xdotool mousemove $((X+TX)) $((Y+TY)) mousedown 1; sleep 0.3; xdotool mousemove $((X+TX+TW/4)) $((Y+TY)); sleep 0.3; xdotool mousemove $((X+TX-TW/4)) $((Y+TY)); sleep 0.5
[[ "$(grep -c "cols: " "$OUT/app.log" || true)" == "$NC0" ]] && ok "arrastrando el slider de tamaño no se aplica nada" || ko "se aplicó durante el arrastre ($(ncols) columnas)"
xdotool mouseup 1; for i in $(seq 1 24); do [[ "$(grep -c "cols: " "$OUT/app.log" || true)" -gt "$NC0" ]] && break; sleep 0.25; done   # la app puede ir saturada generando
[[ "$(grep -c "cols: " "$OUT/app.log" || true)" -gt "$NC0" ]] && ok "al soltar se aplica el valor ($(ncols) columnas)" || ko "al soltar no se aplicó"
to_cols 9; shot 8-mas-columnas 0.3
[[ "$(ncols)" == 9 ]] && centrado_ok "$(centro)" "$(ahora)" "$(ncols)" "$(S)" && ok "más columnas (9): la tesela central ($(centro) s) sigue centrada (ahora $(ahora) s)" || ko "más columnas: cols=$(ncols) centro antes $(centro) s, después $(ahora) s"
to_cols 5; shot 9-menos-columnas 0.3
[[ "$(ncols)" == 5 ]] && centrado_ok "$(centro)" "$(ahora)" "$(ncols)" "$(S)" && ok "menos columnas (5): la tesela central ($(centro) s) sigue centrada (ahora $(ahora) s)" || ko "menos columnas: cols=$(ncols) centro antes $(centro) s, después $(ahora) s"

# 9b. Ctrl+A selecciona todo el vídeo como un único segmento
xdotool windowfocus --sync "$WID"; xdotool key ctrl+a; sleep 0.6
DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$COPY")
python3 -c "import json,sys; d=json.loads(sys.argv[1])['segments']; D=float(sys.argv[2]); sys.exit(0 if len(d)==1 and d[0][0]==0 and abs(d[0][1]-D)<1 else 1)" "$(sel)" "$DUR" && ok "Ctrl+A selecciona todo (0–${DUR%.*} s en un segmento)" || ko "Ctrl+A: selección $(sel)"
shot 9b-ctrl-a 0.3

# 10. Escape limpia la selección
xdotool key Escape; sleep 0.6
ls "$SEL"/*/selection.json >/dev/null 2>&1 && ko "Escape no limpió la selección" || ok "Escape limpia la selección"

# 10b. Cortar y borrar original: la primera pulsación sólo arma el botón (se desarma solo a los 5 s); la
#      segunda corta, borra el original y deja el resultado seleccionado en el panel
xdotool mousemove $((X+700)) $((Y+500)); for i in $(seq 1 80); do xdotool click 4; done; sleep 0.8   # arriba del todo (~1 fila por paso)
xdotool mousemove $(tile 0 1) click 1; sleep 0.5
chk "una tesela seleccionada para el corte (S=$(S))" "$(sel)" "$(seg 5 10)"
dlg() { xdotool search --name "Cortar y unir" 2>/dev/null | head -1 || true; }
shown() { grep "\] vídeo: " "$OUT/app.log" | tail -1 | sed 's/.*vídeo: //; s/ [0-9]*x[0-9]* .*//'; }   # vídeo a la vista
NV=$(grep -c "vídeo: $(basename "$VIDEO_B")" "$OUT/app.log" || true)
xdotool mousemove $((X+CX)) $((Y+CY)) click 1; sleep 1; xdotool key alt+b; shot 10b-armado 1
[[ -n "$(dlg)" && -f "$COPY" ]] && ok "cortar y borrar: la primera pulsación sólo arma el botón (diálogo abierto, original intacto)" || ko "primera pulsación: diálogo='$(dlg)' original=$([[ -f "$COPY" ]] && echo sigue || echo BORRADO)"
sleep 5.5; xdotool key alt+b; sleep 1
[[ -n "$(dlg)" && -f "$COPY" ]] && ok "pasados 5 s se desarma: la pulsación tardía tampoco confirma" || ko "la pulsación tardía confirmó: diálogo='$(dlg)' original=$([[ -f "$COPY" ]] && echo sigue || echo BORRADO)"
xdotool key alt+b
for i in $(seq 1 60); do grep -q "cut: original borrado\|cut: error" "$OUT/app.log" && break; sleep 0.5; done
[[ ! -f "$COPY" && -f "$CUT" ]] && ok "segunda pulsación: corta y borra el original" || ko "cortar y borrar: original=$([[ -f "$COPY" ]] && echo sigue || echo borrado) corte=$([[ -f "$CUT" ]] && echo sí || echo no) · $(grep 'cut:' "$OUT/app.log" | tail -1)"
grep -q "toast ok: Cortado.*original borrado" "$OUT/app.log" && ok "toast verde: cortado y original borrado" || ko "sin toast de cortado+borrado: $(grep 'toast' "$OUT/app.log" | tail -1)"
sleep 2; shot 10b-tras-cortar-borrar 0.5
[[ "$(grep -c "vídeo: $(basename "$VIDEO_B")" "$OUT/app.log" || true)" -gt "$NV" && "$(shown)" == "$(basename "$VIDEO_B")" ]] && ok "el original sale del panel y se abre el siguiente de la lista ($(basename "$VIDEO_B"))" || ko "tras cortar y borrar se muestra '$(shown)'"
grep -q "archivos: 1" "$OUT/app.log" && ok "cabecera del panel: 1 archivo tras cortar y borrar" || ko "cabecera no bajó a 1: $(grep -o 'archivos: [0-9]*' "$OUT/app.log" | tail -1)"
sleep 1.5; grep -q "segundo plano: $(basename "$CUT")\|vídeo: $(basename "$CUT")\|estado: $(basename "$CUT")" "$OUT/app.log" && ko "el cortado se añadió al panel" || ok "los cortados no se añaden al panel (ninguno de los dos)"

# 11. Eliminar con confirmación (Alt+E en el diálogo) sobre el vídeo a la vista: la copia de B (la lista
#     queda vacía). Sólo se pulsa si lo que está a la vista es efectivamente la copia.
if [[ "$(shown)" == "$(basename "$VIDEO_B")" ]]; then
  xdotool mousemove $((X+DX)) $((Y+DY)) click 1; shot 10-dialogo-eliminar 1
  xdotool key alt+e; sleep 1.2
  [[ ! -f "$VIDEO_B" ]] && ok "archivo eliminado del disco (copia de B); la lista queda vacía" || ko "el archivo sigue existiendo"
else ko "Eliminar omitido: a la vista está '$(shown)', no la copia de B"; fi
[[ -f "$VIDEO_B_ORIG" && -f "$VIDEO_A" ]] || { echo "¡¡un vídeo ORIGINAL ha desaparecido!!"; fail=1; }
grep -q "toast ok: Eliminado" "$OUT/app.log" && ok "toast verde al eliminar" || ko "sin toast ok al eliminar"
grep -q "archivos: 0" "$OUT/app.log" && ok "cabecera del panel: sin archivos tras eliminar" || ko "cabecera no bajó a 0: $(grep -o 'archivos: [0-9]*' "$OUT/app.log" | tail -1)"
shot 11-tras-eliminar 1

# 12. cierre limpio y SIGKILL en plena generación
xdotool key ctrl+q; sleep 1
if kill -0 $APP 2>/dev/null; then ko "la app no cerró con Ctrl+Q"; kill $APP; sleep 1; else ok "cierre con Ctrl+Q"; fi
left=$(pgrep -c -x ffmpeg || true); [[ "$left" == 0 ]] && ok "sin ffmpeg tras cerrar" || ko "ffmpeg vivos tras cerrar: $left"
rm -rf "$OUT/cache"; cp -f "$VIDEO_A" "$COPY"
python3 - "$OUT/config/thumbsheet/settings.json" <<'PY'
import json, sys, pathlib; f = pathlib.Path(sys.argv[1]); d = json.loads(f.read_text()) if f.exists() else {}; d["interval"] = 5; f.parent.mkdir(parents=True, exist_ok=True); f.write_text(json.dumps(d))
PY
THUMBSHEET_DEBUG=1 python3 "$DIR/thumbsheet.py" "$COPY" >"$OUT/app2.log" 2>&1 &
APP=$!
for i in $(seq 1 24); do n=$({ ls "$SEL"/*/*.jpg 2>/dev/null || true; } | wc -l); [[ "$n" -ge 100 ]] && break; sleep 0.25; done   # en plena generación
busy=$(pgrep -c -x ffmpeg || true); kill -9 $APP 2>/dev/null; sleep 1.5   # SIGTERM por PDEATHSIG: ffmpeg termina limpio, no instantáneo
left2=$(pgrep -c -x ffmpeg || true); [[ "$busy" -gt 0 && "$left2" == 0 ]] && ok "SIGKILL en plena generación: $busy ffmpeg → 0" || ko "SIGKILL: antes=$busy después=$left2"
echo "--- log"; grep -v "geometry" "$OUT/app.log" | tail -8 | cut -c1-160
echo "RESULTADO: $([[ $fail == 0 ]] && echo OK || echo FALLO)"; exit $fail
