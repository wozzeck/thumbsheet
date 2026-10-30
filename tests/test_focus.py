#!/usr/bin/env python3
"""Sin pantalla, instantáneo: `focus_rows`, las filas que mandan en la prioridad de generación. De las que tocan el
visor, la de arriba y la de abajo sólo cuentan si se ve al menos la mitad de su alto (la que asoma un trozo no manda:
se empieza por la primera que se ve entera); si ninguna llega (visor más bajo que una fila), todas las que tocan.
Rejilla regular y filas de altura propia. Uso: tests/test_focus.py"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import thumbsheet as T

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


# rejilla regular como la de la app: PAD 8, teselas de 90 de alto, hueco 6 (paso 96): la fila r ocupa [8+96r, 98+96r]
PAD, H, GAP = 8, 90, 6
span = lambda r: (PAD + r * (H + GAP), H)   # noqa: E731
row_of = lambda y: max(0, int((y - PAD) // (H + GAP)))   # noqa: E731


def focus(vy1, vy2):
    return T.focus_rows(row_of(vy1), row_of(vy2), span, vy1, vy2)


check(focus(0, 500) == (0, 4), "visor [0,500]: toca las filas 0-5 y de la 5 asoman 12 px (13 %%): el foco es 0-4 (%s)" % (focus(0, 500),))
check(focus(60, 560) == (1, 5), "visor [60,560]: de la fila 0 se ven 38 px (42 %%) → fuera; de la 5, 72 px (80 %%) → dentro: 1-5 (%s)" % (focus(60, 560),))
check(focus(50, 550) == (0, 5), "visor [50,550]: fila 0 al 53 %% y fila 5 al 69 %%: las dos mandan, 0-5 (%s)" % (focus(50, 550),))
check(focus(53, 550) == (0, 5), "justo la mitad (45 de 90 px) cuenta: 0-5 (%s)" % (focus(53, 550),))
check(focus(54, 550) == (1, 5), "un píxel menos de la mitad ya no: 1-5 (%s)" % (focus(54, 550),))
check(focus(60, 90) == (0, 0), "una sola fila a la vista y a menos de la mitad: no hay otra, se queda ella (%s)" % (focus(60, 90),))
check(focus(96, 200) == (1, 1), "el borde superior cae en el hueco entre filas (96): la fila 0 no se ve nada y queda fuera (%s)" % (focus(96, 200),))
check(focus(0, 482) == (0, 4), "el borde inferior justo al pie de la fila 4 (482): entera, dentro (%s)" % (focus(0, 482),))

# teselas más altas que el visor: dos filas a la vista, ninguna a la mitad → todas las que tocan (como hasta ahora)
H2 = 400
span2 = lambda r: (PAD + r * (H2 + GAP), H2)   # noqa: E731
row_of2 = lambda y: max(0, int((y - PAD) // (H2 + GAP)))   # noqa: E731
f = T.focus_rows(row_of2(300), row_of2(500), span2, 300, 500)
check(f == (0, 1), "filas de 400 px en un visor de 200: fila 0 al 27 %% y fila 1 al 21 %%, ninguna llega → las dos (%s)" % (f,))
f = T.focus_rows(row_of2(300), row_of2(700), span2, 300, 700)
check(f == (1, 1), "visor [300,700]: fila 0 al 27 %%, fila 1 (414-814) al 71 %% → sólo la 1 (%s)" % (f,))

# filas de altura propia (vídeos que cambian de orientación): row_y/row_h como los calcula el mosaico, PAD aparte
row_y, row_h = [0, 100, 350], [94, 244, 60]   # filas en [8,102], [108,352], [358,418]
span3 = lambda r: (PAD + row_y[min(r, 2)], row_h[min(r, 2)])   # noqa: E731
f = T.focus_rows(0, 2, span3, 60, 390)
check(f == (1, 2), "altura propia, visor [60,390]: fila 0 al 45 %% fuera, fila 2 (60 px) al 53 %% dentro → 1-2 (%s)" % (f,))
f = T.focus_rows(0, 2, span3, 60, 380)
check(f == (1, 1), "altura propia, visor [60,380]: la fila 2 sólo al 37 %% → 1-1 (%s)" % (f,))
f = T.focus_rows(0, 5, span3, 0, 1000)
check(f == (0, 5), "una fila pedida más allá de las que hay usa la última (entera a la vista): 0-5 (%s)" % (f,))

print("TODO OK" if not fails else "HAY FALLOS: %d" % fails)
sys.exit(1 if fails else 0)
