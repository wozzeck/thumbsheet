#!/usr/bin/env python3
"""Pruebas unitarias sin pantalla: selección como segmentos (reajuste al cambiar el intervalo), proyecto LLC y ajuste de intervalo."""
import sys, pathlib, json, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import thumbsheet as T

fails = 0
def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond: fails += 1

S = T.Selection
# ejemplo del user: 15–25 s seleccionado; al pasar a teselas de 10 s se marcan 10 y 20 → segmento 10–30;
# al volver a 5 s las teselas intermedias aparecen marcadas y el segmento NO cambia
sel = S([(15, 25)])
g5 = list(range(0, 100, 5)); g10 = list(range(0, 100, 10)); dur = 100
check(sel.tiles(g5, 5, dur) == {15, 20}, "15–25 con rejilla de 5 s marca 15 y 20")
check(sel.tiles(g10, 10, dur) == {10, 20}, "15–25 con rejilla de 10 s marca 10 y 20")
sel.resnap(g10, 10, dur)
check(sel.segments == [(10.0, 30.0)], "al bajar a 10 s el segmento se amplía a 10–30: %s" % sel.segments)
sel.resnap(g5, 5, dur)
check(sel.segments == [(10.0, 30.0)], "al subir a 5 s el segmento sigue siendo 10–30: %s" % sel.segments)
check(sel.tiles(g5, 5, dur) == {10, 15, 20, 25}, "y marca las teselas 10, 15, 20, 25 (las intermedias aparecen marcadas)")
# alternar teselas = sumar/restar su tramo
sel = S()
sel.add(*S.tile_range(40, 5, dur)); sel.add(*S.tile_range(45, 5, dur)); sel.add(*S.tile_range(50, 5, dur))
check(sel.segments == [(40.0, 55.0)], "tres teselas contiguas → un segmento 40–55: %s" % sel.segments)
sel.remove(*S.tile_range(45, 5, dur))
check(sel.segments == [(40.0, 45.0), (50.0, 55.0)], "quitar la del medio parte el segmento: %s" % sel.segments)
sel.add(*S.tile_range(95, 5, 97.3))
check(sel.segments[-1] == (95.0, 97.3), "la última tesela se acota a la duración: %s" % (sel.segments[-1],))
check(S([(10, 20), (20, 30), (50, 60), (55, 58)]).segments == [(10.0, 30.0), (50.0, 60.0)], "normalización: funde adyacentes y solapados")
check(S.from_json({"segments": [[1, 2.5]]}).segments == [(1.0, 2.5)] and S.from_json([40, 50]).segments == [(40.0, 45.0), (50.0, 55.0)], "carga JSON nuevo y formato antiguo (teselas → tramos de 5 s)")
check(S([(10, 20)]).to_json() == {"segments": [[10, 20]]}, "to_json con enteros limpios")
check(not S() and bool(S([(0, 1)])), "bool(): vacío / no vacío")

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
