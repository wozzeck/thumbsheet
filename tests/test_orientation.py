#!/usr/bin/env python3
"""Sin pantalla: vídeo que cambia de orientación a mitad (paisaje → retrato → paisaje, construido con el
demuxer concat a partir de un clip). Cada miniatura debe salir con SU proporción real (las verticales,
verticales; sin bandas ni deformación), el generador debe avisar de las no nominales (on_aspect) y dejarlas
apuntadas en aspects.json, tanto en modo seek como en modo tramos (donde ffmpeg las saca deformadas y se
re-encajan al terminar el tramo).
Uso: tests/test_orientation.py <clip de vídeo horizontal de 8 s o más>"""
import os, sys, time, json, pathlib, shutil, subprocess, tempfile
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

def fit_box_dims(info):
    return T.fit_box(576.0 / 1024, info.thumb_w, info.thumb_h)
aspects_seen = {}
loop = GLib.MainLoop()
for S in (2, 1):
    cache = tmp / ("S%d" % S); cache.mkdir()
    st = {"done": False}
    aspects_seen[S] = {}
    gen = T.Generator(info, cache, S, lambda t: False, lambda d, n: False, lambda: (st.__setitem__("done", True), loop.quit()) and False,
                      on_aspect=lambda t, a: aspects_seen[S].__setitem__(t, a) or False)
    GLib.timeout_add(120000, loop.quit); gen.start()
    if not st["done"]: loop.run()
    bad, checked = [], 0
    try: side = {int(k): float(v) for k, v in json.loads((cache / "aspects.json").read_text()).items()}
    except (OSError, ValueError): side = {}
    for t in gen.timestamps:
        p = cache / ("%d.jpg" % t)
        if not p.exists(): bad.append(("falta", t)); continue
        w, h, dark = bars(p)
        if dark > 20: bad.append((t, "bandas negras (%d columnas)" % dark))
        if abs(t - 8) <= 1 or abs(t - 16) <= 1: continue   # frontera: cualquiera de los dos vale
        checked += 1
        portrait = 8 < t < 16
        if portrait:
            if not (w < h and h == info.thumb_h): bad.append((t, "retrato esperado, %dx%d" % (w, h)))
            if abs(side.get(t, 0) - 576.0 / 1024) > 0.02: bad.append((t, "sin aspecto 0.56 en aspects.json (%s)" % side.get(t)))
        else:
            if (w, h) != (info.thumb_w, info.thumb_h): bad.append((t, "paisaje esperado %dx%d, %dx%d" % (info.thumb_w, info.thumb_h, w, h)))
            if t in side: bad.append((t, "paisaje apuntado como no nominal"))
    if not bad: ok("S=%d modo=%s: %d miniaturas con su proporción real (retrato %dx%d, paisaje %dx%d), sin bandas, y aspects.json coherente (%d comprobadas)" % (
        S, gen.mode, len(gen.timestamps), *fit_box_dims(info), info.thumb_w, info.thumb_h, checked))
    else: ko("S=%d modo=%s: %s" % (S, gen.mode, bad[:6]))
    if aspects_seen[S] and all(abs(a - 576.0 / 1024) < 0.02 for a in aspects_seen[S].values()) and all(8 <= t <= 16 for t in aspects_seen[S]):
        ok("S=%d: on_aspect avisó de %d teselas verticales" % (S, len(aspects_seen[S])))
    else: ko("S=%d: on_aspect: %s" % (S, sorted(aspects_seen[S].items())[:6]))
shutil.rmtree(str(tmp), ignore_errors=True)
print("RESULTADO:", "OK" if rc == 0 else "FALLO")
sys.exit(rc)
