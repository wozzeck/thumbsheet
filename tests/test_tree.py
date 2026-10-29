#!/usr/bin/env python3
"""Sin ventana, instantáneo: el panel anidado por carpetas. `is_cut_name` (qué cuenta como ya cortado), `pick_next`
(a qué vídeo se pasa al borrar/procesar/ocultar: el siguiente a la vista, si no el anterior; filas de carpeta y ocultos
se saltan), `walk_videos` (vídeos y TODAS las carpetas recorridas, también las vacías: son las que se vigilan) y las
claves de orden de `Folder` (raíz, sus vídeos por orden de llegada, luego cada subcarpeta —en orden natural por ruta— con los suyos).
Uso: tests/test_tree.py"""
import os, sys, pathlib, tempfile, shutil
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import thumbsheet as T

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


# --- is_cut_name
check(T.is_cut_name("peli-cortado.mp4") and T.is_cut_name("/x/y/peli-cortado-2.mkv") and T.is_cut_name("a-cortado-10.webm"),
      "is_cut_name: -cortado y -cortado-N")
check(not T.is_cut_name("peli.mp4") and not T.is_cut_name("cortado.mp4") and not T.is_cut_name("peli-cortado-x.mp4")
      and not T.is_cut_name("peli-cortado.mp4.part") and not T.is_cut_name("peli-recortado.mp4"),
      "is_cut_name: nada más cuenta (ni el temporal .part)")


# --- pick_next
class D(object):
    def __init__(self, name, hidden=False):
        self.name, self.hidden = name, hidden

    def __repr__(self):
        return self.name


a, b, c, d = D("a"), D("b", hidden=True), D("c"), D("d")
items = [a, b, c, d]
vis = lambda x: not x.hidden  # noqa: E731
check(T.pick_next(items, 1, vis) is c, "pick_next: tras a (pos 1) salta el oculto b y da c")
check(T.pick_next(items, 4, vis) is d, "pick_next: tras el último (pos 4) da el anterior, d")
d.hidden = True
check(T.pick_next(items, 3, vis) is c, "pick_next: tras c, sin nada visible después, da el anterior visible (c mismo no: pos 3 → antes)")
check(T.pick_next(items, 0, vis) is a, "pick_next: desde el principio, el primero visible")
check(T.pick_next([b, d], 0, vis) is None, "pick_next: todo oculto → None")
check(T.pick_next([], 0, vis) is None, "pick_next: lista vacía → None")

# --- walk_videos: vídeos como find_videos y las carpetas recorridas, también las vacías
tmp = pathlib.Path(tempfile.mkdtemp(prefix="ts-tree-"))
try:
    def touch(rel):
        f = tmp / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    for rel in ("b.mp4", "a10.mp4", "a2.MP4", "notas.txt", ".oculto.mp4", "sub/c.mkv", "sub/hondo/d.webm", ".escondida/f.mp4"):
        touch(rel)
    (tmp / "vacia" / "masvacia").mkdir(parents=True)
    (tmp / "sinvideos").mkdir()
    (tmp / "sinvideos" / "texto.txt").write_bytes(b"x")
    os.symlink(str(tmp / "sub"), str(tmp / "enlace"))
    found, seen = T.walk_videos([tmp])
    check([str(f.relative_to(tmp)) for f in found] == ["a2.MP4", "a10.mp4", "b.mp4", "sub/c.mkv", "sub/hondo/d.webm"],
          "walk_videos: los vídeos como find_videos: %s" % [str(f.relative_to(tmp)) for f in found])
    check(found == T.find_videos([tmp]), "find_videos sigue devolviendo lo mismo")
    rel = sorted(str(s.relative_to(tmp)) for s in seen)
    check(rel == [".", "sinvideos", "sub", "sub/hondo", "vacia", "vacia/masvacia"],
          "walk_videos: carpetas recorridas = raíz y todas las subcarpetas (vacías y sin vídeos incluidas), sin ocultas ni "
          "enlaces: %s" % rel)
    check(T.walk_videos([tmp / "no-existe"]) == ([], []), "walk_videos: carpeta inexistente → nada, sin excepción")

    # --- Folder: claves de orden del panel
    root = T.Folder(tmp, None, 0)
    sub = T.Folder(tmp / "sub", root)
    hondo = T.Folder(tmp / "sub" / "hondo", root)
    sub10 = T.Folder(tmp / "sub10", root)
    check(sub.rel[1] == "sub" and hondo.rel[1] == "sub/hondo" and root.rel[1] == "", "Folder.rel: ruta relativa a la raíz")
    check(sub.sort_key() < hondo.sort_key() < sub10.sort_key(), "subcarpetas en orden natural por ruta: sub < sub/hondo < sub10")
    keys = [root.sort_key(), root.doc_key(1), root.doc_key(2), root.doc_key(3), sub.sort_key(), sub.doc_key(1),
            hondo.sort_key(), hondo.doc_key(1), hondo.doc_key(2), sub10.sort_key()]
    check(keys == sorted(keys), "orden del panel: raíz, sus vídeos por orden de llegada, luego cada subcarpeta con los suyos")
    other = T.Folder(tmp / "otra", None, 1)
    check(sub10.sort_key() < other.sort_key() and hondo.doc_key(99) < other.sort_key(),
          "una segunda raíz va después de todo el árbol de la primera")
finally:
    shutil.rmtree(str(tmp), ignore_errors=True)
print("TODO OK" if not fails else "HAY FALLOS: %d" % fails)
sys.exit(1 if fails else 0)
