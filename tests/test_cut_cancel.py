#!/usr/bin/env python3
"""Sin ventana, unos segundos: Cancelar un corte no puede quedarse esperando. `run_cancellable` (lo que lanza el
ffprobe de la búsqueda de keyframes) devuelve la salida normal, y con el aviso de cancelar o al pasar el tope mata el
proceso en seco y vuelve en seguida; `keyframes_near` y `expand_to_keyframes` con el aviso ya dado vuelven sin
resultado al momento (antes, cancelar en la fase «Analizando keyframes…» dejaba el corte «cancelando» para siempre).
Y un Cancelar que llega con el corte ya escrito se respeta: no se borra el original y se retira el resultado.
Uso: tests/test_cut_cancel.py <vídeo>"""
import os, sys, pathlib, threading, time, tempfile, types, shutil
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import thumbsheet as T

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


video = sys.argv[1]
out = T.run_cancellable(["sh", "-c", "echo hola"])
check(out == b"hola\n", "sin cancelar: devuelve la salida (%r)" % out)

ev = threading.Event()
threading.Timer(0.3, ev.set).start()
t0 = time.monotonic()
out = T.run_cancellable(["sleep", "30"], ev)
dt = time.monotonic() - t0
check(out is None and dt < 1.5, "cancelado a los 0,3 s: el proceso muere y vuelve en %.2f s" % dt)

t0 = time.monotonic()
out = T.run_cancellable(["sleep", "30"], None, timeout=0.5)
dt = time.monotonic() - t0
check(out is None and dt < 2.0, "tope de 0,5 s: vuelve en %.2f s" % dt)

check(T.run_cancellable(["/no/existe/ffprobe"]) is None, "programa que no existe: None, sin excepción")

kf = T.keyframes_near(video, [1.0], 20.0)
check(len(kf) >= 1, "keyframes_near sin cancelar: %d keyframes" % len(kf))
ev = threading.Event()
ev.set()
t0 = time.monotonic()
kf = T.keyframes_near(video, [1.0], None, ev)
check(kf == [] and time.monotonic() - t0 < 1.0, "keyframes_near con el aviso dado: nada, al momento")
t0 = time.monotonic()
res = T.expand_to_keyframes([(1.0, 2.0)], video, 9.0, None, ev)
check(res is None and time.monotonic() - t0 < 1.0, "expand_to_keyframes con el aviso dado: None, al momento")
res = T.expand_to_keyframes([(1.0, 2.0)], video, 9.0)
check(isinstance(res, list) and len(res) == 1 and res[0][0] <= 1.0 and res[0][1] >= 2.0,
      "expand_to_keyframes sin aviso: el tramo crece hasta keyframes (%s)" % res)


# Cancelar que llega con el corte ya escrito (ffmpeg acabó, el aviso a la interfaz aún no se ha atendido): visto en la
# GUI, «cortar y borrar» seguía adelante y borraba el original. _cut_done tiene que respetar el Cancelar.
class FakeWin(object):
    def __init__(self, job):
        self._cut, self._cut_pct, self.calls = job, 5, []

    def _flash(self, text):
        self.calls.append(("flash", text))

    def _error(self, *a):
        self.calls.append(("error",) + a)

    def _refresh_output(self, out):
        self.calls.append(("refresh_output", out))

    def _delete_file(self, *a, **kw):
        self.calls.append(("delete_file",))

    def _unhide_cut(self, doc):
        self.calls.append(("unhide", doc))

    def _start_next_background(self):
        return False

    def _update_buttons(self):
        pass

    def _cut_next(self):
        pass

    def _refresh_cuts(self):
        pass

    def _refresh_status(self):
        pass


tmp = pathlib.Path(tempfile.mkdtemp(prefix="ts-cutcancel-"))
try:
    for cancelled in (True, False):
        out = tmp / ("v-cortado-%d.mp4" % cancelled)
        out.write_bytes(b"resultado")
        doc = types.SimpleNamespace(cut_jobs=1, path=tmp / "v.mp4")
        ev = threading.Event()
        if cancelled:
            ev.set()
        job = {"doc": doc, "out": out, "cancel": ev, "delete": True, "segments": [(0.0, 10.0)], "final": [(0.0, 10.0)],
               "expected": 10.0}
        win = FakeWin(job)
        T.ThumbSheet._cut_done(win, job, 10.0, None)
        names = [c[0] for c in win.calls]
        if cancelled:
            check("delete_file" not in names and not out.exists() and ("unhide", doc) in win.calls
                  and ("flash", "Corte cancelado") in win.calls and doc.cut_jobs == 0,
                  "Cancelar con el corte ya escrito: no se borra el original, se retira el resultado y el vídeo vuelve "
                  "a la lista (%s)" % ", ".join(names))
        else:
            check("delete_file" in names and out.exists() and "unhide" not in names and win._cut is None,
                  "sin Cancelar, el mismo final sigue su curso: se borra el original y el resultado queda (%s)"
                  % ", ".join(names))
finally:
    shutil.rmtree(str(tmp), ignore_errors=True)
print("TODO OK" if not fails else "FALLOS: %d" % fails)
sys.exit(1 if fails else 0)
