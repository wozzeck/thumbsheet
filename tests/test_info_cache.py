#!/usr/bin/env python3
"""Sin ventana, unos segundos: el sondeo de un vídeo se guarda en su caché (`info.json`) y en otra sesión se reutiliza
sin lanzar ffprobe, con los mismos datos; uno corrupto, de otra versión o con valores imposibles se ignora y se vuelve a
sondear (y se reescribe); si el fichero cambia (fecha), su caché es otra y se sondea de nuevo. `ensure_info` dice si esa
llamada hizo el trabajo (la cola y el sondeo urgente del vídeo a la vista pueden pedirlo a la vez). Y el recuento de
caché de los iconos del panel (`_doc_coverage`): capturas del plan hechas o marcadas, sin contar las de otros intervalos.
Uso: tests/test_info_cache.py <vídeo>"""
import os, sys, pathlib, tempfile, shutil, json, subprocess, time
tmp = pathlib.Path(tempfile.mkdtemp(prefix="ts-infocache-"))
os.environ["XDG_CACHE_HOME"] = str(tmp / "cache")   # antes de importar: fija CACHE_ROOT
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import thumbsheet as T

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


probes = []
_run = subprocess.run


def counting_run(cmd, *a, **kw):   # cuenta los ffprobe que lanza el sondeo
    if cmd and os.path.basename(str(cmd[0])) == "ffprobe":
        probes.append(cmd)
    return _run(cmd, *a, **kw)


subprocess.run = counting_run
FIELDS = ("duration", "width", "height", "aspect", "fps", "rotated", "codec", "has_audio", "gop", "thumb_w", "thumb_h")
try:
    video = tmp / ("v" + pathlib.Path(sys.argv[1]).suffix)
    shutil.copy(sys.argv[1], str(video))

    d1 = T.Document(video)
    check(d1.ensure_info() is True and d1.info is not None and d1.error is None, "primera vez: se sondea")
    check(len(probes) >= 1 and not d1.info.from_cache, "primera vez: con ffprobe (%d llamadas)" % len(probes))
    f = d1.cache_dir / T.INFO_FILE
    data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    check(data.get("v") == T.INFO_VERSION and data.get("duration") == d1.info.duration,
          "queda guardado en %s/%s" % (d1.cache_dir.name, T.INFO_FILE))
    check(not list(d1.cache_dir.glob(".*")), "sin temporales a la vista en la caché")
    check(d1.ensure_info() is False, "segunda llamada sobre el mismo vídeo: no hace nada (False)")

    n0 = len(probes)
    t0 = time.perf_counter()
    d2 = T.Document(video)   # «otra sesión»
    check(d2.ensure_info() is True and d2.info is not None and d2.info.from_cache, "otra sesión: datos de info.json")
    dt = 1000 * (time.perf_counter() - t0)
    check(len(probes) == n0, "otra sesión: ningún ffprobe (%.1f ms)" % dt)
    same = [k for k in FIELDS if getattr(d1.info, k) != getattr(d2.info, k)]
    check(not same, "otra sesión: mismos datos que el sondeo (%s)" % (", ".join(same) if same else "todos los campos"))
    check(d2.cache_dir == d1.cache_dir, "otra sesión: la misma caché")

    for label, content in (("corrupto", "{no es json"), ("de otra versión", json.dumps(dict(data, v=999))),
                           ("con duración imposible", json.dumps(dict(data, duration=0))),
                           ("con un campo de menos", json.dumps(dict((k, v) for k, v in data.items() if k != "fps")))):
        f.write_text(content, encoding="utf-8")
        n0 = len(probes)
        d = T.Document(video)
        d.ensure_info()
        again = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        check(d.info is not None and not d.info.from_cache and len(probes) > n0 and again.get("v") == T.INFO_VERSION,
              "info.json %s: se ignora, se sondea y se reescribe" % label)

    st = video.stat()
    os.utime(str(video), ns=(st.st_atime_ns, st.st_mtime_ns + 5 * 10 ** 9))
    n0 = len(probes)
    d3 = T.Document(video)
    d3.ensure_info()
    check(d3.cache_dir != d1.cache_dir and not d3.info.from_cache and len(probes) > n0,
          "el fichero cambia (fecha): otra caché y se vuelve a sondear")

    bad = tmp / "texto.mp4"
    bad.write_text("no soy un vídeo", encoding="utf-8")
    d4 = T.Document(bad)
    check(d4.ensure_info() is True and d4.error and d4.info is None, "no es un vídeo: error (%s)" % d4.error)
    check(d4.ensure_info() is False, "con error: no se reintenta en la misma sesión")

    # recuento de caché de las filas del panel
    class Info(object):
        duration = 9.5
    cov_dir = tmp / "cov"
    cov_dir.mkdir()
    doc = T.Document(video)
    doc.info, doc.cache_dir = Info(), cov_dir
    check(T.ThumbSheet._doc_coverage(doc, 5) == (0, 2), "caché vacía: 0 de 2 (S=5 en 9,5 s: 0 y 5)")
    for name in ("0.jpg", "1.jpg", "2.jpg", "100000.jpg", "x.tmp", "selection.json", "info.json"):
        (cov_dir / name).write_text("", encoding="utf-8")
    check(T.ThumbSheet._doc_coverage(doc, 5) == (1, 2), "S=5: 1 de 2 (las de otros intervalos y los demás ficheros no cuentan)")
    (cov_dir / "5.fail").write_text("", encoding="utf-8")
    check(T.ThumbSheet._doc_coverage(doc, 5) == (2, 2), "S=5: una marcada como imposible (.fail) cuenta como hecha")
    check(T.ThumbSheet._doc_coverage(doc, 1) == (4, 10), "S=1: 0, 1, 2 y 5 de los 10 instantes (0 a 9)")
    doc.cache_dir = tmp / "no-existe"
    check(T.ThumbSheet._doc_coverage(doc, 5) == (0, 2), "caché inexistente: 0, sin excepción")
finally:
    subprocess.run = _run
    shutil.rmtree(str(tmp), ignore_errors=True)
print("TODO OK" if not fails else "FALLOS: %d" % fails)
sys.exit(1 if fails else 0)
