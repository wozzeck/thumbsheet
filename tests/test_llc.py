#!/usr/bin/env python3
"""Pruebas unitarias sin pantalla: segmentos a partir de la selección, proyecto LLC y ajuste de intervalo."""
import sys, pathlib, json, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import thumbsheet as T

fails = 0
def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond: fails += 1

ts = list(range(0, 100, 5))   # 0..95, S=5, duración 97.3
segs = T.selection_segments({10, 15, 20, 40, 95}, ts, 5, 97.3)
check(segs == [(10.0, 25.0), (40.0, 45.0), (95.0, 97.3)], "rachas contiguas → segmentos, fin = último + S acotado a la duración: %s" % segs)
check(T.selection_segments(set(), ts, 5, 97.3) == [], "sin selección → sin segmentos")
check(T.selection_segments({7}, ts, 5, 97.3) == [], "un timestamp que no es tesela se ignora")
check(T.selection_segments({0, 5, 10}, [0, 5, 10], 5, 15) == [(0.0, 15.0)], "todo seleccionado → un segmento entero")
check(T.selection_segments({0, 10}, [0, 10, 20], 10, 30) == [(0.0, 20.0)], "contigüidad medida con el intervalo real (S=10)")

with tempfile.TemporaryDirectory() as d:
    v = pathlib.Path(d) / "mi vídeo.raro.mp4"; v.write_bytes(b"x")
    proj = T.write_llc_project(v, [(10, 25), (40, 45.5)])
    check(proj.name == "mi vídeo.raro-proj.llc", "nombre <stem>-proj.llc junto al vídeo: %s" % proj.name)
    data = json.loads(proj.read_text(encoding="utf-8"))
    check(data["version"] == 2 and data["mediaFileName"] == v.name, "cabecera v2 con mediaFileName")
    check(data["cutSegments"] == [{"start": 10, "end": 25, "name": ""}, {"start": 40, "end": 45.5, "name": ""}], "segmentos: %s" % data["cutSegments"])
    txt = proj.read_text()
    check(txt.startswith("{") and txt.rstrip().endswith("}"), "JSON puro (válido también como JSON5 y YAML)")
    try:
        import yaml  # opcional
        check(yaml.safe_load(txt) == data, "lo lee también un parser YAML (versiones antiguas de LosslessCut)")
    except ImportError:
        print("skip PyYAML no instalado")

for v, exp in ((5, 5), (7, 5), (8, 10), (12.6, 15), (300, 300), (301, 300), (0, 5)):
    check(T.snap_interval(v) == exp, "snap_interval(%s) = %s" % (v, exp))

print("RESULTADO:", "OK" if fails == 0 else "FALLO (%d)" % fails)
sys.exit(1 if fails else 0)
