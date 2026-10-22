#!/usr/bin/env python3
"""Sin ventana. El corte respeta el orden del panel de segmentos, también sin pérdida: construye un vídeo de
6 s con un color distinto cada segundo (keyframe cada segundo), expande a keyframes tramos reordenados y
comprueba que salen en el orden pedido (fundiendo sólo los consecutivos que se solapan); después corta de
verdad en los dos modos (copia de streams y exacto) y mira de qué color es cada tramo del resultado."""
import sys, pathlib, tempfile, subprocess
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import thumbsheet as T

fails = 0
def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond: fails += 1

COLORS = ["red", "lime", "blue", "yellow", "magenta", "cyan"]   # segundo 0, 1, 2, 3, 4, 5
NAMES = {"red": (255, 0, 0), "lime": (0, 255, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0),
         "magenta": (255, 0, 255), "cyan": (0, 255, 255)}

def make_video(path):
    fc = "".join("color=c=%s:s=64x64:r=25:d=1[c%d];" % (c, i) for i, c in enumerate(COLORS))
    fc += "".join("[c%d]" % i for i in range(len(COLORS))) + "concat=n=%d:v=1:a=0[v]" % len(COLORS)
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-filter_complex", fc, "-map", "[v]",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", "-g", "25", "-keyint_min", "25",
           "-sc_threshold", "0", "-force_key_frames", "expr:gte(t,n_forced*1)", "-y", str(path)]
    subprocess.run(cmd, check=True, env=T.FF_ENV)

def color_at(path, t):
    """Color dominante del fotograma en t (nombre de COLORS)."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", "%.3f" % t, "-i", str(path),
           "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    raw = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=T.FF_ENV).stdout
    if len(raw) < 3:
        return "?"
    px = raw[len(raw) // 2 - (len(raw) // 2) % 3:][:3]   # un píxel del centro
    best = min(NAMES, key=lambda n: sum((a - b) ** 2 for a, b in zip(NAMES[n], px)))
    return best

def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         stdout=subprocess.PIPE, env=T.FF_ENV).stdout
    return float(out.decode().strip() or 0)

def run_cut(cmd):
    p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=T.FF_ENV)
    return p.returncode, p.stderr.decode("utf-8", "replace").strip()

with tempfile.TemporaryDirectory() as d:
    d = pathlib.Path(d)
    v = d / "colores.mp4"
    make_video(v)
    kfs = T.keyframes_near(v, [0, 1, 2, 3, 4, 5], None)
    check(all(any(abs(k - i) < 0.05 for k in kfs) for i in range(6)), "vídeo de prueba con keyframe en cada segundo: %s" % [round(k, 2) for k in kfs])
    check([color_at(v, 0.5), color_at(v, 3.5)] == ["red", "yellow"], "colores del vídeo de prueba: 0.5 s rojo, 3.5 s amarillo")

    # 1. expand_to_keyframes mantiene el orden de corte
    e = T.expand_to_keyframes([(3.2, 3.8), (1.2, 1.8)], v, 6.0, 1)
    check(e == [(3.0, 4.0), (1.0, 2.0)], "reordenados (3–4 antes que 1–2) siguen en ese orden al expandir: %s" % e)
    e = T.expand_to_keyframes([(1.2, 1.5), (1.6, 1.9)], v, 6.0, 1)
    check(e == [(1.0, 2.0)], "dos tramos consecutivos del mismo segundo se funden en uno: %s" % e)
    e = T.expand_to_keyframes([(1.2, 1.5), (2.2, 2.5)], v, 6.0, 1)
    check(e == [(1.0, 3.0)], "consecutivos y contiguos en el tiempo se funden: %s" % e)
    e = T.expand_to_keyframes([(2.2, 2.5), (1.2, 1.5)], v, 6.0, 1)
    check(e == [(2.0, 3.0), (1.0, 2.0)], "contiguos pero en orden inverso NO se funden: %s" % e)
    e = T.expand_to_keyframes([(4.2, 4.8), (0.2, 0.8), (2.2, 2.8)], v, 6.0, 1)
    check(e == [(4.0, 5.0), (0.0, 1.0), (2.0, 3.0)], "tres tramos en orden arbitrario: %s" % e)

    # 2. corte real sin pérdida: magenta (4–5), rojo (0–1), azul (2–3)
    segs = [(4.0, 5.0), (0.0, 1.0), (2.0, 3.0)]
    lst = d / "lista.txt"; out = d / "copia.mkv"; tmp = d / "copia.mkv.part"
    T.write_concat_list(lst, v, segs)
    rc, err = run_cut(T.cut_command_copy(lst, out, tmp))
    if rc == 0:
        tmp.rename(out)
        dur = duration(out)
        got = [color_at(out, 0.5), color_at(out, 1.5), color_at(out, 2.5)]
        check(abs(dur - 3.0) < 0.3, "copia de streams: dura %.2f s" % dur)
        check(got == ["magenta", "red", "blue"], "copia de streams: tramos en el orden del panel (magenta, rojo, azul): %s" % got)
    else:
        check(False, "copia de streams: ffmpeg falló (%d): %s" % (rc, err[:200]))

    # 3. corte exacto: mismo orden
    out2 = d / "exacto.mkv"; tmp2 = d / "exacto.mkv.part"
    rc, err = run_cut(T.cut_command_exact(v, [(4.2, 4.8), (0.2, 0.8), (2.2, 2.8)], out2, tmp2, False))
    if rc == 0:
        tmp2.rename(out2)
        dur = duration(out2)
        got = [color_at(out2, 0.3), color_at(out2, 0.9), color_at(out2, 1.5)]
        check(abs(dur - 1.8) < 0.2, "exacto: dura %.2f s" % dur)
        check(got == ["magenta", "red", "blue"], "exacto: tramos en el orden del panel (magenta, rojo, azul): %s" % got)
    else:
        check(False, "exacto: ffmpeg falló (%d): %s" % (rc, err[:200]))

print("RESULTADO:", "OK" if fails == 0 else "FALLO (%d)" % fails)
sys.exit(1 if fails else 0)
