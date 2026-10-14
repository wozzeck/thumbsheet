#!/usr/bin/env python3
"""Borrado robusto, sin ventana: remove_with_retry reintenta ante «ocupado» y Generator.quiesce espera a los ffmpeg
de un vídeo cancelado antes de borrarlo (en montajes remotos borrar un fichero abierto puede fallar).
Uso: tests/test_delete.py <vídeo>"""
import errno
import os
import pathlib
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("XDG_CACHE_HOME", tempfile.mkdtemp(prefix="ts-delete-"))
import thumbsheet as ts  # noqa: E402

fails = 0


def check(cond, what):
    global fails
    print(("ok   " if cond else "FAIL ") + what)
    fails += 0 if cond else 1


# 1. remove_with_retry: EBUSY dos veces y luego se borra; EISDIR (no pasajero) sale a la primera
tmp = pathlib.Path(tempfile.mkdtemp(prefix="ts-rm-"))
f = tmp / "a.bin"
f.write_bytes(b"x")
real_remove, calls = os.remove, []


def flaky(path):
    calls.append(path)
    if len(calls) <= 2:
        raise OSError(errno.EBUSY, os.strerror(errno.EBUSY), path)
    real_remove(path)


os.remove = flaky
t0 = time.monotonic()
err = ts.remove_with_retry(f, 5.0)
os.remove = real_remove
check(err is None and not f.exists() and len(calls) == 3, "EBUSY dos veces: se borra al tercer intento (%d llamadas, %.1f s)" % (len(calls), time.monotonic() - t0))
err = ts.remove_with_retry(tmp / "no-existe.bin", 1.0)
check(err is None, "si ya no existe cuenta como borrado")
t0 = time.monotonic()
err = ts.remove_with_retry(tmp, 5.0)   # es un directorio: EISDIR/EPERM según sistema; en Linux os.remove da EISDIR
check(err is not None and time.monotonic() - t0 < 1.0, "error no pasajero: falla a la primera (%s)" % (err or "").splitlines()[-1][:60])


def busy_forever(path):
    raise OSError(errno.EBUSY, os.strerror(errno.EBUSY), path)


os.remove = busy_forever
t0 = time.monotonic()
err = ts.remove_with_retry(f, 0.6)
os.remove = real_remove
check(err is not None and "abierto en otro programa" in err and 0.5 < time.monotonic() - t0 < 2.0, "ocupado permanente: agota el plazo y lo dice (%.1f s)" % (time.monotonic() - t0))
shutil.rmtree(str(tmp), ignore_errors=True)

# 2. quiesce: generador en marcha sobre un vídeo, cancelado; tras quiesce no queda ningún ffmpeg suyo
if len(sys.argv) > 1:
    src = pathlib.Path(sys.argv[1])
    work = pathlib.Path(tempfile.mkdtemp(prefix="ts-q-"))
    video = work / ("copia-" + src.name)
    shutil.copy2(str(src), str(video))
    info = ts.VideoInfo(video)
    cache = work / "cache"
    cache.mkdir()
    gen = ts.Generator(info, cache, 1, lambda t: False, lambda d, n: False, lambda: False)
    gen.start()
    for _ in range(100):   # hasta que haya ffmpeg en marcha
        with gen.lock:
            n = len(gen.procs)
        if n:
            break
        time.sleep(0.05)
    check(n > 0, "generador en marcha con %d ffmpeg" % n)
    gen.cancel()
    t0 = time.monotonic()
    ok = ts.Generator.quiesce(video.resolve(), 6.0)
    with gen.lock:
        alive = [p for p in gen.procs if p.poll() is None]
    check(ok and not alive, "quiesce: sin ffmpeg vivos a los %.2f s de cancelar" % (time.monotonic() - t0))
    check(ts.Generator.quiesce(work / "otro.mp4", 1.0), "quiesce de otro vídeo: no espera nada")
    err = ts.remove_with_retry(video, 2.0)
    check(err is None and not video.exists(), "el vídeo se borra tras quiesce")
    shutil.rmtree(str(work), ignore_errors=True)
else:
    print("(sin vídeo: se omite la prueba de quiesce)")

shutil.rmtree(os.environ["XDG_CACHE_HOME"], ignore_errors=True)
print("RESULTADO:", "OK" if not fails else "%d fallos" % fails)
sys.exit(1 if fails else 0)
