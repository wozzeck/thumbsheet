#!/usr/bin/env python3
"""Prueba sin pantalla del generador: planifica, genera para varios intervalos, mide tiempos,
comprueba que están todas las capturas y que el modo tramos y el modo seek dan el mismo fotograma.
Uso: tests/test_generator.py <vídeo> [intervalos...]   (por defecto 60 y 5)"""
import os, sys, time, pathlib, shutil, subprocess, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("THUMBSHEET_DEBUG", "1")
import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GLib, GdkPixbuf
import thumbsheet as T

video = sys.argv[1]
intervals = [int(x) for x in sys.argv[2:]] or [60, 5]
tmp = pathlib.Path(tempfile.mkdtemp(prefix="thumbsheet-test-"))
info = T.VideoInfo(video)
print("video: %dx%d %s fps=%.2f dur=%.1f gop=%s thumb=%dx%d" % (info.width, info.height, info.codec, info.fps,
      info.duration, info.gop, info.thumb_w, info.thumb_h))
loop = GLib.MainLoop()
rc = 0

def run(S, force_mode=None, cache=None):
    cache = cache or (tmp / ("S%d" % S)); cache.mkdir(parents=True, exist_ok=True)
    got = set(); state = {"done": False, "t0": time.time()}
    gen = T.Generator(info, cache, S, lambda t: got.add(t) or False, lambda d, n: False,
                      lambda: (state.__setitem__("done", True), loop.quit()) and False)
    if force_mode: gen.mode = force_mode
    GLib.timeout_add(600000, loop.quit)
    gen.start()
    if not state["done"]: loop.run()
    el = time.time() - state["t0"]
    files = sorted(int(p.stem) for p in cache.glob("*.jpg"))
    miss = [t for t in gen.timestamps if t not in files]
    print("S=%-4d modo=%-5s capturas=%d en %.1fs  faltan=%d gpu=%s" % (S, gen.mode, len(gen.timestamps), el, len(miss), gen.gpu_ok))
    return gen, cache, miss

for S in intervals:
    gen, cache, miss = run(S)
    if miss: rc = 1; print("  FALTAN:", miss[:10])
    pb = GdkPixbuf.Pixbuf.new_from_file(str(cache / ("%d.jpg" % gen.timestamps[1 if len(gen.timestamps) > 1 else 0])))
    if (pb.get_width(), pb.get_height()) != (info.thumb_w, info.thumb_h):
        rc = 1; print("  tamaño inesperado", pb.get_width(), pb.get_height())

# coherencia seek vs tramos sobre el mismo intervalo (el menor): mismo fotograma (o adyacente)
S = min(intervals)
gen_a, cache_a, _ = run(S, force_mode="seek", cache=tmp / "cmp-seek")
gen_b, cache_b, _ = run(S, force_mode="range", cache=tmp / "cmp-range")
def mean_diff(p, q):
    a = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(p), 64, 64, True); b = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(q), 64, 64, True)
    pa, pb_ = bytes(a.get_pixels()), bytes(b.get_pixels()); n = min(len(pa), len(pb_))
    return sum(abs(pa[i] - pb_[i]) for i in range(n)) / float(n)
diffs = [(t, mean_diff(cache_a / ("%d.jpg" % t), cache_b / ("%d.jpg" % t))) for t in gen_a.timestamps if (cache_a / ("%d.jpg" % t)).exists() and (cache_b / ("%d.jpg" % t)).exists()]
worst = max(diffs, key=lambda x: x[1]) if diffs else (None, 0)
avg = sum(d for _, d in diffs) / max(1, len(diffs))
print("seek vs tramos: %d comparadas, diferencia media %.2f/255, peor %.2f en t=%s" % (len(diffs), avg, worst[1], worst[0]))
if avg > 6: rc = 1; print("  DEMASIADA DIFERENCIA entre modos")
shutil.rmtree(str(tmp), ignore_errors=True)
print("RESULTADO:", "OK" if rc == 0 else "FALLO")
sys.exit(rc)
