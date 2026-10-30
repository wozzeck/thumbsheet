#!/usr/bin/env python3
"""Sin pantalla, instantáneo: lo que hay detrás de las marcas rojas de la barra de scroll y del recuadro rojo de la
vista ampliada. `mark_bands` (filas con teselas marcadas → bandas del contenido, consecutivas fundidas), `fit_rect`
(encaje centrado sin deformar, el mismo para el fotograma y el recuadro) y `Selection.contains` (¿cae el instante
en algún segmento [a, b)?). Uso: tests/test_marks.py"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import thumbsheet as T

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


# --- mark_bands: rejilla regular PAD 8, filas de 90 + hueco 6
span = lambda r: (8 + 96 * r, 90)   # noqa: E731
check(T.mark_bands([], span) == [], "sin filas marcadas: sin bandas")
check(T.mark_bands([3], span) == [(296, 386)], "una fila: su banda [y, y+alto) (%s)" % T.mark_bands([3], span))
check(T.mark_bands([5, 3, 4], span) == [(296, 578)], "filas consecutivas (desordenadas, 3-5): una sola banda hasta el pie de la 5 (%s)"
      % T.mark_bands([5, 3, 4], span))
check(T.mark_bands([0, 1, 7, 9, 9], span) == [(8, 194), (680, 770), (872, 962)],
      "0-1, 7 y 9 (repetida): tres bandas (%s)" % T.mark_bands([0, 1, 7, 9, 9], span))
# filas de altura propia
row_y, row_h = [0, 100, 350], [94, 244, 60]
span2 = lambda r: (8 + row_y[r], row_h[r])   # noqa: E731
check(T.mark_bands([1, 2], span2) == [(108, 418)], "altura propia: filas 1-2 → [108, 418) (%s)" % T.mark_bands([1, 2], span2))

# --- fit_rect
check(T.fit_rect(1920, 1080, 1400, 800) == (0.0, (800 - 787.5) / 2.0, 1400.0, 787.5),
      "16:9 en 1400×800: ancho completo, centrado en vertical (%s)" % (T.fit_rect(1920, 1080, 1400, 800),))
x, y, w, h = T.fit_rect(1080, 1920, 1400, 800)
check(abs(h - 800) < 1e-9 and abs(w - 450) < 1e-9 and abs(x - 475) < 1e-9 and y == 0.0,
      "vertical 9:16 en 1400×800: alto completo, centrado en horizontal (%s)" % ((x, y, w, h),))
check(T.fit_rect(16.0 / 9.0, 1.0, 1600, 900) == (0.0, 0.0, 1600.0, 900.0), "con el aspecto como ancho/alto (vídeo): encaja igual")

# --- Selection.contains
sel = T.Selection([(10, 20), (30, 35), (50, 60)])
check(sel.contains(10) and sel.contains(19.99) and sel.contains(34) and sel.contains(50),
      "dentro: el inicio incluido y hasta justo antes del fin")
check(not sel.contains(20) and not sel.contains(9.9) and not sel.contains(25) and not sel.contains(60) and not sel.contains(1000),
      "fuera: el fin excluido, los huecos y más allá")
check(not T.Selection().contains(5), "sin segmentos: nada dentro")
check(T.Selection([(0, 5), (3, 8)]).contains(6), "segmentos que se funden al normalizar: 6 cae en [0, 8)")

print("TODO OK" if not fails else "HAY FALLOS: %d" % fails)
sys.exit(1 if fails else 0)
