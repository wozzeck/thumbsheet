#!/usr/bin/env python3
"""Sin pantalla: vídeo que cambia de orientación a mitad (paisaje → retrato → paisaje, construido con el
demuxer concat a partir de un clip). Las miniaturas deben salir todas del tamaño nominal y el tramo vertical
con bandas laterales negras en vez de deformado, tanto en modo seek como en modo tramos.
Uso: tests/test_orientation.py <clip de vídeo horizontal de 8 s o más>"""
import os, sys, time, pathlib, shutil, subprocess, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("THUMBSHEET_DEBUG", "1")
import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GLib, GdkPixbuf
import thumbsheet as T

src = sys.argv[1]
tmp = pathlib.Path(tempfile.mkdtemp(prefix="thumbsheet-orient-"))
rc = 0
def ok(m): print("ok   " + m)
def ko(m):
    global rc; rc = 1; print("FAIL " + m)

def ff(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"] + list(args), check=True)
land, port, mixed = tmp / "land.mp4", tmp / "port.mp4", tmp / "mixed.mkv"
enc = ["-an", "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-pix_fmt", "yuv420p"]
ff("-t", "8", "-i", src, *enc, str(land))
ff("-t", "8", "-i", src, "-vf", "transpose=1", *enc, str(port))
(tmp / "list.txt").write_text("file '%s'\nfile '%s'\nfile '%s'\n" % (land, port, land))
ff("-f", "concat", "-safe", "0", "-i", str(tmp / "list.txt"), "-c", "copy", str(mixed))
info = T.VideoInfo(mixed)
print("fixture: %dx%d dur=%.1f gop=%s miniatura=%dx%d (retrato entre 8 y 16 s)" % (info.width, info.height, info.duration, info.gop, info.thumb_w, info.thumb_h))

def bars(path):
    """Columnas negras de borde a borde (bandas laterales)."""
    pb = GdkPixbuf.Pixbuf.new_from_file(str(path)); w, h, rs, n = pb.get_width(), pb.get_height(), pb.get_rowstride(), pb.get_n_channels()
    px = bytes(pb.get_pixels()); dark = 0
    for x in range(w):
        if all(sum(px[y * rs + x * n: y * rs + x * n + 3]) < 60 for y in range(0, h, 9)):
            dark += 1
    return (w, h, dark)

loop = GLib.MainLoop()
for S in (2, 1):
    cache = tmp / ("S%d" % S); cache.mkdir()
    st = {"done": False}
    gen = T.Generator(info, cache, S, lambda t: False, lambda d, n: False, lambda: (st.__setitem__("done", True), loop.quit()) and False)
    GLib.timeout_add(120000, loop.quit); gen.start()
    if not st["done"]: loop.run()
    bad_size, bad_bars, checked = [], [], 0
    for t in gen.timestamps:
        p = cache / ("%d.jpg" % t)
        if not p.exists(): bad_size.append(("falta", t)); continue
        w, h, dark = bars(p)
        if (w, h) != (info.thumb_w, info.thumb_h): bad_size.append((t, "%dx%d" % (w, h)))
        if abs(t - 8) <= 1 or abs(t - 16) <= 1: continue   # frontera: cualquiera de los dos vale
        checked += 1
        portrait = 8 < t < 16
        if portrait and dark < 100: bad_bars.append((t, "retrato sin bandas (%d)" % dark))
        if not portrait and dark > 20: bad_bars.append((t, "paisaje con bandas (%d)" % dark))
    if not bad_size: ok("S=%d modo=%s: %d miniaturas, todas de %dx%d" % (S, gen.mode, len(gen.timestamps), info.thumb_w, info.thumb_h))
    else: ko("S=%d: tamaños inesperados %s" % (S, bad_size[:5]))
    if not bad_bars: ok("S=%d: el tramo vertical sale con bandas laterales y el horizontal sin ellas (%d comprobadas)" % (S, checked))
    else: ko("S=%d: %s" % (S, bad_bars[:5]))
shutil.rmtree(str(tmp), ignore_errors=True)
print("RESULTADO:", "OK" if rc == 0 else "FALLO")
sys.exit(rc)
