#!/usr/bin/env python3
"""Sin ventana: el cargador de miniaturas (Loader) atiende lo visible antes que la precarga, lo visible en orden
«último pedido primero» (la sábana pide los huecos invertidos para que salgan de arriba abajo), descarta lo que ya
no está ni a la vista ni en el margen, y una petición visible saca la tesela de la cola de precarga.
Uso: tests/test_loader.py"""
import sys, time, pathlib, tempfile, shutil
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf
import thumbsheet as T

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


tmp = pathlib.Path(tempfile.mkdtemp(prefix="ts-loader-"))
try:
    for t in range(1, 11):
        pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 64, 36)
        pb.fill(0x3060A0FF)
        pb.savev(str(tmp / ("%d.jpg" % t)), "jpeg", [], [])

    class RecCache(T.PixCache):
        def __init__(self):
            super().__init__(64 * 1024 * 1024)
            self.order = []

        def put(self, t, surface):
            self.order.append(t)
            super().put(t, surface)

    def wait_for(cache, n, timeout=5.0):
        t0 = time.time()
        while len(cache.order) < n and time.time() - t0 < timeout:
            time.sleep(0.01)
        time.sleep(0.15)   # margen por si saliera alguna de más
        return list(cache.order)

    # 1. prioridad: visibles (último pedido primero) y luego precarga
    cache = RecCache()
    L = T.Loader(cache, lambda: None, lambda loaded: None)
    with L.cv:   # se encolan de golpe bajo el cerrojo para que el hilo no empiece a medias
        for t in (5, 4, 3, 2, 1):            # la sábana pide los huecos invertidos: sale 1, 2, 3, 4, 5
            L.queue[t] = (tmp / ("%d.jpg" % t), 32, 18)
        for t in (6, 7, 8, 9, 10):           # precarga
            L.back[t] = (tmp / ("%d.jpg" % t), 32, 18)
        L.cv.notify()
    order = wait_for(cache, 10)
    check(order[:5] == [1, 2, 3, 4, 5], "lo visible primero y de arriba abajo: %s" % order[:5])
    check(sorted(order[5:]) == [6, 7, 8, 9, 10] and len(order) == 10, "la precarga después: %s" % order[5:])

    # 2. descarte de lo que ya no se ve: sólo 2 y 3 están a la vista
    cache = RecCache()
    L = T.Loader(cache, lambda: {2, 3}, lambda loaded: None)
    with L.cv:
        for t in (5, 4, 3, 2, 1):
            L.queue[t] = (tmp / ("%d.jpg" % t), 32, 18)
        for t in (6, 7):
            L.back[t] = (tmp / ("%d.jpg" % t), 32, 18)
        L.cv.notify()
    order = wait_for(cache, 2)
    check(sorted(order) == [2, 3], "fuera de la vista se descarta: decodificadas %s" % order)
    with L.cv:
        check(not L.queue and not L.back, "colas vacías tras descartar")

    # 3. semántica de request(): sin hilo que consuma (se anula _run)
    T.Loader._run = lambda self: None
    L = T.Loader(RecCache(), lambda: None, lambda loaded: None)
    L.request(1, tmp / "1.jpg", 32, 18)
    L.request(1, tmp / "1.jpg", 32, 18, prefetch=True)
    check(1 in L.queue and 1 not in L.back, "una tesela ya pedida como visible no pasa a precarga")
    L.request(2, tmp / "2.jpg", 32, 18, prefetch=True)
    L.request(2, tmp / "2.jpg", 32, 18)
    check(2 in L.queue and 2 not in L.back, "una petición visible saca la tesela de la precarga")
    L.request(3, tmp / "3.jpg", 32, 18, prefetch=True)
    L.request(1, tmp / "1.jpg", 32, 18)
    check(list(L.queue) == [2, 1] and list(L.back) == [3], "repetir una visible la pone la última (se atiende la primera): %s / %s" % (list(L.queue), list(L.back)))
    L.cancel_all()
    check(not L.queue and not L.back, "cancel_all vacía las dos colas")
finally:
    shutil.rmtree(str(tmp), ignore_errors=True)
print("TODO OK" if not fails else "HAY FALLOS: %d" % fails)
sys.exit(1 if fails else 0)
