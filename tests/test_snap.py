#!/usr/bin/env python3
"""Sin pantalla, instantáneo: paradas del «Snap scroll» (funciones snap_* del mosaico). Rejilla regular, filas de
altura propia (vídeos que cambian de orientación) y filas más altas que el visor."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import thumbsheet as T

fails = 0
def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond: fails += 1

# rejilla regular como la del harness: 24 filas de 76 px + hueco 6 (paso 82), PAD 8, visor de 841 px
n, pitch, page = 24, 82, 841
natural = 2 * 8 + n * 76 + (n - 1) * 6
check(natural == 1978, "contenido de 24 filas: %d px" % natural)
last = T.snap_last(n, pitch, natural, page)
check(last == 14 and last * pitch >= natural - page and (last - 1) * pitch < natural - page,
      "última parada necesaria: fila %d (%d px), la primera que deja ver el final (%d)" % (last, last * pitch, natural - page))
H = T.snap_height(n, pitch, natural, page)
check(H == 14 * 82 + 841 and H - natural < pitch, "alto con relleno: %d (+%d px, menos de una fila)" % (H, H - natural))
check(T.snap_target(267.3, n, pitch, natural, page) == 246.0, "267,3 px → parada más cercana 246 (fila 3)")
check(T.snap_target(287.0, n, pitch, natural, page) == 328.0, "287 px → 328 (más cerca de la fila 4)")
check(T.snap_target(-12, n, pitch, natural, page) == 0.0, "por encima del principio → 0")
check(T.snap_target(1500, n, pitch, natural, page) == 1148.0, "más allá del tope → la última parada necesaria (1148)")
check(T.snap_step(246, 1, n, pitch, natural, page) == 328.0 and T.snap_step(246, -2, n, pitch, natural, page) == 82.0,
      "rueda: una fila abajo 246 → 328; dos arriba 246 → 82")
check(T.snap_step(250, 1, n, pitch, natural, page) == 328.0, "desde un valor suelto, primero a la parada más cercana y de ahí una fila")
check(T.snap_step(1148, 3, n, pitch, natural, page) == 1148.0 and T.snap_step(82, -5, n, pitch, natural, page) == 0.0,
      "la rueda no pasa del tope ni del principio")
short = 2 * 8 + 5 * 76 + 4 * 6
check(T.snap_height(5, pitch, short, page) == short and T.snap_target(0, 5, pitch, short, page) == 0.0,
      "contenido que cabe en el visor: sin relleno, parada única 0")
check(T.snap_height(n, pitch, natural, 0) == natural, "sin visor todavía (alto 0): sin relleno")

# filas de altura propia: una de verticales (236 px) en medio
tops = [0, 82, 164, 406, 488, 570]          # las paradas son los «row_y» redondeados
nat2 = 8 + 570 + 76 + 8
check(T.snap_index(300, 6, pitch, tops) == 3 and T.snap_index(280, 6, pitch, tops) == 2,
      "filas variables: 300 px → fila 3 (406), 280 px → fila 2 (164)")
check(T.snap_target(300, 6, pitch, nat2, 400, tops) == 406.0, "la parada es el borde de la fila, no un múltiplo de 82")
check(T.snap_last(6, pitch, nat2, 400, tops) == 3 and T.snap_height(6, pitch, nat2, 400, tops) == 406 + 400,
      "filas variables: última parada 406 (con el visor de 400 px ya se ve el final, %d) y alto %d" % (
          nat2, T.snap_height(6, pitch, nat2, 400, tops)))
check(T.snap_step(164, 1, 6, pitch, nat2, 400, tops) == 406.0 and T.snap_step(406, -1, 6, pitch, nat2, 400, tops) == 164.0,
      "rueda con filas variables: 164 → 406 → 164")

# filas más altas que el visor (ventana diminuta): el final también es parada, para poder verlo
tops3 = [0, 500, 1000]
nat3 = 8 + 1000 + 494 + 8
check(T.snap_target(1210, 3, 500, nat3, 300, tops3) == float(nat3 - 300), "fila más alta que el visor: el final (%d) es parada" % (nat3 - 300))
check(T.snap_step(1000, 1, 3, 500, nat3, 300, tops3) == float(nat3 - 300), "y la rueda llega hasta él")
check(T.snap_height(3, 500, nat3, 300, tops3) == nat3, "sin relleno: el contenido ya llega")

print("RESULTADO:", "OK" if fails == 0 else "FALLO (%d)" % fails)
sys.exit(1 if fails else 0)
