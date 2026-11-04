#!/usr/bin/env python3
"""Sin ventana, unos segundos: Cancelar un corte no puede quedarse esperando. `run_cancellable` (lo que lanza el
ffprobe de la búsqueda de keyframes) devuelve la salida normal, y con el aviso de cancelar o al pasar el tope mata el
proceso en seco y vuelve en seguida; `keyframes_near` y `expand_to_keyframes` con el aviso ya dado vuelven sin
resultado al momento (antes, cancelar en la fase «Analizando keyframes…» dejaba el corte «cancelando» para siempre).
Uso: tests/test_cut_cancel.py <vídeo>"""
import os, sys, pathlib, threading, time
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
print("TODO OK" if not fails else "FALLOS: %d" % fails)
sys.exit(1 if fails else 0)
