#!/usr/bin/env python3
"""Sin ventana, instantáneo: find_videos recorre carpetas y subcarpetas y devuelve sólo los ficheros soportados
(por extensión, sin distinguir mayúsculas), sin ocultos, sin seguir enlaces a carpetas, en orden natural por ruta.
Uso: tests/test_folders.py"""
import os, sys, pathlib, tempfile, shutil
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import thumbsheet as T

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


tmp = pathlib.Path(tempfile.mkdtemp(prefix="ts-folders-"))
try:
    def touch(rel):
        f = tmp / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    for rel in ("b.mp4", "a10.mp4", "a2.MP4", "notas.txt", "foto.jpg", ".oculto.mp4", "sub/c.mkv", "sub/hondo/d.webm",
                "sub/hondo/e.MOV", ".escondida/f.mp4", "sub/.parte.mp4", "otra/z.avi"):
        touch(rel)
    os.symlink(str(tmp / "sub"), str(tmp / "otra" / "bucle"))        # enlace a carpeta: no se sigue
    os.symlink(str(tmp / "b.mp4"), str(tmp / "otra" / "enlace.mp4"))  # enlace a fichero: sí cuenta
    found = [str(f.relative_to(tmp)) for f in T.find_videos([tmp])]
    esperado = ["a2.MP4", "a10.mp4", "b.mp4", "otra/enlace.mp4", "otra/z.avi", "sub/c.mkv", "sub/hondo/d.webm", "sub/hondo/e.MOV"]
    check(found == esperado, "carpeta con subcarpetas: %s" % found)
    check("notas.txt" not in found and "foto.jpg" not in found, "sin ficheros no soportados")
    check(not any(p.startswith(".") or "/." in p for p in found), "sin ocultos ni carpetas ocultas")
    check(not any("bucle" in p for p in found), "no sigue enlaces a carpetas")
    check(found.index("a2.MP4") < found.index("a10.mp4"), "orden natural: a2 antes que a10")
    sub = [str(f.relative_to(tmp)) for f in T.find_videos([tmp / "sub", tmp / "otra"])]
    check(sub == ["otra/enlace.mp4", "otra/z.avi", "sub/c.mkv", "sub/hondo/d.webm", "sub/hondo/e.MOV"], "varias carpetas a la vez: %s" % sub)
    check(T.find_videos([tmp / "no-existe"]) == [], "carpeta inexistente: lista vacía, sin excepción")
    check(T.is_video_name("Pelicula.MKV") and not T.is_video_name(".x.mp4") and not T.is_video_name("a.mp4.txt"), "is_video_name")
    check(T.natural_key("v10") > T.natural_key("v9") and T.natural_key("B") == T.natural_key("b"), "natural_key")
finally:
    shutil.rmtree(str(tmp), ignore_errors=True)
print("TODO OK" if not fails else "HAY FALLOS: %d" % fails)
sys.exit(1 if fails else 0)
