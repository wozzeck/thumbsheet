#!/usr/bin/env python3
"""Sin pantalla: vídeo TRUNCADO (mp4 con la cabecera al principio: anuncia la duración completa pero los
datos acaban antes). Las capturas imposibles quedan marcadas como `<t>.fail` (aspa roja en el mosaico),
forman un sufijo del vídeo, el documento cuenta como completo, y una segunda pasada no vuelve a
intentarlas. Se prueba en modo seek (S grande) y en modo tramos (S=1, que reintenta con seek lo que el
tramo no da). Uso: tests/test_damaged.py <vídeo mp4 faststart> [fracción que se conserva, 0.5]"""
import os, sys, time, pathlib, shutil, tempfile, types
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("THUMBSHEET_DEBUG", "1")
import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GLib
import thumbsheet as T

video = pathlib.Path(sys.argv[1]); frac = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
tmp = pathlib.Path(tempfile.mkdtemp(prefix="thumbsheet-damaged-"))
trunc = tmp / ("trunc-" + video.name)
with open(str(video), "rb") as src, open(str(trunc), "wb") as dst:
    dst.write(src.read(int(video.stat().st_size * frac)))
info = T.VideoInfo(trunc)
print("vídeo truncado al %d %%: dur anunciada=%.1f s gop=%s" % (frac * 100, info.duration, info.gop))
loop = GLib.MainLoop()
rc = 0
def ok(m): print("ok   " + m)
def ko(m):
    global rc; rc = 1; print("FAIL " + m)

def run(S, cache, force_mode=None):
    cache.mkdir(parents=True, exist_ok=True)
    got, failed, state = set(), set(), {"done": False, "t0": time.time()}
    gen = T.Generator(info, cache, S, lambda t: got.add(t) or False, lambda d, n: False,
                      lambda: (state.__setitem__("done", True), loop.quit()) and False,
                      lambda t: failed.add(t) or False)
    if force_mode: gen.mode = force_mode
    GLib.timeout_add(300000, loop.quit)
    gen.start()
    if not state["done"]: loop.run()
    print("S=%-3d modo=%-5s capturas=%d ok=%d fallidas=%d en %.1fs" % (
        S, gen.mode, gen.total, len(got), len(failed), time.time() - state["t0"]))
    return gen, got, failed

for S, mode in ((5, None), (1, None)):
    cache = tmp / ("S%d" % S)
    gen, got, failed = run(S, cache, mode)
    jpg = sorted(int(p.stem) for p in cache.glob("*.jpg")); marks = sorted(int(p.stem) for p in cache.glob("*.fail"))
    if mode is None and S == 1 and gen.mode != "range": ko("S=1 debería ir en modo tramos (gop=%s)" % info.gop)
    if set(jpg) | set(marks) == set(gen.timestamps) and not (set(jpg) & set(marks)): ok("S=%d: cada instante tiene o captura o marca .fail (%d + %d)" % (S, len(jpg), len(marks)))
    else: ko("S=%d: cobertura incompleta o solapada: jpg=%d fail=%d total=%d" % (S, len(jpg), len(marks), gen.total))
    if marks and jpg and min(marks) > max(jpg): ok("S=%d: las fallidas son un sufijo (datos hasta ~%d s, fallan desde %d s)" % (S, max(jpg), min(marks)))
    else: ko("S=%d: fallidas=%s… ok hasta %s" % (S, marks[:5], max(jpg) if jpg else None))
    lo, hi = info.duration * frac * 0.7, info.duration * frac * 1.3
    if marks and lo <= min(marks) <= hi: ok("S=%d: el corte (%d s) es coherente con el %d %% conservado" % (S, min(marks), frac * 100))
    elif marks: ko("S=%d: el corte está en %d s, esperado entre %.0f y %.0f" % (S, min(marks), lo, hi))
    if failed == set(marks): ok("S=%d: on_failed avisó exactamente de las marcadas" % S)
    else: ko("S=%d: on_failed=%d marcas=%d" % (S, len(failed), len(marks)))
    doc = types.SimpleNamespace(info=info, cache_dir=cache)
    if T.doc_complete(doc, S): ok("S=%d: doc_complete cuenta las marcadas como hechas" % S)
    else: ko("S=%d: doc_complete sigue a False" % S)
    # segunda pasada: nada que hacer, avisa de las marcadas sin lanzar ffmpeg
    t0 = time.time(); gen2, got2, failed2 = run(S, cache, mode)
    if gen2.finished and not gen2.tasks and failed2 == set(marks) and got2 == set(jpg) and time.time() - t0 < 2: ok("S=%d: segunda pasada instantánea, sin reintentar las marcadas" % S)
    else: ko("S=%d: segunda pasada: finished=%s tareas=%d fallidas=%d" % (S, gen2.finished, len(gen2.tasks), len(failed2)))

# eta_text
if T.eta_text(None, 5, 5) == "" and T.eta_text(time.time(), 1, 5) == "" and T.eta_text(time.time() - 10, 2, 0) == "": ok("eta_text: sin base → vacío")
else: ko("eta_text sin base devuelve algo")
e = T.eta_text(time.time() - 10, 10, 30)
if e.startswith(" · faltan ~0:3"): ok("eta_text: 10 en 10 s, faltan 30 → '%s'" % e.strip())
else: ko("eta_text inesperado: '%s'" % e)
e = T.eta_text(time.time() - 10, 10, 1000)
m, sec = e.strip().split("~")[-1].split(":")
if int(sec) % 10 == 0 and 1000 <= int(m) * 60 + int(sec) <= 1020: ok("eta_text: en pasos de 10 s cuando queda más de minuto y medio (%s)" % e.strip())
else: ko("eta_text redondeo inesperado: '%s'" % e)
shutil.rmtree(str(tmp), ignore_errors=True)
print("RESULTADO:", "OK" if rc == 0 else "FALLO")
sys.exit(rc)
