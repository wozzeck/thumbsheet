#!/usr/bin/env python3
"""Sin ventana, instantáneo: el registro en disco de los borrados programados por Eliminar (`PendingDeletes`, fichero
`pending-delete.json` en la configuración): añadir (sin repetir ruta), quitar, releer, fichero ausente o corrupto, y
que sin entradas no queda fichero. También que un vídeo a la espera de borrarse (`doomed`) no es un vecino al que pasar.
Uso: tests/test_pending.py"""
import os, sys, pathlib, tempfile, shutil, json
tmp = pathlib.Path(tempfile.mkdtemp(prefix="ts-pending-"))
os.environ["XDG_CONFIG_HOME"] = str(tmp)   # antes de importar: fija PENDING_FILE
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import thumbsheet as T

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


try:
    P = T.PENDING
    f = T.PENDING_FILE
    check(f == tmp / "thumbsheet" / "pending-delete.json", "el registro vive junto a settings.json: %s" % f)
    check(P.load() == [] and not f.exists(), "sin fichero: ninguna entrada")
    a, b = tmp / "v" / "a.mp4", tmp / "v" / "b.mkv"
    P.add(a, tmp / "cache" / "aaaa")
    check(f.exists() and P.load() == [{"path": str(a), "cache": str(tmp / "cache" / "aaaa")}], "add: una entrada con ruta y caché")
    P.add(b, None)
    check([x["path"] for x in P.load()] == [str(a), str(b)] and P.load()[1]["cache"] is None, "add: segunda entrada, sin caché")
    P.add(a, tmp / "cache" / "bbbb")
    check(len(P.load()) == 2 and P.load()[1]["path"] == str(a) and P.load()[1]["cache"].endswith("bbbb"),
          "add de una ruta repetida: no se duplica, se actualiza (y pasa al final)")
    P.remove(tmp / "v" / "no-esta.mp4")
    check(len(P.load()) == 2, "remove de algo que no está: sin cambios")
    P.remove(b)
    check([x["path"] for x in P.load()] == [str(a)], "remove: quita sólo esa entrada")
    P.remove(a)
    check(P.load() == [] and not f.exists(), "sin entradas: el fichero desaparece")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{esto no es json", encoding="utf-8")
    check(P.load() == [], "fichero corrupto: ninguna entrada, sin excepción")
    P.add(b, None)
    check(P.load() == [{"path": str(b), "cache": None}], "tras un fichero corrupto se puede volver a apuntar")
    f.write_text(json.dumps([{"cache": "x"}, "basura", {"path": str(a), "cache": None}]), encoding="utf-8")
    check(P.load() == [{"path": str(a), "cache": None}], "entradas sin ruta o que no son objetos se ignoran")

    class D(object):
        def __init__(self):
            self.row, self.hidden, self.deleting, self.doomed = object(), False, False, False
    d = D()
    check(T.ThumbSheet._usable(d), "un vídeo normal es un vecino al que pasar")
    d.doomed = True
    check(not T.ThumbSheet._usable(d), "a la espera de borrarse (doomed): no se pasa a él")
    check(T.Document(tmp / "v" / "x.mp4").doomed is False, "Document nace sin borrado programado")
    check(T.DELETE_DELAY == 20.0, "20 s de gracia por defecto (THUMBSHEET_DELETE_DELAY): %s" % T.DELETE_DELAY)
finally:
    shutil.rmtree(str(tmp), ignore_errors=True)
print("TODO OK" if not fails else "HAY FALLOS: %d" % fails)
sys.exit(1 if fails else 0)
