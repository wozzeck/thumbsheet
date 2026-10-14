#!/usr/bin/env python3
"""Sin pantalla: vídeos que no se pueden abrir o que no dan ningún fotograma no paran la cola.
1) Un fichero de texto con extensión .mp4: VideoInfo falla con un mensaje de UNA línea (sin «[mov,mp4 @ 0x…]»
   ni la ruta), y short_error resume bien un stderr típico de ffmpeg.
2) Document.ensure_info no deja escapar excepciones (caché que no se puede crear): el documento queda en error.
3) Copia del vídeo con los datos (mdat) a ceros: ffprobe da duración y resolución, pero ffmpeg no saca nada.
   El generador desiste en segundos tras unas pocas capturas (modo seek y modo tramos), avisa (on_done,
   gen.error), no deja marcas .fail ni jpg, y una segunda pasada vuelve a probar (y desiste igual).
4) Vídeo dañado sólo al principio (las primeras capturas fallan, simulado): la comprobación en mitad del vídeo
   sale bien y el generador NO desiste; las fallidas quedan marcadas como siempre.
5) Carrera con GPU: si la GPU falla con los últimos tramos cuando los workers de CPU ya han terminado, los tramos
   se reencolan y el generador tiene que acabar igualmente (antes quedaba sin terminar y el segundo plano parado).
Uso: tests/test_errors.py <vídeo mp4 faststart (moov antes que mdat), de más de ~2 min>"""
import os, sys, time, pathlib, shutil, struct, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("THUMBSHEET_DEBUG", "1")
import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GLib
import thumbsheet as T

video = pathlib.Path(sys.argv[1]).resolve()
tmp = pathlib.Path(tempfile.mkdtemp(prefix="thumbsheet-errors-"))
rc = 0
def ok(m): print("ok   " + m)
def ko(m):
    global rc; rc = 1; print("FAIL " + m)

# --- ficheros de prueba ---
data = bytearray(video.read_bytes())
off, atoms = 0, []
while off + 8 <= len(data):
    size, typ = struct.unpack(">I4s", data[off:off + 8])
    if size == 1:
        size = struct.unpack(">Q", data[off + 8:off + 16])[0]
    if size == 0:
        size = len(data) - off
    atoms.append((typ.decode("latin-1"), off, size)); off += size
names = [a[0] for a in atoms]
if "moov" not in names or "mdat" not in names or names.index("moov") > names.index("mdat"):
    sys.exit("el vídeo debe ser mp4 con moov antes que mdat (faststart): %s" % names)
_, mo, ms = [a for a in atoms if a[0] == "mdat"][0]
zero = tmp / "zero.mp4"
z = bytearray(data); z[mo + 8:mo + ms] = b"\0" * (ms - 8); zero.write_bytes(z); del z
text = tmp / "text.mp4"; text.write_text("esto no es un vídeo\n" * 100)

# 1. no se puede abrir: mensaje de una línea
try:
    T.VideoInfo(text); ko("VideoInfo aceptó un fichero de texto")
except RuntimeError as e:
    msg = str(e)
    if msg and "\n" not in msg and "@ 0x" not in msg and "text.mp4" not in msg: ok("fichero de texto: VideoInfo falla con mensaje de una línea: «%s»" % msg)
    else: ko("mensaje de VideoInfo: %r" % msg)
s = T.short_error("[h264 @ 0x1] Invalid NAL unit size\n[vist#0:0/h264 @ 0x2] Decode error rate 1 exceeds maximum 0.666667\n"
                  "[out#0/image2 @ 0x3] Nothing was written into output file, because at least one of its streams received no packets.\n"
                  "Conversion failed!\n")
if s == "Decode error rate 1 exceeds maximum 0.666667": ok("short_error: salta las líneas genéricas y quita el prefijo «[… @ 0x…]»")
else: ko("short_error: %r" % s)
s = T.short_error("[mov,mp4 @ 0x1] moov atom not found\n/tmp/x y/vídeo.mp4: Invalid data found when processing input\n")
if s == "Invalid data found when processing input (moov atom not found)": ok("short_error: quita la ruta y añade la causa anterior")
else: ko("short_error: %r" % s)
if T.short_error("") == "error" and T.short_error("x" * 500).endswith("…") and len(T.short_error("x" * 500)) == 160: ok("short_error: vacío → por defecto; largo → acortado")
else: ko("short_error: vacío=%r largo=%d" % (T.short_error(""), len(T.short_error("x" * 500))))

# 2. ensure_info nunca deja escapar excepciones
old_root = T.CACHE_ROOT
T.CACHE_ROOT = pathlib.Path("/proc/thumbsheet-no-se-puede-crear")
try:
    doc = T.Document(video); doc.ensure_info()
    if doc.error and doc.info is None: ok("ensure_info: caché imposible de crear → documento en error («%s»)" % doc.error)
    else: ko("ensure_info: error=%r info=%r" % (doc.error, doc.info))
except Exception as e:  # noqa: BLE001
    ko("ensure_info dejó escapar %r" % e)
finally:
    T.CACHE_ROOT = old_root

# --- generador ---
loop = GLib.MainLoop()

def run(info, S, cache, force_mode=None, timeout_s=90):
    cache.mkdir(parents=True, exist_ok=True)
    got, failed, state = set(), set(), {"done": False, "t0": time.time()}
    gen = T.Generator(info, cache, S, lambda t: got.add(t) or False, lambda d, n: False,
                      lambda: (state.__setitem__("done", True), loop.quit()) and False,
                      lambda t: failed.add(t) or False)
    if force_mode: gen.mode = force_mode
    tid = GLib.timeout_add(int(timeout_s * 1000), loop.quit)
    gen.start()
    if not state["done"]: loop.run()
    GLib.source_remove(tid) if not state["done"] else None
    el = time.time() - state["t0"]
    jpg = len(list(cache.glob("*.jpg"))); marks = len(list(cache.glob("*.fail")))
    print("S=%-3d modo=%-5s capturas=%d done=%s error=%r ok=%d fallidas=%d jpg=%d fail=%d en %.1fs" % (
        S, gen.mode, gen.total, state["done"], gen.error, len(got), len(failed), jpg, marks, el))
    return gen, state["done"], got, failed, jpg, marks, el

# 3. datos a ceros: desiste pronto, sin marcas
info_z = T.VideoInfo(zero)
print("vídeo a ceros: dur=%.1f s %dx%d gop=%s" % (info_z.duration, info_z.width, info_z.height, info_z.gop))
for mode in ("seek", "range"):
    cache = tmp / ("zero-" + mode)
    for ronda in (1, 2):
        gen, done, got, failed, jpg, marks, el = run(info_z, 10, cache, mode)
        tag = "datos a ceros, modo %s, pasada %d" % (mode, ronda)
        if done and gen.error: ok("%s: desiste y avisa (on_done) en %.1f s: «%s»" % (tag, el, gen.error))
        elif done: ko("%s: terminó sin error (lo dio por bueno)" % tag)
        else: ko("%s: no terminó en %.0f s (cola parada)" % (tag, el))
        if marks == 0 and jpg == 0: ok("%s: sin marcas .fail ni jpg en la caché" % tag)
        else: ko("%s: jpg=%d fail=%d" % (tag, jpg, marks))
        lim = T.Generator.GIVE_UP_FAILS + T.default_workers() + 2
        if gen.hard_fails <= lim and len(failed) <= lim: ok("%s: sólo %d capturas intentadas (tope %d)" % (tag, gen.hard_fails, lim))
        else: ko("%s: %d capturas fallidas, avisadas %d (tope %d)" % (tag, gen.hard_fails, len(failed), lim))
        if el < 45: ok("%s: %.1f s" % (tag, el))
        else: ko("%s: demasiado lento: %.1f s" % (tag, el))
        left = [p for p in gen.procs if p.poll() is None]
        if not left: ok("%s: sin ffmpeg vivos" % tag)
        else: ko("%s: %d ffmpeg vivos" % (tag, len(left)))

# 4. dañado sólo al principio (simulado): no desiste
info = T.VideoInfo(video)
orig_seek = T.Generator._do_seek
bad_until = info.duration * 0.3
def seek_bad_start(self, t):
    if t < bad_until:
        self._tile_failed(t, permanent=True)
        return False
    return orig_seek(self, t)
T.Generator._do_seek = seek_bad_start
try:
    cache = tmp / "head"
    gen, done, got, failed, jpg, marks, el = run(info, 120, cache, "seek")
    exp_bad = sum(1 for t in gen.timestamps if t < bad_until)
    if done and gen.error is None: ok("dañado al principio: no desiste (la captura del medio salió)")
    else: ko("dañado al principio: done=%s error=%r" % (done, gen.error))
    if marks == exp_bad and jpg == gen.total - exp_bad: ok("dañado al principio: %d marcadas .fail y %d capturas, como siempre" % (marks, jpg))
    else: ko("dañado al principio: fail=%d (esperadas %d) jpg=%d (esperadas %d)" % (marks, exp_bad, jpg, gen.total - exp_bad))
finally:
    T.Generator._do_seek = orig_seek

# 5. carrera con GPU: fallo en los últimos tramos con los workers de CPU ya terminados
one = next(iter((tmp / "head").glob("*.jpg")))
JPG = one.read_bytes()
T.Gpu.device = classmethod(lambda cls: "/dev/dri/fake")
T.Gpu.selftest = staticmethod(lambda info, dev, d: True)
orig_range = T.Generator._do_range
gpu_fail = {"n": 0}
def fake_range(self, ts, dev):
    if dev is not None:   # «GPU»: espera a que los workers de CPU hayan acabado todo lo demás y entonces falla
        while any(th.is_alive() for th in self.threads if th.name.startswith("ts-cpu")):
            time.sleep(0.02)
        gpu_fail["n"] += 1
        return False
    for t in ts:
        time.sleep(0.03)
        (self.cache_dir / ("%d.jpg" % t)).write_bytes(JPG)
        self._tile_ready(t)
    return True
T.Generator._do_range = fake_range
try:
    cache = tmp / "gpu"
    gen, done, got, failed, jpg, marks, el = run(info, 5, cache, "range", timeout_s=30)
    if gpu_fail["n"] == 0: ko("carrera GPU: la GPU simulada no llegó a coger ningún tramo (prueba no concluyente)")
    elif done and gen.error is None and jpg == gen.total: ok("carrera GPU: %d tramos fallidos en GPU al final se rehacen y el generador termina (%d/%d)" % (gpu_fail["n"], jpg, gen.total))
    else: ko("carrera GPU: done=%s error=%r jpg=%d/%d (fallos GPU=%d): el generador no termina" % (done, gen.error, jpg, gen.total, gpu_fail["n"]))
finally:
    T.Generator._do_range = orig_range

shutil.rmtree(str(tmp), ignore_errors=True)
print("RESULTADO:", "OK" if rc == 0 else "FALLO")
sys.exit(rc)
