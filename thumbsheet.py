#!/usr/bin/env python3
"""thumbsheet — sábana de miniaturas de un vídeo.

Abre un vídeo y muestra un mosaico con una captura cada N segundos. Dos controles:
el intervalo entre capturas (5–300 s) y el tamaño de las teselas.

Pensado para máquinas modestas:
  * las capturas las saca ffmpeg en procesos con `nice`/`ionice` (nunca compiten con el escritorio);
  * modo "seek" (una búsqueda exacta por captura, en paralelo) cuando el intervalo es grande
    respecto al GOP del vídeo, y modo "tramos" (decodificación continua por trozos) cuando es denso;
  * en modo tramos se suman workers VAAPI (GPU Intel/AMD) si existen y pasan un autotest;
  * el mosaico es un DrawingArea virtual: sólo se decodifican las miniaturas visibles, en un hilo
    aparte, con una caché LRU acotada en bytes;
  * las miniaturas se guardan en ~/.cache/thumbsheet/<huella del vídeo>/ y se reutilizan entre
    intervalos y entre sesiones.
"""
import collections
import ctypes
import errno
import hashlib
import bisect
import json
import math
import re
import os
import pathlib
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import weakref

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
import cairo  # noqa: E402
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402

try:   # reproducción en la vista ampliada (opcional): gir1.2-gstreamer-1.0 + gstreamer1.0-gtk3 + gstreamer1.0-libav
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # noqa: E402
    HAVE_GST = True
except (ValueError, ImportError):
    Gst = None
    HAVE_GST = False
_GST_READY = False

APP = "thumbsheet"
APP_DIR = pathlib.Path(__file__).resolve().parent
HOME = pathlib.Path.home()
CACHE_ROOT = pathlib.Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / APP
CONFIG_FILE = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / APP / "settings.json"

THUMB_MAX = int(os.environ.get("THUMBSHEET_THUMB_PX", "480"))     # lado mayor de la miniatura guardada
INTERVALS = [1, 2, 5, 10, 20, 30, 60, 300, 600]     # valores del slider de intervalo (s)
INTERVAL_DEF = 30
RESP_CUT_DELETE = 1                      # respuesta del diálogo de corte: cortar y borrar el original
CUT_DEL_LABEL = "Cortar y _borrar original"
CUT_DEL_ARM_MS = 5000                    # tras la primera pulsación, el botón se desarma si no se confirma a tiempo
COLS_MIN, COLS_MAX, COLS_DEF = 3, 20, 6                     # teselas por fila
PIX_BUDGET = int(os.environ.get("THUMBSHEET_PIX_MB", "64")) * 1024 * 1024
DEBUG = os.environ.get("THUMBSHEET_DEBUG") == "1"

VIDEO_EXT = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".mpg", ".mpeg", ".ts", ".m2ts",
             ".wmv", ".flv", ".3gp", ".ogv", ".vob")


def natural_key(text):
    """«vídeo2» antes que «vídeo10»: los números se comparan como números."""
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", text)]


def is_video_name(name):
    return not name.startswith(".") and os.path.splitext(name)[1].lower() in VIDEO_EXT


def walk_videos(dirs):
    """Ficheros soportados (por extensión, sin distinguir mayúsculas) dentro de esas carpetas y todas sus
    subcarpetas, en orden natural por ruta, y las carpetas recorridas (cada raíz y todas sus subcarpetas, también
    las vacías: son las que vigila la vigilancia). Se saltan los nombres que empiezan por punto y no se siguen
    enlaces a carpetas (bucles). Puede tardar en carpetas remotas: llamar desde un hilo."""
    found, seen = [], []
    for d in dirs:
        for root, subdirs, files in os.walk(str(d)):
            subdirs[:] = [x for x in subdirs if not x.startswith(".")]
            seen.append(pathlib.Path(root))
            for f in files:
                if is_video_name(f):
                    found.append(pathlib.Path(root) / f)
    found.sort(key=lambda q: [natural_key(part) for part in q.parts])
    return found, seen


def find_videos(dirs):
    """Sólo los vídeos de walk_videos."""
    return walk_videos(dirs)[0]


def is_cut_name(path):
    """Vídeo ya procesado: el resultado de Cortar o de Full (<nombre>-cortado, -cortado-2…)."""
    return re.search(r"-cortado(-\d+)?$", pathlib.Path(path).stem) is not None


def pick_next(items, pos, usable):
    """A qué vídeo pasar al borrar, procesar u ocultar uno: el primero de items[pos:] que cumpla `usable` o, si no queda
    ninguno después, el último de antes que la cumpla; None si no hay. Las filas de carpeta no están en `items` y los
    ocultos no cumplen `usable`: se saltan."""
    for d in items[pos:]:
        if usable(d):
            return d
    for d in reversed(items[:pos]):
        if usable(d):
            return d
    return None


_PROF = {}
_CNT = {}


def _prof(name, dt):
    _PROF[name] = _PROF.get(name, 0.0) + dt


def _count(name, n=1):
    _CNT[name] = _CNT.get(name, 0) + n


def _prof_report():
    """DEBUG: las funciones que más tiempo han ocupado desde el último informe, y contadores."""
    if not _PROF and not _CNT:
        return ""
    items = sorted(_PROF.items(), key=lambda kv: -kv[1])[:6]
    rep = ", ".join("%s %d ms" % (k, 1000 * v) for k, v in items)
    if _CNT:
        rep += " · " + ", ".join("%s=%d" % kv for kv in sorted(_CNT.items()))
    _PROF.clear()
    _CNT.clear()
    return rep


def log(*a):
    if DEBUG:
        print("[%s] %s" % (time.strftime("%H:%M:%S"), " ".join(str(x) for x in a)), file=sys.stderr, flush=True)


def physical_cores():
    """Núcleos físicos (sin SMT). Medido: más workers que núcleos reales no acorta nada y multiplica la CPU
    gastada y la RAM (~70 MB por ffmpeg)."""
    try:
        seen = set()
        for topo in pathlib.Path("/sys/devices/system/cpu").glob("cpu[0-9]*/topology"):
            seen.add(((topo / "physical_package_id").read_text().strip(), (topo / "core_id").read_text().strip()))
        if seen:
            return len(seen)
    except OSError:
        pass
    return os.cpu_count() or 2


def mem_available_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return 4096


def default_workers():
    env = os.environ.get("THUMBSHEET_WORKERS")
    if env and env.isdigit() and int(env) > 0:
        return int(env)
    n = physical_cores()
    n = n - 1 if n >= 4 else n            # un núcleo libre para el escritorio y la propia interfaz
    n = min(n, max(1, mem_available_mb() // 150))   # ~70 MB por ffmpeg, con margen
    return max(1, min(16, n))


def snap_interval(v):
    """Intervalo válido: el valor de INTERVALS más cercano. La caché va por segundo, así que las capturas
    coincidentes entre intervalos (p. ej. 10 s y 30 s) se reutilizan."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return INTERVAL_DEF
    return min(INTERVALS, key=lambda x: abs(x - v))


def fmt_interval(s):
    return "%d s" % s if s < 60 else "%d min" % (s // 60)


def interval_mark(s):
    return "%ds" % s if s < 60 else "%dm" % (s // 60)


def fmt_time(t):
    t = int(round(t))
    h, m, s = t // 3600, (t // 60) % 60, t % 60
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def eta_text(started, done, remaining):
    """' · faltan ~m:ss' estimado con el ritmo medio desde `started` (time.time()); vacío hasta tener base."""
    if not started or done < 2 or remaining <= 0:
        return ""
    elapsed = time.time() - started
    if elapsed < 1.5:
        return ""
    eta = elapsed * remaining / float(done)
    if eta >= 90:
        eta = math.ceil(eta / 10.0) * 10   # con minutos por delante, en pasos de 10 s para que no baile
    return " · faltan ~%s" % fmt_time(eta)


def fit_vf(W, H):
    """Filtro de escala que encaja el fotograma en la caja W×H respetando SU relación de aspecto (`dar`, que
    incluye el SAR de los anamórficos): la miniatura sale con su proporción real, sin deformar ni rellenar.
    trunc(…/2)*2 redondea hacia abajo a par; el 0.999 absorbe el redondeo de la caja nominal."""
    r = repr(W / float(H) * 0.999)
    return "scale=w='if(gte(dar,%s),%d,trunc(%d*dar/2)*2)':h='if(gte(dar,%s),trunc(%d/dar/2)*2,%d)'" % (r, W, H, r, W, H)


def fit_box(aspect, W, H):
    """Tamaño (par) de una miniatura de aspecto `aspect` encajada en W×H; mismo redondeo que fit_vf."""
    if aspect >= W / float(H) * 0.999:
        return W, max(2, int(W / aspect / 2) * 2)
    return max(2, int(H * aspect / 2) * 2), H


def _nice_prefix():
    pre = ["nice", "-n", "10"] if shutil.which("nice") else []
    if shutil.which("ionice"):
        pre += ["ionice", "-c", "2", "-n", "7"]
    return pre


NICE = _nice_prefix()
# El ffmpeg de Ubuntu arrastra (por alguna librería enlazada) un pool OpenMP de un hilo por núcleo que
# gira en sched_yield mientras el proceso vive, aunque se pida -threads 1. Medido: una captura de 0,1 s
# de trabajo real costaba 1,5 s de CPU. Un solo hilo OMP y a dormir.
FF_ENV = dict(os.environ, OMP_NUM_THREADS="1", OMP_WAIT_POLICY="passive")


_APP_PID = os.getpid()


def _pdeathsig_preexec():
    """Se ejecuta en el hijo justo antes del exec: pide al kernel que lo mate (SIGKILL) si el padre
    (esta app) muere, sea como sea. El exec de nice/ionice/ffmpeg lo conserva. Si el padre ya murió
    entre el fork y este prctl (carrera clásica de PDEATHSIG), el hijo se va directamente.
    SIGKILL y no SIGTERM: ffmpeg a veces se queda colgado en un futex al intentar salir limpio tras
    SIGTERM (visto con la señal llegando durante el arranque), y todo lo que escribe son temporales
    que sólo se renombran al terminar bien, así que matarlo en seco no deja nada a medias."""
    try:
        _LIBC.prctl(1, signal.SIGKILL, 0, 0, 0)   # PR_SET_PDEATHSIG = 1
        if os.getppid() != _APP_PID:
            os._exit(0)
    except Exception:  # noqa: BLE001
        pass


try:
    _LIBC = ctypes.CDLL("libc.so.6", use_errno=True)
    PREEXEC = _pdeathsig_preexec
except OSError:
    _LIBC = None
    PREEXEC = None
def _pdeathsig_prefix():
    """Prefijo que fija PR_SET_PDEATHSIG en el hijo (si la app muere, el ffmpeg también) sin recurrir a preexec_fn:
    con preexec_fn Python no puede usar vfork y cada lanzamiento es un fork completo, 5 ms en un proceso de 180 MB
    y bloqueando la memoria de todos los hilos mientras dura; con 13 workers arrancando a la vez, o uno por
    captura en modo seek, eso eran los parones de la interfaz. Con vfork cuesta 0,4 ms y no bloquea nada."""
    exe = shutil.which("setpriv")
    if exe:
        try:
            out = subprocess.run([exe, "--help"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=5).stdout
            if b"pdeathsig" in out:
                return [exe, "--pdeathsig=KILL", "--"]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


PDEATH_PREFIX = _pdeathsig_prefix()
if PDEATH_PREFIX:
    PREEXEC = None   # con setpriv no hace falta preexec_fn: Python usa vfork
FFMPEG_BASE = NICE + (PDEATH_PREFIX or []) + ["ffmpeg", "-nostdin", "-hide_banner"]
FFMPEG = FFMPEG_BASE + ["-loglevel", "error"]
FFMPEG_INFO = FFMPEG_BASE + ["-loglevel", "info", "-nostats"]   # para leer lo que imprime showinfo
def _showinfo_filter():
    """showinfo sin sumas de verificación (coste por fotograma) si este ffmpeg admite la opción (≥ 4.3)."""
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-h", "filter=showinfo"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=10)
        if b"checksum" in r.stdout:
            return "showinfo=checksum=0"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "showinfo"


SHOWINFO = _showinfo_filter()
_RE_SHOW_N = re.compile(r"\bn:\s*(\d+)\b")
_RE_SHOW_S = re.compile(r"\bs:(\d+)x(\d+)\b")
_RE_SHOW_SAR = re.compile(r"\bsar:(\d+)/(\d+)\b")
# decodificador software: 1 hilo por proceso (el paralelismo va entre capturas), sin B-frames ni
# filtro de desbloqueo (invisible a tamaño de miniatura, ahorra un 20-30 % de CPU)
SW_DEC = ["-threads", "1", "-skip_frame", "noref", "-skip_loop_filter", "all", "-flags2", "+fast"]
FULL_DEC = ["-threads", "1", "-skip_frame", "noref"]   # fotograma a resolución nativa (vista ampliada)


# ----------------------------------------------------------------------------------------------
# sondeo del vídeo
# ----------------------------------------------------------------------------------------------
class VideoInfo(object):
    def __init__(self, path):
        self.path = pathlib.Path(path).resolve()
        st = self.path.stat()
        self.size, self.mtime_ns = st.st_size, st.st_mtime_ns
        self.duration = 0.0
        self.width = self.height = 0          # dimensiones tal como se ven (ya rotadas)
        self.aspect = 16.0 / 9.0
        self.fps = 25.0
        self.rotated = False
        self.codec = "?"
        self.has_audio = False
        self.gop = None                       # segundos entre keyframes (None = desconocido/enorme)
        self._probe()
        # tamaño de la miniatura guardada (par, lado mayor = THUMB_MAX, sin ampliar vídeos pequeños)
        scale = min(1.0, THUMB_MAX / float(max(self.width, self.height) or THUMB_MAX))
        self.thumb_w = max(2, int(round(self.width * scale / 2.0)) * 2)
        self.thumb_h = max(2, int(round(self.height * scale / 2.0)) * 2)

    @property
    def key(self):
        h = hashlib.sha1()
        h.update(("%s|%d|%d|%d" % (self.path, self.size, self.mtime_ns, THUMB_MAX)).encode("utf-8"))
        return h.hexdigest()[:16]

    def _probe(self):
        cmd = ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(self.path)]
        try:
            # en una ubicación remota lenta el sondeo puede tardar bastante; el tope evita que una conexión muerta
            # deje la cola de sondeo (y con ella el segundo plano) parada para siempre
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=FF_ENV, timeout=600)
        except subprocess.TimeoutExpired:
            raise RuntimeError("ffprobe no responde (¿ubicación remota caída?)")
        if r.returncode != 0:
            raise RuntimeError(short_error(r.stderr.decode("utf-8", "replace"), "ffprobe falló"))
        data = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
        streams = data.get("streams") or []
        videos = [x for x in streams if x.get("codec_type") == "video"]
        # la primera pista de vídeo que no sea una carátula incrustada
        real = [x for x in videos if not (x.get("disposition") or {}).get("attached_pic")]
        if not videos:
            raise RuntimeError("el fichero no tiene pista de vídeo")
        s = (real or videos)[0]
        self.has_audio = any(x.get("codec_type") == "audio" for x in streams)
        fmt = data.get("format") or {}
        self.codec = s.get("codec_name", "?")
        try:
            self.duration = float(fmt.get("duration") or s.get("duration") or 0)
        except ValueError:
            self.duration = 0.0
        if self.duration <= 0:
            raise RuntimeError("no se pudo determinar la duración del vídeo")
        w, h = int(s.get("width") or 0), int(s.get("height") or 0)
        if not w or not h:
            raise RuntimeError("no se pudo determinar la resolución del vídeo")
        # relación de aspecto de muestra (anamórficos)
        sar = 1.0
        sar_s = s.get("sample_aspect_ratio") or "1:1"
        try:
            a, b = sar_s.split(":")
            if float(a) > 0 and float(b) > 0:
                sar = float(a) / float(b)
        except ValueError:
            pass
        # rotación (ffmpeg autorrota al decodificar, así que las miniaturas salen ya derechas)
        rot = 0
        try:
            rot = int(float((s.get("tags") or {}).get("rotate", 0)))
        except (TypeError, ValueError):
            pass
        for sd in s.get("side_data_list") or []:
            if "rotation" in sd:
                try:
                    rot = int(float(sd["rotation"]))
                except (TypeError, ValueError):
                    pass
        self.rotated = (abs(rot) % 180) == 90
        if self.rotated:
            w, h = h, w
        self.width, self.height = w, h
        self.aspect = (w * sar) / float(h)
        for key in ("avg_frame_rate", "r_frame_rate"):
            fr = s.get(key) or ""
            try:
                a, b = fr.split("/")
                if float(b) > 0 and float(a) > 0:
                    self.fps = float(a) / float(b)
                    break
            except ValueError:
                continue
        self.gop = self._estimate_gop()

    def _estimate_gop(self):
        """Intervalo típico entre keyframes, muestreando paquetes (sólo demux, sin decodificar) al
        inicio y en el medio del vídeo. Devuelve None si no se ven dos keyframes en la muestra."""
        n = 400
        intervals = "%%+#%d" % n
        if self.duration > 30:
            intervals += ",%.1f%%+#%d" % (self.duration / 2.0, n)
        cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "packet=pts_time,dts_time,flags", "-of", "csv=p=0", "-read_intervals", intervals,
               str(self.path)]
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, env=FF_ENV)
        except (OSError, subprocess.TimeoutExpired):
            return None
        gaps, prev = [], None
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split(",")
            if len(parts) < 3:
                continue
            flags = parts[2]
            t = parts[0] if parts[0] not in ("", "N/A") else parts[1]
            try:
                t = float(t)
            except ValueError:
                continue
            if "K" in flags:
                if prev is not None and 0 < t - prev < 600:
                    gaps.append(t - prev)
                prev = t
            # reinicio entre las dos muestras: un salto hacia atrás delata el cambio de intervalo
            if prev is not None and t < prev - 1:
                prev = None
        if not gaps:
            return None
        gaps.sort()
        return gaps[len(gaps) // 2]


# ----------------------------------------------------------------------------------------------
# GPU (VAAPI) — sólo se usa en modo tramos, tras un autotest contra la decodificación software
# ----------------------------------------------------------------------------------------------
class Gpu(object):
    _hwaccels = None

    @classmethod
    def device(cls):
        if os.environ.get("THUMBSHEET_HWACCEL", "auto") in ("0", "off", "no"):
            return None
        if cls._hwaccels is None:
            try:
                out = subprocess.run(["ffmpeg", "-hide_banner", "-hwaccels"], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, timeout=10, env=FF_ENV).stdout.decode("utf-8", "replace")
            except (OSError, subprocess.TimeoutExpired):
                out = ""
            cls._hwaccels = set(out.split())
        if "vaapi" not in cls._hwaccels:
            return None
        for dev in sorted(pathlib.Path("/dev/dri").glob("renderD*")):
            if os.access(str(dev), os.R_OK | os.W_OK):
                return str(dev)
        return None

    @staticmethod
    def dec_opts(device):
        return ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi", "-hwaccel_device", device]

    _tested = {}   # (dispositivo, códec, ancho, alto) -> resultado: una autoprueba por tipo de vídeo y sesión

    @classmethod
    def selftest(cls, info, device, workdir):
        key = (device, info.codec, info.width, info.height)
        if key not in cls._tested:
            cls._tested[key] = cls._selftest(info, device, workdir)
        return cls._tested[key]

    @staticmethod
    def _selftest(info, device, workdir):
        """Decodifica un fotograma por GPU y por CPU y compara: si difieren, la GPU miente (driver
        sin soporte real del perfil, fotogramas verdes...) y no se usa."""
        if info.rotated:   # la autorrotación es un filtro software: incompatible con frames vaapi
            return False
        workdir.mkdir(parents=True, exist_ok=True)
        t = min(max(1.0, info.duration / 3.0), 30.0)
        sw, hw = workdir / "sw.jpg", workdir / "hw.jpg"
        size = "scale=%d:%d" % (info.thumb_w, info.thumb_h)
        cmds = [
            FFMPEG + SW_DEC + ["-ss", "%.3f" % t, "-i", str(info.path), "-map", "0:v:0", "-an", "-sn", "-dn",
                               "-frames:v", "1", "-vf", size, "-q:v", "4", "-f", "image2", "-update", "1", "-y", str(sw)],
            FFMPEG + Gpu.dec_opts(device) + ["-ss", "%.3f" % t, "-i", str(info.path), "-map", "0:v:0", "-an",
                                             "-sn", "-dn", "-frames:v", "1",
                                             "-vf", "scale_vaapi=w=%d:h=%d,hwdownload,format=nv12" % (info.thumb_w, info.thumb_h),
                                             "-q:v", "4", "-f", "image2", "-update", "1", "-y", str(hw)],
        ]
        try:
            for c in cmds:
                r = subprocess.run(c, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60, env=FF_ENV)
                if r.returncode != 0:
                    log("gpu selftest: ffmpeg falló:", r.stderr.decode("utf-8", "replace")[-200:])
                    return False
            a = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(sw), 32, 32, True)
            b = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(hw), 32, 32, True)
            if (a.get_width(), a.get_height()) != (b.get_width(), b.get_height()):
                return False
            pa, pb = bytes(a.get_pixels()), bytes(b.get_pixels())
            n = min(len(pa), len(pb))
            diff = sum(abs(pa[i] - pb[i]) for i in range(n)) / float(n or 1)
            log("gpu selftest: diferencia media %.1f/255" % diff)
            return diff < 16
        except Exception as e:  # noqa: BLE001
            log("gpu selftest: excepción", e)
            return False
        finally:
            shutil.rmtree(str(workdir), ignore_errors=True)


# ----------------------------------------------------------------------------------------------
# generación de miniaturas
# ----------------------------------------------------------------------------------------------
_PLAN_CACHE = {}


def plan_timestamps(duration, interval):
    """Instantes de captura: 0, S, 2S... sin pasarse del final (la última debe tener medio segundo de margen).
    Memorizado: se pide muchas veces por segundo y con miles de instantes no es gratis. NO modificar la lista."""
    key = (float(duration), int(interval))
    out = _PLAN_CACHE.get(key)
    if out is None:
        D = float(duration)
        out = [t for t in range(0, int(math.floor(D)) + 1, int(interval)) if t <= D - 0.5] or [0]
        if len(_PLAN_CACHE) > 64:
            _PLAN_CACHE.clear()
        _PLAN_CACHE[key] = out
    return out


_RE_FF_PREFIX = re.compile(r"^\[[^\]]*\]\s*")
_FF_GENERIC = ("Conversion failed", "Nothing was written into output", "Error opening input file ", "Command exited")


def short_error(text, default="error", limit=160):
    """Resumen en una línea de un stderr de ffmpeg/ffprobe (o de cualquier mensaje): la última línea con sustancia,
    sin el prefijo «[mov,mp4 @ 0x…]» ni la ruta del fichero, acortada; para tooltips, barra de estado y toasts.
    Si la última es el genérico «Invalid data found…» y hay otra antes (p. ej. «moov atom not found»), se añade."""
    lines = []
    for raw in (text or "")[-4000:].splitlines():
        line = _RE_FF_PREFIX.sub("", raw.strip())
        if not line or line.startswith(_FF_GENERIC):
            continue
        head, sep, rest = line.partition(": ")
        if sep and rest and (os.sep in head or re.search(r"\.\w{2,4}$", head)):
            line = rest   # «/ruta/vídeo.mp4: Invalid data found…» → «Invalid data found…»
        lines.append(line)
    if not lines:
        return default
    out = lines[-1]
    if out.startswith("Invalid data found") and len(lines) > 1 and lines[-2] != out:
        out = "%s (%s)" % (out, lines[-2])
    return out if len(out) <= limit else out[:limit - 1] + "…"


_RETRY_ERRNOS = {errno.EBUSY, errno.ETXTBSY, errno.EAGAIN, errno.EACCES, errno.EPERM}


def _fs_with_retry(op, path, timeout, verb, missing_ok):
    """Ejecuta `op()` (borrar, renombrar) reintentando durante `timeout` s si el fallo puede ser pasajero: un proceso
    que aún tiene el fichero abierto y está terminando. En un montaje FUSE (sftp por gvfs, sshfs) borrar un fichero
    abierto se resuelve renombrándolo a .fuse_hidden… y, si el servidor no lo permite, falla con «recurso ocupado»;
    un servidor Windows contesta «permiso denegado» mientras alguien lo tiene abierto. Devuelve None si salió bien
    o el texto del error."""
    path = pathlib.Path(path)
    deadline = time.monotonic() + timeout
    delay, first = 0.1, None
    while True:
        try:
            op()
            if first is not None:
                log("%s: %s hecho tras reintentar %.1f s" % (verb, path.name, time.monotonic() - first))
            return None
        except FileNotFoundError as e:
            if missing_ok:
                return None
            return "%s\n%s" % (path, e)
        except OSError as e:
            if e.errno not in _RETRY_ERRNOS or time.monotonic() >= deadline:
                hint = ""
                if e.errno in (errno.EBUSY, errno.ETXTBSY):
                    hint = "\nEl archivo sigue abierto en otro programa (un montaje remoto no deja tocarlo mientras tanto)."
                return "%s\n%s%s" % (path, e, hint)
            if first is None:
                first = time.monotonic()
                log("%s: %s: %s; reintentando" % (verb, path.name, e.strerror))
            time.sleep(delay)
            delay = min(1.0, delay * 2)


def remove_with_retry(path, timeout):
    """os.remove con reintentos (ver _fs_with_retry). None si se borró o ya no existía; si no, el texto del error."""
    return _fs_with_retry(lambda: os.remove(str(path)), path, timeout, "borrar", missing_ok=True)


def rename_with_retry(path, target, timeout):
    """os.rename con reintentos (ver _fs_with_retry). None si se renombró; si no, el texto del error."""
    return _fs_with_retry(lambda: os.rename(str(path), str(target)), path, timeout, "renombrar", missing_ok=False)


def doc_complete(doc, interval):
    """¿Están ya en caché todas las capturas de este vídeo para este intervalo?"""
    if doc.info is None or doc.cache_dir is None:
        return True
    return all((doc.cache_dir / ("%d.jpg" % t)).exists() or (doc.cache_dir / ("%d.fail" % t)).exists()
               for t in plan_timestamps(doc.info.duration, interval))


class Generator(object):
    """Genera las capturas de un intervalo dado. Avisa por GLib.idle_add: on_tile(t), on_failed(t) (no hay
    fotograma que sacar: datos truncados o dañados, queda marcado con <t>.fail y no se reintenta),
    on_progress(done, total), on_done(). Cancelable (mata los ffmpeg en marcha). Si el vídeo no da NINGÚN
    fotograma se desiste pronto (self.error; ver _maybe_give_up) en vez de marcar cientos de capturas una a una.

    Orden de trabajo (ver _task_key): primero lo que está a la vista (focus_fn), luego lo que viene por delante
    y al final lo que quedó atrás. Sólo cambia el orden, no el trabajo."""

    GPU_WORKERS = 2
    _live = weakref.WeakSet()   # generadores con hilos o ffmpeg vivos (quiesce: esperar antes de borrar su vídeo)

    def __init__(self, info, cache_dir, interval, on_tile, on_progress, on_done, on_failed=None, focus_fn=None,
                 on_aspect=None, on_tiles=None):
        Generator._live.add(self)
        self.info = info
        self.cache_dir = cache_dir
        self.S = snap_interval(interval)
        self.on_tile, self.on_progress, self.on_done = on_tile, on_progress, on_done
        self.on_failed = on_failed or (lambda t: False)
        self.on_aspect = on_aspect or (lambda t, a: False)   # on_aspect(t, a): la miniatura t no tiene el aspecto nominal
        self.on_tiles = on_tiles                              # on_tiles([t…]): las que ya estaban en caché, de golpe
        self.nominal = info.aspect
        self.aspects = {}            # t -> aspecto real de las que difieren del nominal (sidecar aspects.json)
        self._aspects_dirty = 0
        self.focus_fn = focus_fn or (lambda: None)   # () -> (t_lo, t_hi) a la vista, o None (segundo plano)
        self._order_key = None                       # foco con el que se ordenó la cola la última vez
        self._order_time = 0.0
        self.pending_ts = set()                      # instantes sin resolver: cada uno se cuenta una sola vez
        self.initial_done = 0      # capturas que ya estaban en caché al arrancar (para estimar el ritmo)
        self.eta_logged = False
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.procs = set()
        self.tasks = []            # pendientes; se reordenan por prioridad cuando cambia el foco (_pop_task)
        self.pending = 0
        self.done_count = 0
        self.finished = False
        self.error = None          # texto si se ha desistido: el vídeo no da ningún fotograma (ver _maybe_give_up)
        self.hard_fails = 0        # capturas imposibles (ffmpeg terminó sin fotograma) en esta pasada
        self.frames_made = 0       # fotogramas obtenidos en esta pasada
        self.had_frames = False    # ya había alguno en caché al arrancar
        self.last_err = ""         # resumen del último stderr de ffmpeg con error
        self._verifying = False
        self._fails_written = []   # marcas .fail de esta pasada (se retiran si al final se desiste)
        self.gpu_ok = False
        self.threads = []
        self.started = time.time()

        self.timestamps = plan_timestamps(info.duration, self.S)
        self.total = len(self.timestamps)
        self.workers = default_workers()
        gop = info.gop if info.gop is not None else max(400.0 / info.fps, 20.0)
        # coste por captura: seek ≈ decodificar GOP/2 + arranque (~1 s de vídeo equivalente); tramos ≈ S
        self.mode = "seek" if (gop / 2.0 + 1.0) < self.S else "range"
        log("plan: S=%d capturas=%d gop=%.1fs fps=%.2f modo=%s workers=%d [%s]" % (
            self.S, self.total, gop, info.fps, self.mode, self.workers, info.path.name))

    # --- API ---
    def start(self):
        t_start = time.perf_counter()
        cached, failed = set(), set()
        try:
            for name in os.listdir(str(self.cache_dir)):
                if name.endswith(".jpg") and name[:-4].isdigit():
                    cached.add(int(name[:-4]))
                elif name.endswith(".fail") and name[:-5].isdigit():
                    failed.add(int(name[:-5]))
        except OSError:
            pass
        failed -= cached
        self.had_frames = bool(cached)
        missing = [t for t in self.timestamps if t not in cached and t not in failed]
        self.pending_ts = set(missing)
        self.done_count = self.initial_done = self.total - len(missing)
        try:
            data = json.loads((self.cache_dir / "aspects.json").read_text(encoding="utf-8"))
            self.aspects = dict((int(k), float(v)) for k, v in data.items())
        except (OSError, ValueError, TypeError, AttributeError):
            self.aspects = {}
        cached_ts = [t for t in self.timestamps if t in cached]
        if self.on_tiles is not None:
            self.on_tiles(cached_ts)
        else:
            for t in cached_ts:
                self.on_tile(t)
        for t in cached_ts:
            if t in self.aspects:
                self.on_aspect(t, self.aspects[t])
        for t in self.timestamps:
            if t in failed:
                self.on_failed(t)   # ya se intentó y no hay fotograma: no se reintenta
        self._progress()
        if DEBUG:
            log("start: %.1f ms en el hilo de la interfaz (%d en caché, %d por hacer)" % (
                1000 * (time.perf_counter() - t_start), len(cached), len(missing)))
        if not missing:
            self._finish()
            return
        if self.mode == "seek":
            for t in missing:
                self.tasks.append(("seek", t))
        else:
            self._make_chunks(missing)
        self.pending = len(self.tasks)
        nworkers = min(self.workers, len(self.tasks))
        for i in range(nworkers):
            th = threading.Thread(target=self._worker, args=("cpu", None), name="ts-cpu-%d" % i, daemon=True)
            th.start()
            self.threads.append(th)
        dev = Gpu.device() if self.mode == "range" and len(self.tasks) > 1 else None
        if dev:
            th = threading.Thread(target=self._gpu_bootstrap, args=(dev,), name="ts-gpu", daemon=True)
            th.start()
            self.threads.append(th)

    def wait_procs(self, timeout):
        """Espera (como mucho `timeout` s) a que no quede ningún ffmpeg de este generador; True si no queda ninguno.
        Para llamar fuera del hilo de la interfaz."""
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                alive = [p for p in self.procs if p.returncode is None and p.poll() is None]
            if not alive:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    @classmethod
    def quiesce(cls, path, timeout=6.0):
        """Espera a que ningún generador de `path` (cancelado o no) tenga ffmpeg vivos: antes de borrar un vídeo
        nadie debe tenerlo abierto (ver remove_with_retry). True si se consiguió dentro del plazo."""
        deadline = time.monotonic() + timeout
        ok = True
        for gen in list(cls._live):
            if gen.info.path == path:
                ok = gen.wait_procs(max(0.0, deadline - time.monotonic())) and ok
        return ok

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            self.tasks.clear()
            procs = list(self.procs)
        self._flush_aspects()
        for p in procs:
            try:
                p.terminate()
            except OSError:
                pass
        if procs:
            # si alguno no se va con SIGTERM (cuelgue raro de ffmpeg al salir), SIGKILL a los 2 s
            def reap():
                for q in procs:
                    if q.poll() is None:
                        try:
                            q.kill()
                        except OSError:
                            pass
            threading.Timer(2.0, reap).start()

    # --- planificación de tramos ---
    def _make_chunks(self, missing):
        """Agrupa capturas consecutivas en tramos de ~L segundos (múltiplo de S). Tramos cortos
        reparten mejor y permiten ver resultados pronto; cada uno paga decodificar desde el keyframe
        anterior, así que no se hacen minúsculos."""
        D = self.info.duration
        target = max(30.0, min(300.0, D / (3.0 * self.workers)))
        per_chunk = max(1, int(math.ceil(target / self.S)))
        missing = sorted(missing)
        i = 0
        while i < len(missing):
            start = missing[i]
            ts = [start]
            j = i + 1
            while j < len(missing) and len(ts) < per_chunk and missing[j] == ts[-1] + self.S:
                ts.append(missing[j])
                j += 1
            self.tasks.append(("range", tuple(ts)))
            i = j

    # --- orden de trabajo ---
    @staticmethod
    def _task_key(task, focus):
        """Prioridad (menor = antes). A la vista: por tiempo. Después lo que viene por delante (lo más cercano
        antes) y al final lo que quedó atrás."""
        if task[0] == "seek":
            a = b = task[1]
        else:
            a, b = task[1][0], task[1][-1]
        if focus is None:
            return (1, 0, a)
        lo, hi = focus
        if b >= lo and a <= hi:
            return (0, 0, a)
        if a > hi:
            return (1, 0, a - hi)
        return (1, 1, lo - b)

    def _pop_task(self):
        """Siguiente tarea según el foco actual. Sólo reordena si el foco ha cambiado desde la última vez: en
        reposo no cuesta nada; mientras se hace scroll, un sort de la cola por captura (milisegundos)."""
        with self.lock:
            if not self.tasks:
                return None
            focus = self.focus_fn()
            now = time.monotonic()
            if focus != self._order_key and now - self._order_time >= 0.1:   # en pleno scroll, una vez cada 100 ms
                self._order_key = focus
                self._order_time = now
                t_sort = time.perf_counter()
                self.tasks.sort(key=lambda tk: self._task_key(tk, focus), reverse=True)   # la mejor al final: pop() O(1)
                if DEBUG:
                    _prof("sort(hilo)", time.perf_counter() - t_sort)
                if DEBUG:
                    first = self.tasks[-1]
                    log("orden: foco=%s primera=%s" % (
                        ("%d-%d" % focus) if focus else "ninguno",
                        first[1] if first[0] == "seek" else "%d..%d" % (first[1][0], first[1][-1])))
            return self.tasks.pop()

    # --- workers ---
    def _gpu_bootstrap(self, dev):
        ok = Gpu.selftest(self.info, dev, self.cache_dir / ".gputest")
        if self.cancelled.is_set() or not ok:
            log("gpu: no se usa")
            return
        self.gpu_ok = True
        log("gpu: activa en", dev)
        for i in range(1, self.GPU_WORKERS):
            th = threading.Thread(target=self._worker, args=("gpu", dev), name="ts-gpu-%d" % i, daemon=True)
            th.start()
            self.threads.append(th)
        self._worker("gpu", dev)

    def _worker(self, kind, dev):
        while not self.cancelled.is_set():
            if kind == "gpu" and not self.gpu_ok:
                return
            if self._maybe_give_up():
                return   # el vídeo no se puede decodificar: la cola ya está vacía y _finish llega por la cuenta
            task = self._pop_task()
            if task is None:
                return
            ok = False
            try:
                if task[0] == "seek":
                    ok = self._do_seek(task[1])
                else:
                    ok = self._do_range(task[1], dev if kind == "gpu" else None)
            except Exception as e:  # noqa: BLE001
                log("worker: excepción", e)
            if self.cancelled.is_set():
                return
            if not ok and kind == "gpu" and self.error is None:
                # la GPU ha fallado con este tramo: lo devuelve a la cola y este hilo sigue como worker de CPU. Si
                # se retirara sin más y fuera el último tramo, los workers de CPU ya habrían acabado y nadie lo
                # recogería: el generador no terminaría nunca y el segundo plano quedaría parado.
                log("gpu: fallo en tramo, se desactiva")
                self.gpu_ok = False
                with self.lock:
                    self.tasks.append(task)
                    self._order_key = None
                kind, dev = "cpu", None
                continue
            with self.lock:
                self.pending -= 1
                last = self.pending == 0
            if last:
                GLib.idle_add(self._finish)

    def _run(self, cmd, timeout=None, stderr_path=None):
        """Lanza ffmpeg. Con `stderr_path`, su stderr va a ese fichero en vez de a una tubería: los tramos imprimen
        showinfo (y avisos por fotograma) y una tubería de 64 KB se llena y bloquea a ffmpeg mientras aquí se
        espera a que termine."""
        if self.cancelled.is_set():
            return None
        fh = open(str(stderr_path), "wb") if stderr_path is not None else None
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=fh if fh is not None else subprocess.PIPE,
                                 preexec_fn=PREEXEC, env=FF_ENV)
        finally:
            if fh is not None:
                fh.close()
        p.ts_stderr_path = stderr_path
        with self.lock:
            self.procs.add(p)
        if self.cancelled.is_set():   # cancelado entre la comprobación de arriba y el registro: que no se escape
            try:
                p.terminate()
            except OSError:
                pass
        return p

    def _wait(self, p):
        """(ok, stderr) del proceso."""
        try:
            _, err = p.communicate()
        finally:
            with self.lock:
                self.procs.discard(p)
        if err is None:
            try:
                err = pathlib.Path(p.ts_stderr_path).read_bytes()
            except (OSError, TypeError):
                err = b""
        err = err.decode("utf-8", "replace")
        if p.returncode != 0 and not self.cancelled.is_set():
            log("ffmpeg rc=%s: %s" % (p.returncode, err.strip()[-300:]))
            self.last_err = short_error(err, "ffmpeg falló (rc %s)" % p.returncode)
        return p.returncode == 0, err

    # --- aspecto real de cada miniatura (vídeos que cambian de orientación o resolución) ---
    def _note_aspect(self, t, w, h, sar=1.0):
        """Apunta el aspecto real de la miniatura t si difiere del nominal y avisa (on_aspect)."""
        if not w or not h:
            return
        a = (w * sar) / float(h)
        with self.lock:
            if abs(a / self.nominal - 1.0) <= 0.02:
                changed = self.aspects.pop(t, None) is not None
                a = self.nominal
            else:
                changed = abs(self.aspects.get(t, 0.0) - a) > 1e-3
                self.aspects[t] = a
            if changed:
                self._aspects_dirty += 1
            flush = self._aspects_dirty >= 8
        if changed:
            GLib.idle_add(self.on_aspect, t, a)
        if flush:
            self._flush_aspects()

    def _flush_aspects(self):
        """Sidecar aspects.json: {t: aspecto} de las no nominales, para no releer miles de JPEG al abrir."""
        with self.lock:
            if not self._aspects_dirty:
                return
            self._aspects_dirty = 0
            data = dict((str(t), round(a, 4)) for t, a in sorted(self.aspects.items()))
        try:
            tmp = self.cache_dir / ".aspects.json.tmp"
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(str(tmp), str(self.cache_dir / "aspects.json"))
        except OSError:
            pass

    def _fix_aspects(self, ts, published, err):
        """Modo tramos: ffmpeg no puede cambiar el tamaño de salida a mitad, así que el tramo sale todo a W×H y un
        fotograma de otra proporción llega deformado. showinfo (antes de escalar) deja en stderr el tamaño y SAR
        reales de cada fotograma de salida: se apunta el aspecto y las de proporción distinta se re-encajan a su
        tamaño natural (la imagen deformada conserva toda la información: sólo se le devuelve su forma)."""
        sizes = {}
        for line in err.splitlines():
            if " s:" not in line:
                continue
            mn, ms = _RE_SHOW_N.search(line), _RE_SHOW_S.search(line)
            if not mn or not ms:
                continue
            sar = 1.0
            msar = _RE_SHOW_SAR.search(line)
            if msar and int(msar.group(1)) and int(msar.group(2)):
                sar = int(msar.group(1)) / float(msar.group(2))
            sizes[int(mn.group(1))] = (int(ms.group(1)), int(ms.group(2)), sar)
        W, H = self.info.thumb_w, self.info.thumb_h
        if DEBUG and sizes:
            odd_n = sum(1 for (w, h, sar) in sizes.values() if abs((w * sar / float(h)) / self.nominal - 1.0) > 0.02)
            if odd_n:
                log("tramo %d..%d: %d fotogramas con otra proporción (de %d)" % (ts[0], ts[-1], odd_n, len(sizes)))
        elif DEBUG:
            log("tramo %d..%d: showinfo sin tamaños (stderr %d bytes)" % (ts[0], ts[-1], len(err)))
        for k in range(published):
            size = sizes.get(k)
            if size is None:
                continue
            w, h, sar = size
            a = (w * sar) / float(h)
            odd = abs(a / self.nominal - 1.0) > 0.02
            if odd:
                tw, th = fit_box(a, W, H)
                path = self.cache_dir / ("%d.jpg" % ts[k])
                try:
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), tw, th, False)
                    tmp = self.cache_dir / (".%d.fix.jpg" % ts[k])
                    pb.savev(str(tmp), "jpeg", ["quality"], ["88"])
                    os.replace(str(tmp), str(path))
                except (GLib.Error, OSError):
                    continue
            self._note_aspect(ts[k], w, h, sar)
            if odd:
                GLib.idle_add(self.on_tile, ts[k])   # que el mosaico recargue la miniatura ya re-encajada

    def _out_opts(self, n, vf, out):
        opts = ["-map", "0:v:0", "-an", "-sn", "-dn", "-vf", vf, "-frames:v", str(n), "-q:v", "4",
                "-f", "image2"]
        if n == 1 and "%d" not in out:
            opts += ["-update", "1"]
        return opts + ["-y", out]

    def _do_seek(self, t):
        with self.lock:
            if t not in self.pending_ts:
                return True   # ya resuelto por otra tarea (p. ej. la comprobación de _maybe_give_up)
        final = self.cache_dir / ("%d.jpg" % t)
        tmp = self.cache_dir / (".%d.tmp.jpg" % t)
        vf = fit_vf(self.info.thumb_w, self.info.thumb_h)   # con su proporción real
        cmd = FFMPEG + SW_DEC + ["-ss", str(t), "-i", str(self.info.path)] + self._out_opts(1, vf, str(tmp))
        p = self._run(cmd)
        if p is None:
            return False
        ok, _ = self._wait(p)
        ok = ok and tmp.exists()
        if ok:
            os.replace(str(tmp), str(final))
            try:
                _fmt, w, h = GdkPixbuf.Pixbuf.get_file_info(str(final))   # sólo la cabecera: ~40 µs
                self._note_aspect(t, w, h)
            except GLib.Error:
                pass
            self._tile_ready(t)
        else:
            try:
                tmp.unlink()
            except OSError:
                pass
            # ffmpeg ha terminado por sí mismo sin sacar fotograma (datos truncados o dañados): definitivo.
            # Si lo mató una señal ajena (OOM…) no se marca, para que otra pasada lo reintente.
            self._tile_failed(t, permanent=p.returncode is not None and p.returncode >= 0)
        return ok

    def _do_range(self, ts, dev):
        if dev is None:
            # si una GPU falló a medias ya publicó un prefijo del tramo: no repetirlo
            while len(ts) > 1 and (self.cache_dir / ("%d.jpg" % ts[0])).exists():
                ts = ts[1:]
        a, n = ts[0], len(ts)
        dur = n * self.S + 1.0   # holgura: `-t` de entrada corta por paquetes y podría perder el último
        tmpdir = self.cache_dir / (".range-%d-%d" % (a, os.getpid()))
        shutil.rmtree(str(tmpdir), ignore_errors=True)
        tmpdir.mkdir(parents=True)
        # fps=1/S:round=up => en la ranura k va el ÚLTIMO fotograma con t <= a+kS (el del seek exacto
        # es el primero con t >= T: son el mismo o adyacentes). start_time=0 ancla la ranura 0 al
        # arranque del tramo aunque el primer fotograma caiga unas milésimas después.
        fps = "fps=1/%d:round=up:start_time=0" % self.S
        # salida a tamaño fijo W×H (ffmpeg no lo cambia a mitad de un tramo) y showinfo antes de escalar para saber
        # el tamaño real de cada fotograma; -reinit_filter 0: si el tamaño cambia a mitad, el grafo no se reinicia
        # (fps no pierde la cuenta, showinfo sigue numerando) y _fix_aspects re-encaja las de otra proporción
        W, H = self.info.thumb_w, self.info.thumb_h
        if dev:
            vf = "%s,%s,scale_vaapi=w=%d:h=%d,hwdownload,format=nv12" % (fps, SHOWINFO, W, H)
            dec = Gpu.dec_opts(dev)
        else:
            vf = "%s,%s,scale=%d:%d" % (fps, SHOWINFO, W, H)
            dec = SW_DEC
        cmd = FFMPEG_INFO + dec + ["-reinit_filter", "0", "-ss", str(a), "-t", "%.1f" % dur, "-i", str(self.info.path)] + \
            self._out_opts(n, vf, str(tmpdir / "%d.jpg"))
        p = self._run(cmd, stderr_path=tmpdir / "stderr.txt")
        if p is None:
            shutil.rmtree(str(tmpdir), ignore_errors=True)
            return False
        published = 0
        try:
            # publica cada captura en cuanto está completa: el muxer image2 cierra k antes de abrir k+1
            while True:
                try:
                    p.wait(timeout=0.15)
                    running = False
                except subprocess.TimeoutExpired:
                    running = True
                while published < n:
                    k = published + 1
                    if not running or (tmpdir / ("%d.jpg" % (k + 1))).exists():
                        if (tmpdir / ("%d.jpg" % k)).exists():
                            self._publish(tmpdir / ("%d.jpg" % k), ts[published])
                            published += 1
                            continue
                    break
                if not running:
                    break
            ok, err = self._wait(p)
            if self.cancelled.is_set():
                return False
            if dev is not None and not (ok and published == n):
                # la GPU sólo cuenta como OK si ha producido todo; si no, la CPU rehace lo que falte
                return False
            self._fix_aspects(ts, published, err)
            # lo que el tramo no ha producido (datos truncados o dañados) se reintenta una a una con seek;
            # lo que tampoco salga así queda marcado como no generable
            for i in range(published, n):
                if self.cancelled.is_set() or self._maybe_give_up():
                    return False
                self._do_seek(ts[i])
            return ok
        finally:
            shutil.rmtree(str(tmpdir), ignore_errors=True)

    def _publish(self, src, t):
        try:
            os.replace(str(src), str(self.cache_dir / ("%d.jpg" % t)))
        except OSError:
            self._tile_failed(t, permanent=False)
            return
        self._tile_ready(t)

    def _tile_ready(self, t):
        with self.lock:
            self.frames_made += 1
            fresh = t in self.pending_ts
            if fresh:
                self.pending_ts.discard(t)
                self.done_count += 1
        if not fresh:
            # ya resuelto por otra tarea (nivel + tramo): no se cuenta dos veces; si había quedado como
            # imposible, ya no lo es
            try:
                (self.cache_dir / ("%d.fail" % t)).unlink()
            except OSError:
                pass
        GLib.idle_add(self.on_tile, t)
        if fresh:
            self._progress()

    def _tile_failed(self, t, permanent=True):
        if self.cancelled.is_set():
            return
        with self.lock:
            if t not in self.pending_ts:
                return   # ya resuelto por otra tarea
            self.pending_ts.discard(t)
            self.done_count += 1
            permanent = permanent and self.error is None   # desistido: sin marcas ni avisos
            if permanent:
                self.hard_fails += 1
                self._fails_written.append(t)
        if permanent:
            try:
                (self.cache_dir / ("%d.fail" % t)).touch()
            except OSError:
                pass
            GLib.idle_add(self.on_failed, t)
        self._progress()

    GIVE_UP_FAILS = 3   # capturas imposibles, sin ningún fotograma del vídeo, antes de comprobar si es decodificable

    def _maybe_give_up(self):
        """Vídeo que no se puede decodificar (códec sin soporte, fichero que no es lo que dice, DRM, datos a ceros…):
        ffprobe da duración y resolución, pero ninguna captura sale. Marcarlas una a una como imposibles costaría un
        ffmpeg por captura (minutos en un vídeo largo, más en remoto), bloquearía el segundo plano mientras tanto y
        dejaría el icono como «listas». Así que, cuando han fallado ya unas cuantas sin que este vídeo haya dado
        NINGÚN fotograma (ni en esta pasada ni en caché), se prueba una captura en mitad del vídeo: si tampoco sale,
        se desiste (self.error), se vacía la cola y no se deja ninguna marca .fail (si fue algo pasajero, la próxima
        pasada lo reintenta). Si la del medio sale, es un vídeo dañado sólo al principio y se sigue con normalidad.
        Devuelve True si se ha desistido. Lo llaman los workers antes de cada tarea y los tramos en su reintento."""
        with self.lock:
            if self.error is not None:
                return True
            if self.frames_made or self.had_frames or self.hard_fails < self.GIVE_UP_FAILS or self.cancelled.is_set():
                return False
            verify = not self._verifying
            self._verifying = True
        if not verify:   # otro worker ya está comprobando: esperar su veredicto en vez de seguir fallando capturas
            while self._verifying and not self.cancelled.is_set():
                time.sleep(0.05)
            return self.error is not None
        try:
            with self.lock:
                pend = sorted(self.pending_ts)
            if not pend:
                return False
            goal = self.info.duration / 2.0
            mid = min(pend, key=lambda t: abs(t - goal))
            log("sin fotogramas tras %d capturas fallidas: se comprueba a los %d s [%s]" % (
                self.hard_fails, mid, self.info.path.name))
            if self._do_seek(mid) or self.cancelled.is_set():
                return False
            with self.lock:
                self.error = self.last_err or "ffmpeg no saca ningún fotograma"
                self.pending -= len(self.tasks)   # lo encolado no se va a hacer; sólo quedan las tareas en curso
                self.tasks.clear()
                last = self.pending <= 0
            if last:
                GLib.idle_add(self._finish)
            return True
        finally:
            self._verifying = False

    def _progress(self):
        GLib.idle_add(self.on_progress, self.done_count, self.total)

    def _finish(self):
        if self.finished or self.cancelled.is_set():
            return False
        self.finished = True
        self._flush_aspects()
        if self.error is not None:
            for t in self._fails_written:   # las pocas marcas de la comprobación: que la próxima pasada reintente
                try:
                    (self.cache_dir / ("%d.fail" % t)).unlink()
                except OSError:
                    pass
            log("fin: sin fotogramas, se desiste tras %d capturas y %.1fs [%s]: %s" % (
                self.hard_fails, time.time() - self.started, self.info.path.name, self.error))
        else:
            log("fin: %d capturas en %.1fs (%s) [%s]" % (self.total, time.time() - self.started, self.mode, self.info.path.name))
        self.on_done()
        return False


# ----------------------------------------------------------------------------------------------
# caché de pixbufs (LRU por bytes) y cargador en segundo plano
# ----------------------------------------------------------------------------------------------
class PixCache(object):
    """Miniaturas decodificadas como superficies cairo (listas para pintar sin convertir), LRU acotada por bytes."""

    def __init__(self, budget):
        self.budget = budget
        self.items = collections.OrderedDict()   # t -> cairo.ImageSurface
        self.bytes = 0
        self.lock = threading.Lock()

    @staticmethod
    def _size(surface):
        return surface.get_stride() * surface.get_height()

    def get(self, t):
        with self.lock:
            pb = self.items.get(t)
            if pb is not None:
                self.items.move_to_end(t)
            return pb

    def put(self, t, surface):
        size = self._size(surface)
        with self.lock:
            old = self.items.pop(t, None)
            if old is not None:
                self.bytes -= self._size(old)
            self.items[t] = surface
            self.bytes += size
            while self.bytes > self.budget and len(self.items) > 1:
                _, victim = self.items.popitem(last=False)
                self.bytes -= self._size(victim)

    def clear(self):
        with self.lock:
            self.items.clear()
            self.bytes = 0


class Loader(object):
    """Hilo que decodifica miniaturas al tamaño pedido. Dos colas: lo que está en pantalla (se atiende primero,
    y dentro de ella lo último pedido) y la precarga de fuera de pantalla, que sólo se toca cuando no queda nada
    visible por sacar. Descarta lo que ya no está ni a la vista ni en el margen de precarga."""

    def __init__(self, cache, visible_fn, redraw_fn, aspect_fn=None):
        self.cache = cache
        self.visible_fn = visible_fn      # () -> (set de t visibles o None=todo)
        self.redraw_fn = redraw_fn
        self.aspect_fn = aspect_fn        # (t, aspecto) si el JPEG no tiene la proporción de su tesela
        self.cv = threading.Condition()
        self.queue = collections.OrderedDict()   # t -> (path, width, height): a la vista
        self.back = collections.OrderedDict()    # precarga fuera de pantalla
        self.redraw_pending = False
        self.loaded = set()                      # cargadas desde el último repintado (sólo se repintan ésas)
        th = threading.Thread(target=self._run, name="ts-loader", daemon=True)
        th.start()

    def request(self, t, path, width, height, prefetch=False):
        with self.cv:
            if prefetch:
                if t in self.queue:
                    return
                self.back.pop(t, None)
                self.back[t] = (path, width, height)
            else:
                self.back.pop(t, None)
                self.queue.pop(t, None)
                self.queue[t] = (path, width, height)
            self.cv.notify()

    def cancel_all(self):
        with self.cv:
            self.queue.clear()
            self.back.clear()

    def _run(self):
        while True:
            with self.cv:
                while not self.queue and not self.back:
                    self.cv.wait()
                if self.queue:
                    t, (path, width, height) = self.queue.popitem(last=True)
                else:
                    t, (path, width, height) = self.back.popitem(last=True)
            vis = self.visible_fn()
            if vis is not None and t not in vis:
                continue
            t0 = time.perf_counter()
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), width, height, True)
                surface = Gdk.cairo_surface_create_from_pixbuf(pb, 1, None)   # conversión aquí, no en cada pintado
            except GLib.Error:
                continue
            if DEBUG:
                _prof("loader(hilo)", time.perf_counter() - t0)
            self.cache.put(t, surface)
            if self.aspect_fn is not None and pb.get_height() > 0 and height > 0:
                pa, ta = pb.get_width() / float(pb.get_height()), width / float(height)
                if abs(pa / ta - 1.0) > 0.02:
                    GLib.idle_add(self.aspect_fn, t, pa)   # red de seguridad: el JPEG manda sobre lo apuntado
            with self.cv:
                self.loaded.add(t)
                if not self.redraw_pending:
                    self.redraw_pending = True
                    GLib.idle_add(self._redraw)

    def _redraw(self):
        with self.cv:
            self.redraw_pending = False
            loaded, self.loaded = self.loaded, set()
        self.redraw_fn(loaded)
        return False


# ----------------------------------------------------------------------------------------------
# el mosaico
# ----------------------------------------------------------------------------------------------
# ----------------------------------------------------------------------------------------------
# «Snap scroll»: paradas del visor en filas enteras. Parada k = desplazamiento que deja la fila k arriba, en el mismo
# sitio que la primera con el visor a cero: k·pitch en rejilla regular, tops[k] con filas de altura propia.
# ----------------------------------------------------------------------------------------------
def snap_index(v, n, pitch, tops=None):
    """Fila (0..n-1) cuya parada está más cerca del desplazamiento v."""
    if n <= 0:
        return 0
    if tops is None:
        k = int(round(v / float(pitch)))
    else:
        i = bisect.bisect_left(tops, v)
        k = i if i < n and (i == 0 or tops[i] - v < v - tops[i - 1]) else i - 1
    return max(0, min(n - 1, k))


def snap_top(k, pitch, tops=None):
    return tops[k] if tops is not None else k * pitch


def snap_last(n, pitch, natural_h, page, tops=None):
    """Última parada necesaria: la primera fila que, puesta arriba, deja ver el final del contenido."""
    need = natural_h - page
    if n <= 0 or need <= 0:
        return 0
    k = int(math.ceil(need / float(pitch))) if tops is None else bisect.bisect_left(tops, need)
    return min(n - 1, k)


def snap_target(v, n, pitch, natural_h, page, tops=None):
    """La parada más cercana a v, sin pasar de la última necesaria. Si una fila es más alta que el visor (ventana
    diminuta), el final del contenido también es parada, para poder verlo."""
    if n <= 0:
        return 0.0
    k = min(snap_index(v, n, pitch, tops), snap_last(n, pitch, natural_h, page, tops))
    t = float(snap_top(k, pitch, tops))
    end = natural_h - page
    if k == n - 1 and end > t + 0.5 and abs(v - end) < abs(v - t):
        t = float(end)
    return t


def snap_step(v, rows, n, pitch, natural_h, page, tops=None):
    """Parada a `rows` filas (positivo = bajar) de la más cercana a v, sin salir de [0, última necesaria]."""
    if n <= 0:
        return 0.0
    last = snap_last(n, pitch, natural_h, page, tops)
    k = max(0, min(last, min(snap_index(v, n, pitch, tops), last) + rows))
    t = float(snap_top(k, pitch, tops))
    end = natural_h - page
    if rows > 0 and k == n - 1 and end > t + 0.5:
        t = float(end)
    return t


def snap_height(n, pitch, natural_h, page, tops=None):
    """Alto del contenido con el relleno justo para que la última parada sea también el tope del scroll."""
    if n <= 0 or page <= 0 or natural_h <= page:
        return natural_h   # sin visor todavía, o todo cabe: no hay scroll que alinear
    return max(natural_h, int(math.ceil(snap_top(snap_last(n, pitch, natural_h, page, tops), pitch, tops) + page)))


def mark_bands(rows, span):
    """Bandas [y0, y1) que ocupan las filas dadas (span(r) = (y, alto)), fundiendo las consecutivas: las marcas
    rojas de la barra de scroll, una por tramo de filas con teselas marcadas."""
    out, last = [], None
    for r in sorted(set(rows)):
        top, h = span(r)
        if last is not None and r == last + 1:
            out[-1] = (out[-1][0], top + h)
        else:
            out.append((top, top + h))
        last = r
    return out


def fit_rect(pw, ph, aw, ah):
    """Rectángulo (x, y, ancho, alto) de una imagen pw×ph encajada y centrada en un área aw×ah, sin deformar."""
    sc = min(aw / float(pw), ah / float(ph))
    dw, dh = pw * sc, ph * sc
    return (aw - dw) / 2.0, (ah - dh) / 2.0, dw, dh


def focus_rows(r0, r1, span, vy1, vy2, min_frac=0.5):
    """De las filas [r0, r1] que tocan el visor [vy1, vy2], las que mandan en la prioridad de generación: la de
    arriba y la de abajo sólo cuentan si se ve al menos `min_frac` de su alto, así que tras un salto se empieza por
    la primera fila que se ve entera y no por la que asoma un trozo. `span(r)` = (y, alto) de las teselas de la
    fila. Si ninguna llega (visor más bajo que una fila), todas las que tocan."""
    def frac(r):
        top, h = span(r)
        return max(0.0, min(top + h, vy2) - max(top, vy1)) / max(float(h), 1e-9)
    f0 = r0 + 1 if frac(r0) < min_frac else r0
    f1 = r1 - 1 if frac(r1) < min_frac else r1
    return (f0, f1) if f0 <= f1 else (r0, r1)


class Sheet(Gtk.DrawingArea):
    GAP = 6
    PAD = 8
    SETTLE_MS = 160
    LABEL_PX = 12          # "Sans 9" a 96 ppp

    def __init__(self):
        super(Sheet, self).__init__()
        self.cache_dir = None
        self.ts = []
        self.index = {}
        self.ready = set()
        self.failed = set()        # instantes sin fotograma posible (aspa roja)
        self.failed_all = False
        self.aspect = 16.0 / 9.0
        self.cols_wanted = COLS_DEF
        self.cols = COLS_DEF
        self.cell_w = 160
        self.cell_h = 90
        self.aspects = {}            # t -> aspecto real de las teselas que no son del aspecto nominal
        self.row_y = None            # geometría por filas (None = rejilla regular, camino rápido)
        self.row_h = None
        self.tile_x = None
        self.tile_w = None
        self._aspect_relayout_id = None
        self._draw_stats = [0, 0.0, 0.0, time.time()]   # DEBUG: dibujados, tiempo total, máximo, último informe
        self.vadj = None                      # ajuste vertical del ScrolledWindow (lo pone la ventana)
        self.cache = PixCache(PIX_BUDGET)
        self.visible = (0, -1)
        self.visible_core = (0, -1)   # a la vista de verdad (sin margen; filas vistas al menos a la mitad): prioridad de generación
        self.vis_lock = threading.Lock()
        self.settled = True
        self._settle_id = None
        self.loader = Loader(self.cache, self._visible_set, self._tiles_loaded, self.set_aspect)
        self._buf = None                           # lienzo de imagen para componer cada fotograma (_draw_buffered)
        self._label_bgs = {}                       # (ancho, alto) -> fondo redondeado de etiqueta ya pintado
        self._text_adv = {}                        # longitud del texto -> ancho en px (dígitos tabulares)
        self._font_ext = None
        self.font = Pango.FontDescription("Sans 9")
        self.selected = set()                 # teselas marcadas: vista derivada de los segmentos del documento
        self.on_drag_begin = None             # () -> None            : la ventana guarda una instantánea
        self.on_drag_apply = None             # (timestamps, mode)    : aplica sobre la instantánea
        self.on_drag_end = None               # () -> None            : persistir / refrescar
        self.on_preview = None                # (t) -> None           : clic derecho = ampliar a ventana completa
        self.on_marks_changed = None          # () -> None            : han cambiado las marcas rojas de la barra de scroll
        self._bands = None                    # bandas de filas marcadas (mark_bands); None = recalcular
        self._drag = None
        self._anchor_t = None                 # última tesela pulsada con el botón izquierdo (para Shift+clic)
        self._pending_center = None   # instante a recentrar en cuanto se asigne la nueva altura
        self._holes = []              # huecos visibles de este dibujado, para pedirlos al cargador de arriba abajo
        self._prev_visible = None     # para pedir la precarga sólo cuando el visor se ha movido
        self._fill_core = None        # DEBUG: medir cuánto tarda en completarse la pantalla tras un salto
        self._fill_t0 = None
        self.snap = False             # «Snap scroll»: el visor sólo se para con una fila entera arriba (snap_value)
        self._snapping = False
        self._snap_id = None
        self._snap_tops = None        # filas de altura propia: desplazamiento del visor que deja cada fila arriba
        self._natural_h = 2 * self.PAD   # alto del contenido, sin el relleno final que añade el modo snap
        self.connect("draw", self.on_draw)
        self.connect("size-allocate", self._on_size_allocate)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                        Gdk.EventMask.BUTTON1_MOTION_MASK)
        self.connect("button-press-event", self.on_press)
        self.connect("motion-notify-event", self.on_motion)
        self.connect("button-release-event", self.on_release)
        self.set_hexpand(True)

    # --- modelo ---
    def set_video(self, aspect, cache_dir):
        self.aspect = max(0.2, min(5.0, aspect))
        self.cache_dir = cache_dir
        self.selected = set()
        self._drag = None
        self._anchor_t = None
        self.cache.clear()
        self.loader.cancel_all()
        self.set_plan([])

    def set_selected(self, selected):
        selected = set(selected)
        if selected == self.selected:
            return
        changed = selected ^ self.selected
        self.selected = selected
        self._marks_dirty()
        if len(changed) > 200:
            self.queue_draw()
        else:
            for t in changed:   # sólo las teselas que cambian
                i = self.index.get(t)
                if i is not None:
                    self._redraw_tile(i)

    def _marks_dirty(self):
        self._bands = None
        if self.on_marks_changed:
            self.on_marks_changed()

    def mark_bands(self):
        """Bandas [y0, y1) del contenido (coordenadas del mosaico) con teselas marcadas: filas enteras, las
        consecutivas fundidas. Son las marcas rojas de la barra de scroll. Se recalculan sólo al cambiar la selección
        o la geometría, no en cada repintado de la barra."""
        if self._bands is None:
            cols = max(1, self.cols)
            rows = [i // cols for i in (self.index.get(t) for t in self.selected) if i is not None]
            self._bands = mark_bands(rows, self._row_span)
        return self._bands

    def _tiles_loaded(self, loaded):
        """El cargador ha decodificado estas miniaturas: repintar sólo sus teselas."""
        if len(loaded) > 150:
            self.queue_draw()
            return
        for t in loaded:
            i = self.index.get(t)
            if i is not None:
                self._redraw_tile(i)

    # --- selección: clic = alternar una tesela; clic y arrastrar = aplicar a un rango contiguo ---
    def _row_of_y(self, cy):
        """Fila que contiene la y (relativa al PAD)."""
        if self.row_y is None:
            return int(cy // (self.cell_h + self.GAP))
        return max(0, bisect.bisect_right(self.row_y, cy) - 1)

    def _tile_at(self, x, y, loose=False):
        if not self.ts:
            return None
        n = len(self.ts)
        cx, cy = x - self.PAD, y - self.PAD
        if cx < 0 or cy < 0:
            if not loose:
                return None
            cx, cy = max(0, cx), max(0, cy)
        row = self._row_of_y(cy)
        if self.row_y is None:
            col, rx = divmod(int(cx), self.cell_w + self.GAP)
            ry = cy - row * (self.cell_h + self.GAP)
            if loose:
                col = min(col, self.cols - 1)
            elif rx >= self.cell_w or ry >= self.cell_h or col >= self.cols:
                return None
            i = row * self.cols + col
        else:
            if row >= len(self.row_y):
                return n - 1 if loose else None
            if not loose and cy - self.row_y[row] >= self.row_h[row]:
                return None   # en el hueco entre filas
            lo, hi = row * self.cols, min(n, (row + 1) * self.cols)
            i = None
            for j in range(lo, hi):
                if cx < self.tile_x[j] + self.tile_w[j]:
                    i = j if (loose or cx >= self.tile_x[j]) else None
                    break
            if i is None:
                if not loose:
                    return None
                i = hi - 1
        if i >= n:
            return n - 1 if loose else None
        return i

    def _nearest_index(self, t):
        if t is None or not self.ts:
            return None
        return min(range(len(self.ts)), key=lambda k: abs(self.ts[k] - t))

    def _run_around(self, i):
        """Índices del tramo contiguo de teselas seleccionadas que contiene a i."""
        a = b = i
        while a - 1 >= 0 and self.ts[a - 1] in self.selected:
            a -= 1
        while b + 1 < len(self.ts) and self.ts[b + 1] in self.selected:
            b += 1
        return a, b

    def _oneshot(self, timestamps, mode):
        """Operación de selección completa (instantánea + aplicar + fin) sin arrastre."""
        self._drag = None
        if self.on_drag_begin:
            self.on_drag_begin()
        if self.on_drag_apply:
            self.on_drag_apply(timestamps, mode)
        if self.on_drag_end:
            self.on_drag_end()

    def on_press(self, widget, event):
        if event.type == Gdk.EventType._2BUTTON_PRESS and event.button == 1:
            # Doble clic. GTK ya entregó dos pulsaciones simples (alternar dos veces = como estaba). Si la
            # tesela está seleccionada se quita TODO su tramo contiguo; si no, se deja seleccionada.
            i = self._tile_at(event.x, event.y)
            if i is not None:
                if self.ts[i] in self.selected:
                    a, b = self._run_around(i)
                    self._oneshot([self.ts[k] for k in range(a, b + 1)], False)
                else:
                    self._oneshot([self.ts[i]], True)
            return True
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return event.button in (1, 3)
        if event.button == 3:
            i = self._tile_at(event.x, event.y)
            if i is not None and self.on_preview:
                self.on_preview(self.ts[i])
            return True
        if event.button != 1:
            return False
        i = self._tile_at(event.x, event.y)
        if i is None:
            return False
        anchor = self._nearest_index(self._anchor_t)
        if anchor is None and event.state & Gdk.ModifierType.SHIFT_MASK:
            anchor = 0   # Shift+clic sin tesela previa (vídeo recién abierto): desde el principio
        self._anchor_t = self.ts[i]
        if event.state & Gdk.ModifierType.SHIFT_MASK and anchor is not None:
            # Shift+clic: seleccionar todo entre la última tesela pulsada y esta
            a, b = sorted((anchor, i))
            self._oneshot([self.ts[k] for k in range(a, b + 1)], True)
            return True
        self._drag = {"i0": i, "mode": self.ts[i] not in self.selected, "last": None}
        if self.on_drag_begin:
            self.on_drag_begin()
        self._apply_drag(i)
        return True

    def on_motion(self, widget, event):
        if self._drag is None or not (event.state & Gdk.ModifierType.BUTTON1_MASK):
            return False
        i = self._tile_at(event.x, event.y, loose=True)
        if i is not None and i != self._drag["last"]:
            self._apply_drag(i)
        return True

    def on_release(self, widget, event):
        if event.button != 1 or self._drag is None:
            return False
        self._drag = None
        if self.on_drag_end:
            self.on_drag_end()
        return True

    def _apply_drag(self, i):
        d = self._drag
        a, b = sorted((d["i0"], i))
        d["last"] = i
        if self.on_drag_apply:
            self.on_drag_apply([self.ts[k] for k in range(a, b + 1)], d["mode"])

    def set_plan(self, ts):
        self.ts = list(ts)
        self.index = dict((t, i) for i, t in enumerate(self.ts))
        self.ready = set()
        self.failed = set()
        self.aspects = {}   # el generador vuelve a avisar de las no nominales (sidecar) al arrancar
        with self.vis_lock:
            self.visible_core = (0, -1)   # índices de otro plan: sin foco hasta el primer dibujo
        self._relayout()

    def tile_ready(self, t):
        """Captura nueva. Si no está a la vista no hay nada que pintar; si lo está, se pide su decodificación y se
        pinta una sola vez cuando llegue (antes se pintaba el hueco y luego la imagen, para todas las teselas)."""
        t0 = time.perf_counter()
        i = self.index.get(t)
        if i is not None:
            self.ready.add(t)
            with self.vis_lock:
                a, b = self.visible
            if a <= i <= b:
                x, y, w, h = self._tile_rect(i)
                if self.cache.get(t) is None:
                    self.loader.request(t, self.cache_dir / ("%d.jpg" % t), w, h)
                else:
                    self.queue_draw_area(x, y, w, h)
        if DEBUG:
            _prof("tile_ready", time.perf_counter() - t0)
        return False

    def tiles_ready(self, ts):
        """Muchas de golpe (las que ya estaban en caché al arrancar): un solo repintado."""
        self.ready.update(t for t in ts if t in self.index)
        self.queue_draw()
        return False

    def tile_failed(self, t):
        i = self.index.get(t)
        if i is not None:
            self.failed.add(t)
            with self.vis_lock:
                a, b = self.visible
            if a <= i <= b:
                self._redraw_tile(i)
        return False

    def fail_all(self):
        """El vídeo no da ningún fotograma (Generator._maybe_give_up): aspa en todas las que faltan."""
        self.failed.update(t for t in self.ts if t not in self.ready)
        self.queue_draw()
        return False

    # --- posición de lectura: la tesela del centro del visor sigue en el centro al recomponer ---
    def set_aspect(self, t, a):
        """Aspecto real de la tesela t (lo apunta el generador al sacarla; el cargador lo confirma al leerla). Si
        difiere del nominal, su fila se recompone: misma altura para todas, ancho según cada aspecto. Las
        llegadas seguidas se agrupan en un solo recálculo."""
        if a is None or a <= 0 or t not in self.index:
            return False
        if abs(a / self.aspect - 1.0) <= 0.02:
            changed = self.aspects.pop(t, None) is not None
        else:
            changed = abs(self.aspects.get(t, 0.0) - a) > 1e-3
            self.aspects[t] = a
        if changed and self._aspect_relayout_id is None:
            self._aspect_relayout_id = GLib.timeout_add(60, self._aspect_relayout)
        return False

    def _aspect_relayout(self):
        self._aspect_relayout_id = None
        self._relayout(force=True)
        return False

    def center_time(self):
        """Instante de la tesela que está en el centro del visor (None si no hay plan)."""
        if not self.ts or self.vadj is None:
            return None
        y = self.vadj.get_value() + self.vadj.get_page_size() / 2.0
        rows = int(math.ceil(len(self.ts) / float(self.cols)))
        row = max(0, min(rows - 1, self._row_of_y(y - self.PAD)))
        i = min(len(self.ts) - 1, row * self.cols + self.cols // 2)
        return self.ts[i]

    def _on_size_allocate(self, *_):
        self._relayout()
        if self._pending_center is not None:
            # ya con la altura nueva (el Viewport actualiza el ajuste antes de asignarnos): recentrar de verdad
            t, self._pending_center = self._pending_center, None
            self._apply_center(t, final=True)

    def _apply_center(self, t, final=False):
        if t is None or not self.ts or self.vadj is None:
            return False
        i = min(range(len(self.ts)), key=lambda k: abs(self.ts[k] - t))
        row = i // self.cols
        if self.row_y is not None and row < len(self.row_y):
            y = self.PAD + self.row_y[row] + self.row_h[row] / 2.0
        else:
            y = self.PAD + row * (self.cell_h + self.GAP) + self.cell_h / 2.0
        page = self.vadj.get_page_size()
        upper = self.vadj.get_upper()
        self.vadj.set_value(max(0.0, min(max(0.0, upper - page), y - page / 2.0)))
        if final:
            log("scroll: centrada t=%d (cols=%d), centro real ahora t=%s" % (self.ts[i], self.cols, self.center_time()))
        return False

    def scroll_to_time(self, t):
        """Deja centrada la tesela más cercana a t. Se aplica ya (por si el alto no cambió) y de nuevo cuando GTK
        asigna la altura nueva (size-allocate), que es cuando el ajuste deja de recortar el valor; si la altura
        no cambia, no habrá asignación y vale con la primera. Un idle de respaldo cubre el caso en que la
        asignación ya hubiera pasado."""
        if t is None or not self.ts or self.vadj is None:
            return
        self._apply_center(t)
        self._pending_center = t

        def fallback():
            if self._pending_center is not None:
                self._pending_center = None
                self._apply_center(t, final=True)
            return False
        GLib.idle_add(fallback)

    def set_cols(self, n):
        n = int(max(COLS_MIN, min(COLS_MAX, n)))
        if n == self.cols_wanted:
            return
        center = self.center_time()
        log("cols: %d -> %d, centro t=%s" % (self.cols_wanted, n, center))
        self.cols_wanted = n
        self.settled = False
        if self._settle_id:
            GLib.source_remove(self._settle_id)
        self._settle_id = GLib.timeout_add(self.SETTLE_MS, self._settle)
        self._relayout()
        self.scroll_to_time(center)

    def _settle(self):
        self._settle_id = None
        self.settled = True
        self.queue_draw()
        return False

    # --- geometría ---
    def _relayout(self, force=False):
        width = max(1, self.get_allocated_width())
        avail = max(1, width - 2 * self.PAD)
        cols = max(1, self.cols_wanted)
        cell_w = max(24, (avail - (cols - 1) * self.GAP) // cols)
        cell_h = max(8, int(round(cell_w / self.aspect)))
        n = len(self.ts)
        rows = int(math.ceil(n / float(cols))) if n else 0
        changed = force or (cols, cell_w, cell_h) != (self.cols, self.cell_w, self.cell_h)
        self.cols, self.cell_w, self.cell_h = cols, cell_w, cell_h
        if not self.aspects:
            # todas del aspecto nominal: rejilla regular
            self.row_y = self.row_h = self.tile_x = self.tile_w = None
            total_h = 2 * self.PAD + rows * cell_h + max(0, rows - 1) * self.GAP
        else:
            # filas de altura propia: las teselas de una fila miden lo mismo de alto y el ancho va con su aspecto,
            # ocupando la fila entera (en la última, los huecos cuentan como teselas nominales). Una fila de
            # verticales sale más alta que una mixta, y ésta más que una de horizontales. Sin márgenes negros.
            inner = max(1.0, float(avail - (cols - 1) * self.GAP))
            row_y, row_h, tile_x, tile_w = [], [], [], []
            y = 0.0
            for r in range(rows):
                lo, hi = r * cols, min(n, (r + 1) * cols)
                asp = [self.aspects.get(self.ts[i], self.aspect) for i in range(lo, hi)]
                h = max(8.0, inner / (sum(asp) + (cols - (hi - lo)) * self.aspect))
                row_y.append(y)
                row_h.append(h)
                x = 0.0
                for a in asp:
                    tile_x.append(x)
                    tile_w.append(a * h)
                    x += a * h + self.GAP
                y += h + self.GAP
            self.row_y, self.row_h, self.tile_x, self.tile_w = row_y, row_h, tile_x, tile_w
            total_h = int(math.ceil(2 * self.PAD + y - self.GAP)) if rows else 2 * self.PAD
        # paradas del modo snap: en rejilla regular salen de la cuenta (fila k = k·(alto+hueco)); con filas de
        # altura propia, del mismo redondeo con que se dibujan (_tile_rect)
        self._snap_tops = [int(round(v)) for v in self.row_y] if self.row_y is not None else None
        self._natural_h = total_h
        self._marks_dirty()   # las filas han podido cambiar de sitio: las marcas de la barra también
        self._apply_height()
        if self.snap:
            self._snap_soon()   # las filas pueden haber cambiado de sitio: volver a una parada
        if changed:
            self.queue_draw()

    # --- «Snap scroll»: el visor se para sólo donde una fila entera queda arriba, en el mismo sitio que la
    #     primera con el visor a cero; así la rejilla no se mueve al desplazarse (salvo filas de distinta altura) ---
    def set_vadj(self, adj):
        self.vadj = adj
        adj.connect("value-changed", self._on_vadj_value)
        adj.connect("changed", self._on_vadj_changed)   # alto del visor o del contenido: recalcular el relleno

    def set_snap(self, on):
        on = bool(on)
        if on == self.snap:
            return
        self.snap = on
        log("snap: %s" % ("activado" if on else "desactivado"))
        self._apply_height()
        if on:
            self._snap_soon()

    def _snap_rows(self):
        if self._snap_tops is not None:
            return len(self._snap_tops)
        return int(math.ceil(len(self.ts) / float(self.cols))) if self.ts else 0

    def _snap_args(self):
        """(n, pitch, alto del contenido, alto del visor, tops) para las funciones snap_*."""
        page = self.vadj.get_page_size() if self.vadj is not None else 0
        return self._snap_rows(), self.cell_h + self.GAP, self._natural_h, page, self._snap_tops

    def snap_value(self, v):
        return snap_target(v, *self._snap_args())

    def snap_scroll(self, steps):
        """Rueda en modo snap: cada paso baja (steps > 0) o sube filas enteras, tantas como desplazaría la rueda
        normal de GTK (página^(2/3) píxeles), y como mínimo una."""
        if self.vadj is None or not self.ts:
            return
        n, pitch, natural_h, page, tops = self._snap_args()
        per = max(1, int(round(page ** (2.0 / 3.0) / float(pitch)))) if page > 0 else 1
        self.vadj.set_value(snap_step(self.vadj.get_value(), steps * per, n, pitch, natural_h, page, tops))

    def _apply_height(self):
        """Alto pedido: el del contenido; con snap, más el relleno justo para que la última parada sea también el
        tope del scroll (queda menos de una fila en blanco al final)."""
        n, pitch, natural_h, page, tops = self._snap_args()
        h = snap_height(n, pitch, natural_h, page, tops) if self.snap and self.ts else natural_h
        if self.get_size_request()[1] != h:
            self.set_size_request(-1, h)
            if self.snap:
                log("snap: alto %d (contenido %d, visor %d)" % (h, self._natural_h, page))

    def _on_vadj_changed(self, adj):
        if self.snap:
            self._snap_soon()

    def _snap_soon(self):
        if self._snap_id is None:
            self._snap_id = GLib.idle_add(self._snap_now)

    def _snap_now(self):
        self._snap_id = None
        if self.snap and self.vadj is not None:
            self._apply_height()
            self._on_vadj_value(self.vadj)
        return False

    def _on_vadj_value(self, adj):
        """Cualquier desplazamiento (barra, centrado al cambiar tamaño o intervalo, ir a un segmento...) acaba en la
        parada más cercana. Arrastrando la barra, la rejilla avanza de fila en fila."""
        if self._snapping:
            return
        v = adj.get_value()
        if not self.snap or not self.ts:
            if DEBUG:
                log("vscroll: %.1f" % v)
            return
        t = self.snap_value(v)
        if abs(t - v) >= 0.5:
            self._snapping = True
            try:
                adj.set_value(t)
            finally:
                self._snapping = False
        if DEBUG:
            n, pitch, _h, _p, tops = self._snap_args()
            log("vscroll: %.1f -> %d (snap, fila %d, paso %s)" % (
                v, int(round(adj.get_value())), snap_index(adj.get_value(), n, pitch, tops),
                pitch if tops is None else "variable"))

    def _tiles_in_rect(self, x1, y1, x2, y2):
        """Índices de las teselas que tocan el rectángulo (coordenadas del mosaico)."""
        n = len(self.ts)
        if not n:
            return ()
        r0 = max(0, self._row_of_y(y1 - self.PAD))
        r1 = self._row_of_y(y2 - self.PAD)
        if self.row_y is None:
            pitch = self.cell_w + self.GAP
            c0 = max(0, int((x1 - self.PAD) // pitch))
            c1 = min(self.cols - 1, int((x2 - self.PAD) // pitch))
            out = []
            for r in range(r0, r1 + 1):
                base = r * self.cols
                for c in range(c0, c1 + 1):
                    if base + c < n:
                        out.append(base + c)
            return out
        out = []
        for r in range(r0, min(r1, len(self.row_y) - 1) + 1):
            for i in range(r * self.cols, min(n, (r + 1) * self.cols)):
                tx = self.PAD + self.tile_x[i]
                if tx < x2 and tx + self.tile_w[i] > x1:
                    out.append(i)
        return out

    def _row_span(self, r):
        """(y, alto) de las teselas de la fila r en coordenadas del mosaico (PAD incluido)."""
        if not self.row_y:
            return self.PAD + r * (self.cell_h + self.GAP), self.cell_h
        r = min(r, len(self.row_y) - 1)
        return self.PAD + self.row_y[r], self.row_h[r]

    def _tile_rect(self, i):
        r, c = divmod(i, self.cols)
        if self.row_y is None or i >= len(self.tile_x):
            x = self.PAD + c * (self.cell_w + self.GAP)
            y = self.PAD + r * (self.cell_h + self.GAP)
            return x, y, self.cell_w, self.cell_h
        return (self.PAD + int(round(self.tile_x[i])), self.PAD + int(round(self.row_y[r])),
                max(1, int(round(self.tile_w[i]))), max(1, int(round(self.row_h[r]))))

    def _redraw_tile(self, i):
        x, y, w, h = self._tile_rect(i)
        self.queue_draw_area(x, y, w, h)

    def focus_range(self):
        """[t_lo, t_hi] de las teselas a la vista (sin margen, y sin la fila que asoma menos de la mitad por arriba
        o por abajo), o None si aún no se ha dibujado el plan. Lo lee el generador desde sus hilos para decidir qué
        captura va primero."""
        with self.vis_lock:
            a, b = self.visible_core
        ts = self.ts
        if b < a or not ts:
            return None
        try:
            return (ts[a], ts[b])
        except IndexError:
            return None

    def _visible_set(self):
        with self.vis_lock:
            a, b = self.visible
        if b < a:
            return set()
        return set(self.ts[max(0, a):b + 1])

    # --- dibujo ---
    def on_draw(self, widget, cr):
        if DEBUG:
            t0 = time.perf_counter()
            self._draw_buffered(cr)
            dt = time.perf_counter() - t0
            _prof("draw", dt)
            st = self._draw_stats
            st[0] += 1
            st[1] += dt
            st[2] = max(st[2], dt)
            now = time.time()
            if now - st[3] >= 2.0 and st[0]:
                log("draw: %d dibujados, media %.1f ms, máximo %.1f ms" % (st[0], 1000 * st[1] / st[0], 1000 * st[2]))
                self._draw_stats = [0, 0.0, 0.0, now]
            return False
        return self._draw_buffered(cr)

    def _draw_buffered(self, cr):
        """Compone el fotograma en un lienzo de imagen en memoria (operaciones de microsegundos) y lo sube al
        servidor gráfico de una vez. Pintando directamente sobre la superficie de la ventana, cada miniatura y
        cada etiqueta eran una subida aparte: con cientos de teselas a la vista, decenas de milisegundos."""
        x1, y1, x2, y2 = cr.clip_extents()
        ox, oy = int(math.floor(x1)), int(math.floor(y1))
        w, h = int(math.ceil(x2)) - ox, int(math.ceil(y2)) - oy
        if w <= 0 or h <= 0:
            return False
        buf = self._buf
        if buf is None or buf.get_width() < w or buf.get_height() < h:
            buf = self._buf = cairo.ImageSurface(cairo.FORMAT_RGB24, max(w, 64), max(h, 64))
        c = cairo.Context(buf)
        c.translate(-ox, -oy)
        for rc in cr.copy_clip_rectangle_list():   # mismo recorte, para que sólo se compongan las teselas sucias
            c.rectangle(rc.x, rc.y, rc.width, rc.height)
        c.clip()
        self._draw_frame(c)
        cr.set_source_surface(buf, ox, oy)
        cr.get_source().set_filter(cairo.FILTER_NEAREST)
        cr.paint()
        return False

    def _draw_frame(self, cr):
        x1, y1, x2, y2 = cr.clip_extents()
        cr.set_source_rgb(0.11, 0.11, 0.12)
        cr.paint()
        if not self.ts:
            return False
        row_h = self.cell_h + self.GAP
        r0 = max(0, self._row_of_y(y1 - self.PAD))
        r1 = self._row_of_y(y2 - self.PAD)
        i0 = min(len(self.ts), r0 * self.cols)   # un repintado bajo el contenido (toast, vídeo corto) cae más allá
        i1 = min(len(self.ts) - 1, (r1 + 1) * self.cols - 1)
        # lo visible y el margen de precarga salen del VISOR real, no del recorte de este dibujado: al repintar
        # una sola tesela recién cargada el recorte es sólo su fila, y con él el cargador tomaba por «ya no se ve»
        # (y descartaba) el resto de la pantalla, que se quedaba en negro hasta otro repintado completo
        vadj = self.vadj
        if vadj is not None and vadj.get_page_size() > 0:
            vy1 = vadj.get_value()
            vy2 = vy1 + vadj.get_page_size()
        else:
            vy1, vy2 = y1, y2
        vr0 = max(0, self._row_of_y(vy1 - self.PAD))
        vr1 = self._row_of_y(vy2 - self.PAD)
        core = (vr0 * self.cols, min(len(self.ts) - 1, (vr1 + 1) * self.cols - 1))
        # prioridad de generación: sólo las filas que se ven al menos a la mitad; la que asoma menos por arriba o
        # por abajo no manda, y tras un salto se empieza por la primera que se ve entera (ver focus_rows)
        fr0, fr1 = focus_rows(vr0, vr1, self._row_span, vy1, vy2)
        focus = (fr0 * self.cols, min(len(self.ts) - 1, (fr1 + 1) * self.cols - 1))
        # precarga: una pantalla por delante y por detrás para que el scroll corto no muestre huecos
        margin = self.cols * max(1, int(math.ceil((vy2 - vy1) / float(row_h))))
        with self.vis_lock:
            self.visible = (max(0, core[0] - margin), min(len(self.ts) - 1, core[1] + margin))
            self.visible_core = focus
            vis_changed = self.visible != self._prev_visible
            self._prev_visible = self.visible
        if DEBUG:
            c0, c1 = core
            holes = sum(1 for i in range(c0, c1 + 1) if self.ts[i] in self.ready and self.cache.get(self.ts[i]) is None)
            if core != self._fill_core:
                self._fill_core = core
                self._fill_t0 = time.perf_counter() if holes else None
                if holes:
                    log("salto: núcleo %d-%d, %d huecos" % (c0, c1, holes))
            elif holes == 0 and self._fill_t0 is not None:
                log("lleno: %d ms" % (1000 * (time.perf_counter() - self._fill_t0)))
                self._fill_t0 = None
        self._holes = []
        # sólo las teselas que tocan los rectángulos sucios de verdad: mientras se genera, las nuevas caen
        # repartidas por la pantalla y la caja envolvente del recorte abarcaba toda la zona visible
        rects = cr.copy_clip_rectangle_list()   # lista de cairo.Rectangle (x, y, width, height)
        if 1 < len(rects) <= 1024:
            todo = set()
            for rc in rects:
                todo.update(self._tiles_in_rect(rc.x, rc.y, rc.x + rc.width, rc.y + rc.height))
            if DEBUG:
                _count("rects", len(rects))
                _count("teselas", len(todo))
                _count("dibujos")
            labels = []
            for i in sorted(todo):
                if i0 <= i <= i1:
                    self._draw_tile(cr, i, labels)
        else:
            if DEBUG:
                _count("rects", len(rects))
                _count("teselas", i1 + 1 - i0)
                _count("dibujos_completos")
            labels = []
            for i in range(i0, i1 + 1):
                self._draw_tile(cr, i, labels)
        self._draw_labels(cr, labels)
        # huecos visibles: pedidos en orden inverso para que el cargador (último pedido primero) los saque de
        # arriba abajo
        for t, w, h in reversed(self._holes):
            self.loader.request(t, self.cache_dir / ("%d.jpg" % t), w, h)
        self._holes = []
        # precarga fuera de pantalla (sin dibujar), sólo cuando el visor se ha movido: en los repintados parciales
        # de las teselas que van llegando no hay nada nuevo que pedir
        if vis_changed:
            c0, c1 = core
            for i in list(range(max(0, c0 - margin), c0)) + list(range(c1 + 1, min(len(self.ts), c1 + 1 + margin))):
                t = self.ts[i]
                if t in self.ready and self.cache.get(t) is None:
                    _x, _y, pw, ph = self._tile_rect(i)
                    self.loader.request(t, self.cache_dir / ("%d.jpg" % t), pw, ph, prefetch=True)
        return False

    def _draw_tile(self, cr, i, labels):
        t = self.ts[i]
        x, y, w, h = self._tile_rect(i)
        t0 = time.perf_counter()
        pb = self.cache.get(t) if t in self.ready else None
        if DEBUG:
            _prof("dt_cache", time.perf_counter() - t0)
            t0 = time.perf_counter()
        if pb is None:
            cr.set_source_rgb(0.18, 0.18, 0.2)
            cr.rectangle(x, y, w, h)
            cr.fill()
            if t in self.failed:
                # zona dañada o truncada: no hay fotograma que sacar
                r = max(5.0, min(w, h) * 0.11)
                cx, cy = x + w / 2.0, y + h / 2.0
                cr.set_source_rgb(0.93, 0.16, 0.16)
                cr.set_line_width(max(2.0, r / 3.0))
                cr.set_line_cap(cairo.LINE_CAP_ROUND)
                cr.move_to(cx - r, cy - r)
                cr.line_to(cx + r, cy + r)
                cr.move_to(cx + r, cy - r)
                cr.line_to(cx - r, cy + r)
                cr.stroke()
            elif t in self.ready:
                self._holes.append((t, w, h))
            if DEBUG:
                _prof("dt_hueco", time.perf_counter() - t0)
                t0 = time.perf_counter()
        else:
            pw, ph = pb.get_width(), pb.get_height()
            if (abs(pw - w) > 1 or abs(ph - h) > 1) and self.settled:
                self.loader.request(t, self.cache_dir / ("%d.jpg" % t), w, h)
            if abs(pw - w) <= 1 and abs(ph - h) <= 1:
                # tamaño exacto (±1 px de redondeo): copia directa, recortada a la celda
                cr.save()
                cr.rectangle(x, y, w, h)
                cr.clip()
                cr.set_source_surface(pb, x, y)
                cr.get_source().set_filter(cairo.FILTER_FAST)
                cr.paint()
                cr.restore()
            else:
                # tamaño intermedio (slider en movimiento) o relación distinta: escalar y encajar
                s = min(w / float(pw), h / float(ph))
                dw, dh = pw * s, ph * s
                dx, dy = x + (w - dw) / 2.0, y + (h - dh) / 2.0
                if dw < w - 0.5 or dh < h - 0.5:
                    cr.set_source_rgb(0.05, 0.05, 0.05)
                    cr.rectangle(x, y, w, h)
                    cr.fill()
                cr.save()
                cr.translate(dx, dy)
                cr.scale(s, s)
                cr.set_source_surface(pb, 0, 0)
                cr.get_source().set_filter(cairo.FILTER_BILINEAR)
                cr.rectangle(0, 0, pw, ph)
                cr.fill()
                cr.restore()
            if DEBUG:
                _prof("dt_imagen", time.perf_counter() - t0)
                t0 = time.perf_counter()
        if w >= 56:
            labels.append((x, y, h, t))   # las etiquetas de tiempo se pintan en lote al final (ver _draw_labels)
        # recuadro rojo de selección, grueso, en los cuatro lados de cada tesela
        if t in self.selected:
            lw = 6 if w >= 120 else 4
            cr.set_source_rgb(0.93, 0.16, 0.16)
            cr.set_line_width(lw)
            cr.rectangle(x + lw / 2.0, y + lw / 2.0, w - lw, h - lw)
            cr.stroke()

    def _draw_labels(self, cr, labels):
        """Etiquetas de tiempo de las teselas pintadas, en lote y con la API de texto simple de cairo (glifos en
        la caché interna de cairo): Pango por etiqueta costaba 0,5 ms y era el 85 % del dibujado. El fondo
        redondeado es una superficie por ancho de texto."""
        if not labels:
            return
        t0 = time.perf_counter()
        cr.save()
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(self.LABEL_PX)
        if self._font_ext is None:
            self._font_ext = cr.font_extents()   # (ascent, descent, height, …)
        asc, desc, fh = self._font_ext[0], self._font_ext[1], self._font_ext[2]
        th = int(math.ceil(fh))
        items = []
        for x, y, h, t in labels:
            text = fmt_time(t)
            tw = self._text_adv.get(len(text))
            if tw is None:   # dígitos tabulares: el ancho depende sólo de la longitud
                tw = int(math.ceil(cr.text_extents(text).x_advance))
                self._text_adv[len(text)] = tw
            bx, by = x + 4, y + h - th - 6
            cr.set_source_surface(self._label_bg(tw + 8, th + 2), bx, by)
            cr.paint()
            items.append((bx + 4, by + 1 + asc, text))
        cr.set_source_rgb(0.95, 0.95, 0.95)
        for tx, ty, text in items:
            cr.move_to(tx, ty)
            cr.show_text(text)
        cr.restore()
        if DEBUG:
            _prof("dt_etiqueta", time.perf_counter() - t0)

    def _label_bg(self, w, h):
        surf = self._label_bgs.get((w, h))
        if surf is None:
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
            c = cairo.Context(surf)
            c.set_source_rgba(0, 0, 0, 0.6)
            self._rounded(c, 0, 0, w, h, 3)
            c.fill()
            self._label_bgs[(w, h)] = surf
        return surf

    @staticmethod
    def _rounded(cr, x, y, w, h, r):
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()


# ----------------------------------------------------------------------------------------------
# vista ampliada de un fotograma (clic derecho sobre una tesela)
# ----------------------------------------------------------------------------------------------
class Preview(Gtk.DrawingArea):
    """Capa negra sobre el panel y el mosaico con el fotograma encajado al tamaño disponible. Primero muestra
    la miniatura ampliada (instantáneo) y, en cuanto ffmpeg saca el fotograma a resolución nativa, lo sustituye."""

    def __init__(self):
        super(Preview, self).__init__()
        self.pixbuf = None
        self.t = None
        self.label = ""
        self.on_close = None
        self.font = Pango.FontDescription("Sans 11")
        self.set_no_show_all(True)
        self.set_halign(Gtk.Align.FILL)
        self.set_valign(Gtk.Align.FILL)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("draw", self.on_draw)
        self.connect("button-press-event", self._on_press)

    def _on_press(self, widget, event):
        if self.on_close:
            self.on_close()
        return True

    def set_frame(self, pixbuf, t, label):
        self.pixbuf, self.t, self.label = pixbuf, t, label
        self.queue_draw()

    def on_draw(self, widget, cr):
        alloc = self.get_allocation()
        aw, ah = max(1, alloc.width), max(1, alloc.height)
        cr.set_source_rgb(0, 0, 0)
        cr.paint()
        pb = self.pixbuf
        if pb is not None:
            pw, ph = pb.get_width(), pb.get_height()
            x, y, dw, dh = fit_rect(pw, ph, aw, ah)
            sc = dw / float(pw)
            cr.save()
            cr.translate(x, y)
            cr.scale(sc, sc)
            Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_GOOD if sc < 1 else cairo.FILTER_BILINEAR)
            cr.rectangle(0, 0, pw, ph)
            cr.fill()
            cr.restore()
        if self.t is not None:
            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(self.font)
            layout.set_text("%s · %s" % (fmt_time(self.t), self.label), -1)
            tw, th = layout.get_pixel_size()
            bx, by = 10, ah - th - 14
            cr.set_source_rgba(0, 0, 0, 0.65)
            Sheet._rounded(cr, bx, by, tw + 14, th + 6, 4)
            cr.fill()
            cr.set_source_rgb(0.95, 0.95, 0.95)
            cr.move_to(bx + 7, by + 3)
            PangoCairo.show_layout(cr, layout)
        return False


_GST_LOCK = threading.Lock()


def gst_warmup():
    """Carga GStreamer y los plugins del reproductor en un hilo aparte. Crear el primer reproductor costaba
    ~0,7 s en el hilo de la interfaz (registro + carga de playbin, gtksink y lo que arrastran); con los plugins
    ya en memoria, crearlo cuesta milisegundos. El gtksink no se instancia aquí: su widget es de GTK."""
    global _GST_READY
    if not HAVE_GST:
        return
    t0 = time.perf_counter()
    with _GST_LOCK:
        if not _GST_READY:
            Gst.init(None)
            _GST_READY = True
    try:
        Gst.ElementFactory.make("playbin", None)
        for name in ("gtk", "playback", "libav", "videoconvertscale", "videoconvert", "audioconvert"):
            Gst.Plugin.load_by_name(name)
    except Exception as e:  # noqa: BLE001
        log("gst: precarga fallida", e)
    if DEBUG:
        log("gst: precarga en %.0f ms (hilo aparte)" % (1000 * (time.perf_counter() - t0)))


class Player(object):
    """playbin + gtksink embebido en la capa de la vista ampliada. Los avisos del bus llegan en el hilo GTK
    (add_signal_watch) y se reparten por callbacks: on_state(playing), on_eos(), on_error(mensaje)."""

    FLAG_AUDIO = 1 << 1   # GST_PLAY_FLAG_AUDIO

    def __init__(self, audio=True):
        global _GST_READY
        if not HAVE_GST:
            raise RuntimeError("GStreamer no disponible")
        with _GST_LOCK:
            if not _GST_READY:
                Gst.init(None)
                _GST_READY = True
        self.playbin = Gst.ElementFactory.make("playbin", None)
        sink = Gst.ElementFactory.make("gtksink", None)
        if self.playbin is None or sink is None:
            raise RuntimeError("faltan playbin o gtksink (gstreamer1.0-plugins-base, gstreamer1.0-gtk3)")
        self.widget = sink.props.widget
        self.playbin.set_property("video-sink", sink)
        self.audio = True
        if not audio:
            self._disable_audio()
        self.loaded_path = None
        self.want_play = False
        self._pending = None          # (segundo de arranque, reproducir) hasta que el pipeline haga preroll
        self._prerolled = False
        self.on_state = self.on_eos = self.on_error = None
        bus = self.playbin.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)

    def _disable_audio(self):
        self.audio = False
        self.playbin.set_property("flags", self.playbin.get_property("flags") & ~self.FLAG_AUDIO)

    def load(self, path, start_s, play):
        t0 = time.perf_counter()
        self.playbin.set_state(Gst.State.NULL)
        t1 = time.perf_counter()
        self.playbin.set_property("uri", pathlib.Path(path).as_uri())
        self.loaded_path = pathlib.Path(path)
        self.want_play = play
        self._pending = (start_s, play)
        self._prerolled = False
        self.playbin.set_state(Gst.State.PAUSED)
        if DEBUG:
            log("player: load: NULL %.0f ms, PAUSED %.0f ms" % (1000 * (t1 - t0), 1000 * (time.perf_counter() - t1)))

    def _on_message(self, bus, msg):
        t = msg.type
        if t == Gst.MessageType.ASYNC_DONE:
            if not self._prerolled:
                self._prerolled = True
                start_s, play = self._pending or (None, False)
                self._pending = None
                t0 = time.perf_counter()
                if start_s is not None:
                    self.seek(start_s)
                t1 = time.perf_counter()
                if play:
                    self.playbin.set_state(Gst.State.PLAYING)
                if DEBUG:
                    log("player: preroll: seek %.0f ms, PLAYING %.0f ms" % (1000 * (t1 - t0), 1000 * (time.perf_counter() - t1)))
        elif t == Gst.MessageType.STATE_CHANGED and msg.src is self.playbin:
            _old, new, _pending = msg.parse_state_changed()
            if self.on_state:
                self.on_state(new == Gst.State.PLAYING)
        elif t == Gst.MessageType.EOS:
            self.want_play = False
            self.playbin.set_state(Gst.State.PAUSED)
            if self.on_eos:
                self.on_eos()
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            factory = msg.src.get_factory() if msg.src is not None else None
            klass = (factory.get_metadata("klass") or "") if factory else ""
            log("player: error [%s/%s] %s | %s" % (msg.src.get_name() if msg.src else "?", klass, err.message, dbg))
            if self.audio and "Audio" in klass and self.loaded_path is not None:
                # sin dispositivo de sonido utilizable: seguir en silencio desde donde estábamos
                pos = self.position() or (self._pending[0] if self._pending else 0.0)
                path, play = self.loaded_path, self.want_play
                self._disable_audio()
                self.load(path, pos, play)
                return
            if self.on_error:
                self.on_error(err.message)

    def seek(self, s):
        return self.playbin.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                                        max(0, int(float(s) * Gst.SECOND)))

    def play(self):
        self.want_play = True
        self.playbin.set_state(Gst.State.PLAYING)

    def pause(self):
        self.want_play = False
        self.playbin.set_state(Gst.State.PAUSED)

    def stop(self):
        self.want_play = False
        self.loaded_path = None
        self._pending = None
        self.playbin.set_state(Gst.State.NULL)

    def position(self):
        ok, pos = self.playbin.query_position(Gst.Format.TIME)
        return pos / float(Gst.SECOND) if ok and pos >= 0 else None

    def is_playing(self):
        _ok, state, pending = self.playbin.get_state(0)
        target = pending if pending != Gst.State.VOID_PENDING else state
        return target == Gst.State.PLAYING


class PreviewLayer(Gtk.Box):
    """Capa de la vista ampliada: arriba el fotograma fijo o el vídeo, con un recuadro rojo encima cuando lo que se
    ve cae en un segmento marcado (set_marked); abajo play/pausa, barra de progreso (arrastrable: hace de scrubber),
    tiempo y cerrar. La rueda sobre la imagen o el vídeo llega por on_wheel(event)."""

    MARK_W = 6   # grosor del recuadro rojo

    def __init__(self):
        super(PreviewLayer, self).__init__(orientation=Gtk.Orientation.VERTICAL)
        self.on_close = None
        self.on_toggle_play = None
        self.on_seek = None
        self.on_wheel = None
        self.duration = 1.0
        self._dragging = False
        self._updating = False
        self.marked = False
        self.video_aspect = None   # ancho/alto del vídeo cargado, para encajar el recuadro como lo encaja el sink
        self.stack = Gtk.Stack()
        self.stack.set_homogeneous(True)
        self.still = Preview()
        self.still.set_no_show_all(False)   # Preview nace con no_show_all (era la capa entera); aquí es un hijo normal
        self.still.on_close = lambda: self.on_close() if self.on_close else None
        self.still.add_events(Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.SMOOTH_SCROLL_MASK)
        self.stack.add_named(self.still, "still")
        self.video_box = Gtk.EventBox()
        self.video_box.get_style_context().add_class("ts-black")
        self.video_box.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.SMOOTH_SCROLL_MASK)
        self.video_box.connect("button-press-event", lambda w, e: (self.on_toggle_play() if self.on_toggle_play else None) or True)
        self.stack.add_named(self.video_box, "video")
        # el recuadro rojo va en una capa transparente sobre el fotograma o el vídeo (al sink no se le puede pintar encima)
        self.view = Gtk.Overlay()
        self.view.add(self.stack)
        self.frame = Gtk.DrawingArea()
        self.frame.set_has_window(False)   # sin ventana propia: transparente y sin robar clics ni rueda
        self.frame.connect("draw", self._draw_mark)
        self.view.add_overlay(self.frame)
        self.view.set_overlay_pass_through(self.frame, True)
        self.pack_start(self.view, True, True, 0)
        self.connect("scroll-event", lambda w, e: bool(self.on_wheel(e)) if self.on_wheel else False)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.get_style_context().add_class("ts-preview-bar")
        self.play_btn = Gtk.Button()
        self.play_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic", Gtk.IconSize.BUTTON)
        self.play_btn.add(self.play_icon)
        self.play_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.play_btn.set_tooltip_text("Reproducir / pausar (espacio). Flechas o rueda: ±intervalo; con Ctrl, ±1 s")
        self.play_btn.connect("clicked", lambda *_: self.on_toggle_play() if self.on_toggle_play else None)
        bar.pack_start(self.play_btn, False, False, 0)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.1)
        self.scale.set_draw_value(False)
        self.scale.set_hexpand(True)
        self.scale.connect("button-press-event", self._scrub_begin)
        self.scale.connect("button-release-event", self._scrub_end)
        self.scale.connect("value-changed", self._on_value)
        bar.pack_start(self.scale, True, True, 0)
        self.time_label = Gtk.Label(label="")
        self.time_label.set_width_chars(15)
        self.time_label.set_xalign(1.0)
        bar.pack_start(self.time_label, False, False, 0)
        self.close_btn = Gtk.Button()
        self.close_btn.add(Gtk.Image.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON))
        self.close_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.close_btn.set_tooltip_text("Cerrar (Esc)")
        self.close_btn.connect("clicked", lambda *_: self.on_close() if self.on_close else None)
        bar.pack_start(self.close_btn, False, False, 0)
        self.pack_start(bar, False, False, 0)

        self.set_halign(Gtk.Align.FILL)
        self.set_valign(Gtk.Align.FILL)
        self.show_all()
        self.hide()
        self.set_no_show_all(True)

    @property
    def dragging(self):
        return self._dragging

    def show_still(self, pixbuf, t, label):
        self.update_still(pixbuf, t, label)
        self.stack.set_visible_child_name("still")
        self.set_playing(False)

    def update_still(self, pixbuf, t, label):
        """Otro fotograma (o el mismo a más resolución): el recuadro se reencaja con él."""
        self.still.set_frame(pixbuf, t, label)
        self.frame.queue_draw()

    def show_video(self, aspect=None):
        self.video_aspect = aspect
        self.stack.set_visible_child_name("video")
        self.frame.queue_draw()

    def attach_video(self, widget):
        widget.add_events(Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.SMOOTH_SCROLL_MASK)
        self.video_box.add(widget)
        widget.show()

    def set_marked(self, on):
        """Recuadro rojo sí/no. Devuelve True si ha cambiado."""
        on = bool(on)
        if on == self.marked:
            return False
        self.marked = on
        self.frame.queue_draw()
        return True

    def image_rect(self):
        """Dónde se ve la imagen dentro de la capa: el fotograma encajado (modo fijo) o el vídeo encajado por su
        aspecto (como hace el sink); si no se sabe, toda la capa."""
        alloc = self.frame.get_allocation()
        aw, ah = max(1, alloc.width), max(1, alloc.height)
        if self.stack.get_visible_child_name() == "still":
            pb = self.still.pixbuf
            if pb is not None:
                return fit_rect(pb.get_width(), pb.get_height(), aw, ah)
        elif self.video_aspect:
            return fit_rect(self.video_aspect, 1.0, aw, ah)
        return 0.0, 0.0, float(aw), float(ah)

    def _draw_mark(self, widget, cr):
        if not self.marked:
            return False
        x, y, w, h = self.image_rect()
        lw = self.MARK_W
        cr.set_source_rgb(0.93, 0.16, 0.16)   # el rojo de las teselas marcadas
        cr.set_line_width(lw)
        cr.rectangle(x + lw / 2.0, y + lw / 2.0, max(lw, w - lw), max(lw, h - lw))
        cr.stroke()
        return False

    def set_duration(self, d):
        self.duration = max(0.1, float(d))
        self._updating = True
        self.scale.set_range(0, self.duration)
        self._updating = False

    def set_position(self, s):
        if self._dragging:
            return
        self._updating = True
        self.scale.set_value(max(0.0, min(self.duration, float(s))))
        self._updating = False
        self._label(s)

    def _label(self, s):
        self.time_label.set_text("%s / %s" % (fmt_time(s), fmt_time(self.duration)))

    def set_playing(self, playing):
        self.play_icon.set_from_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic",
                                          Gtk.IconSize.BUTTON)

    def _scrub_begin(self, widget, event):
        self._dragging = True
        return False

    def _scrub_end(self, widget, event):
        self._dragging = False
        if self.on_seek:
            self.on_seek(self.scale.get_value())
        return False

    def _on_value(self, scale):
        if self._updating:
            return
        self._label(scale.get_value())
        if self.on_seek:
            self.on_seek(scale.get_value())


class Toast(Gtk.Revealer):
    """Aviso superpuesto abajo en el centro. Verde = operación correcta, se oculta a los 3 s. Rojo = error,
    permanente, con el texto seleccionable y botones Copiar y cerrar."""

    OK_MS = 3000

    def __init__(self):
        super(Toast, self).__init__()
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.set_transition_duration(180)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.END)
        self.set_margin_bottom(28)
        self._hide_id = None
        self.is_error = False
        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.box.get_style_context().add_class("ts-toast")
        self.label = Gtk.Label(label="")
        self.label.set_line_wrap(True)
        self.label.set_max_width_chars(90)
        self.label.set_xalign(0.0)
        self.box.pack_start(self.label, True, True, 0)
        self.copy_btn = Gtk.Button(label="Copiar")
        self.copy_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.copy_btn.set_tooltip_text("Copiar el mensaje al portapapeles")
        self.copy_btn.connect("clicked", self._copy)
        self.box.pack_start(self.copy_btn, False, False, 0)
        self.close_btn = Gtk.Button()
        self.close_btn.add(Gtk.Image.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON))
        self.close_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.close_btn.set_tooltip_text("Cerrar (Esc)")
        self.close_btn.connect("clicked", lambda *_: self.dismiss())
        self.box.pack_start(self.close_btn, False, False, 0)
        self.add(self.box)
        self.box.show_all()
        self.set_reveal_child(False)
        self.show()

    def _show(self, text, error):
        if self._hide_id:
            GLib.source_remove(self._hide_id)
            self._hide_id = None
        self.is_error = error
        ctx = self.box.get_style_context()
        ctx.remove_class("ts-toast-ok")
        ctx.remove_class("ts-toast-err")
        ctx.add_class("ts-toast-err" if error else "ts-toast-ok")
        self.label.set_text(text)
        self.label.set_selectable(error)
        self.copy_btn.set_visible(error)
        self.close_btn.set_visible(error)
        self.set_reveal_child(True)
        if not error:
            self._hide_id = GLib.timeout_add(self.OK_MS, self._timeout)
        log("toast %s: %s" % ("error" if error else "ok", text.replace("\n", " | ")))

    def show_ok(self, text):
        self._show(text, False)

    def show_error(self, text):
        self._show(text, True)

    def _timeout(self):
        self._hide_id = None
        self.set_reveal_child(False)
        return False

    def dismiss(self):
        if self._hide_id:
            GLib.source_remove(self._hide_id)
            self._hide_id = None
        self.set_reveal_child(False)

    @property
    def visible_error(self):
        return self.is_error and self.get_reveal_child()

    def _copy(self, *_):
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(self.label.get_text(), -1)


# ----------------------------------------------------------------------------------------------
# selección → segmentos → proyecto de LosslessCut
# ----------------------------------------------------------------------------------------------
class Selection(object):
    """Lo seleccionado son SEGMENTOS de tiempo [inicio, fin), no teselas. Una tesela t del intervalo S se
    muestra marcada si su tramo [t, t+S) solapa algún segmento. Los segmentos guardados NO dependen de la
    rejilla: con 15–25 s y teselas de 10 s se ven marcadas 10 y 20 (la rejilla gruesa sólo puede mostrar
    10–30), pero el segmento sigue siendo 15–25 y al volver a 5 s recupera sus bordes. Marcar o desmarcar
    una tesela suma o resta su tramo [t, t+S) a los segmentos."""

    def __init__(self, segments=None, order=None):
        self.segments = self._normalize(segments or [])
        self.order = [float(k) for k in (order or [])]   # orden de corte: instante de inicio de cada tramo

    @staticmethod
    def _normalize(segs):
        out = []
        for a, b in sorted((float(a), float(b)) for a, b in segs):
            if b <= a:
                continue
            if out and a <= out[-1][1]:
                out[-1] = (out[-1][0], max(out[-1][1], b))
            else:
                out.append((a, b))
        return out

    def copy(self):
        return Selection(list(self.segments), list(self.order))

    def ordered(self):
        """Tramos en el orden de corte: primero los que tienen posición asignada (identificados por el instante
        de inicio con que se ordenaron), después los demás por tiempo. Las posiciones sobreviven a los retoques:
        un tramo que crece o se funde con otro sigue conteniendo su instante; uno borrado desaparece sin más."""
        segs = list(self.segments)
        out, used = [], set()
        for key in self.order:
            for i, (a, b) in enumerate(segs):
                if i not in used and a <= key < b:
                    out.append((a, b))
                    used.add(i)
                    break
        out.extend(s for i, s in enumerate(segs) if i not in used)
        return out

    def set_order(self, segs):
        self.order = [float(a) for a, _b in segs]

    def __bool__(self):
        return bool(self.segments)

    __nonzero__ = __bool__

    def add(self, a, b):
        self.segments = self._normalize(self.segments + [(a, b)])

    def remove(self, a, b):
        out = []
        for x, y in self.segments:
            if y <= a or x >= b:
                out.append((x, y))
                continue
            if x < a:
                out.append((x, a))
            if y > b:
                out.append((b, y))
        self.segments = self._normalize(out)

    def contains(self, s):
        """¿Cae el instante s dentro de algún segmento [a, b)?"""
        s = float(s)
        i = bisect.bisect_right(self.segments, (s, float("inf"))) - 1
        return i >= 0 and self.segments[i][0] <= s < self.segments[i][1]

    @staticmethod
    def tile_range(t, interval, duration):
        return float(t), min(float(t + interval), float(duration))

    def tiles(self, timestamps, interval, duration):
        """Teselas marcadas para esta rejilla. Instantes y tramos están ordenados: una sola pasada."""
        out = set()
        segs = self.segments
        if not segs:
            return out
        j, m, S, D = 0, len(segs), float(interval), float(duration)
        for t in timestamps:
            a = float(t)
            b = min(a + S, D)
            while j < m and segs[j][1] <= a:
                j += 1
            if j < m and segs[j][0] < b:
                out.add(t)
        return out

    def to_json(self):
        d = {"segments": [[_num(a), _num(b)] for a, b in self.segments]}
        ordered = self.ordered()
        if ordered != self.segments:
            d["order"] = [_num(a) for a, _b in ordered]
        return d

    @classmethod
    def from_json(cls, data):
        if isinstance(data, dict):
            return cls([(a, b) for a, b in data.get("segments") or []], data.get("order") or [])
        if isinstance(data, list):   # formato antiguo: lista de teselas sueltas; se asumen tramos de 5 s
            return cls([(t, t + 5) for t in data])
        return cls()


def llc_project_path(video_path):
    video_path = pathlib.Path(video_path)
    return video_path.with_name(video_path.stem + "-proj.llc")


def _num(x):
    x = float(x)
    return int(x) if x.is_integer() else round(x, 3)


def write_llc_project(video_path, segments):
    """Proyecto de LosslessCut junto al vídeo (<nombre>-proj.llc), que LosslessCut carga solo al abrir el
    vídeo. Se escribe como JSON: es JSON5 válido (LosslessCut actual, esquema v2) y también YAML válido
    (versiones antiguas, que guardaban YAML)."""
    video_path = pathlib.Path(video_path)
    data = {
        "version": 2,
        "mediaFileName": video_path.name,
        "cutSegments": [{"start": _num(a), "end": _num(b), "name": ""} for a, b in segments],
    }
    proj = llc_project_path(video_path)
    proj.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return proj


# ----------------------------------------------------------------------------------------------
# cortar y unir los segmentos seleccionados (ffmpeg, demuxer concat con inpoint/outpoint)
# ----------------------------------------------------------------------------------------------
COPY_CONTAINERS = (".mp4", ".mkv", ".mov", ".m4v", ".webm")
CONTAINER_FORMAT = {".mp4": "mp4", ".m4v": "mp4", ".mov": "mov", ".mkv": "matroska", ".webm": "webm"}


def cut_output_path(video_path):
    video_path = pathlib.Path(video_path)
    ext = video_path.suffix.lower() if video_path.suffix.lower() in COPY_CONTAINERS else ".mkv"
    return video_path.with_name(video_path.stem + "-cortado" + ext)


def full_output_path(video_path):
    """Nombre «cortado» para el vídeo entero sin tocarlo (botón Full): mismo contenedor y extensión, y si ya existe
    uno con ese nombre, -cortado-2, -cortado-3…"""
    video_path = pathlib.Path(video_path)
    k = 1
    while True:
        cand = video_path.with_name("%s-cortado%s%s" % (video_path.stem, "" if k == 1 else "-%d" % k, video_path.suffix))
        if not cand.exists():
            return cand
        k += 1


def keyframes_near(path, times, window):
    """Instantes (s) de los keyframes de vídeo en las ventanas [t-window, t+window] de cada t. Sólo
    demux (ffprobe -show_packets con -read_intervals): no decodifica ni lee el fichero entero."""
    if not times:
        return []
    if window is None:
        intervals = []
    else:
        intervals = ["%.3f%%%.3f" % (max(0.0, t - window), t + window) for t in sorted(set(times))]
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,dts_time,flags",
           "-of", "csv=p=0"]
    if intervals:
        cmd += ["-read_intervals", ",".join(intervals)]
    cmd.append(str(path))
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=FF_ENV, timeout=600).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    kfs = set()
    for line in out.decode("utf-8", "replace").splitlines():
        parts = line.split(",")
        if len(parts) >= 3 and "K" in parts[2]:
            t = parts[0] if parts[0] not in ("", "N/A") else parts[1]
            try:
                kfs.add(float(t))
            except ValueError:
                pass
    return sorted(kfs)


def expand_to_keyframes(segments, path, duration, gop=None):
    """Mueve cada borde HACIA FUERA al keyframe más cercano (inicio: el anterior o igual; fin: el siguiente o
    igual) para que la copia de streams no pierda nada de lo seleccionado. Ventanas de búsqueda crecientes
    y, como último recurso, un barrido completo."""
    base = max(20.0, 2.0 * (gop or 10.0))
    starts = [a for a, _ in segments]
    ends = [b for _, b in segments]
    kfs = []
    for window in (base, base * 6, None):
        kfs = keyframes_near(path, starts + ends, window)
        ok = True
        for a in starts:
            if a > 0.05 and not any(k <= a + 1e-3 for k in kfs) and (window is None or a - window > 0):
                ok = False
        for b in ends:
            if b < duration - 0.05 and not any(k >= b - 1e-3 for k in kfs) and (window is None or b + window < duration):
                ok = False
        if ok:
            break
    out = []
    for a, b in segments:
        before = [k for k in kfs if k <= a + 1e-3]
        after = [k for k in kfs if k >= b - 1e-3]
        na = max(before) if before else 0.0
        nb = min(after) if after else float(duration)
        na, nb = float(na), min(float(nb), float(duration))
        # Se respeta el orden de corte (el del panel de segmentos): NO se ordena por tiempo. Sólo se funden
        # dos tramos consecutivos en el corte cuando, al crecer hasta los keyframes, el segundo empieza
        # dentro del primero (o justo donde acaba); así no se repiten fotogramas entre ellos.
        if out and out[-1][0] <= na <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], nb))
        else:
            out.append((na, nb))
    return out


def write_concat_list(list_path, video_path, segments):
    quoted = str(video_path).replace("'", "'\\''")
    lines = ["ffconcat version 1.0"]
    for a, b in segments:
        lines += ["file '%s'" % quoted, "inpoint %.3f" % a, "outpoint %.3f" % b]
    pathlib.Path(list_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _out_format(out_path):
    """El resultado se escribe en <salida>.part y se renombra al acabar, así que el formato va explícito."""
    return ["-f", CONTAINER_FORMAT.get(pathlib.Path(out_path).suffix.lower(), "matroska")]


def cut_command_copy(list_path, out_path, tmp_path):
    """Sin pérdida: demuxer concat con inpoint/outpoint (en keyframes) y copia de streams."""
    return FFMPEG + ["-progress", "pipe:1", "-nostats", "-f", "concat", "-safe", "0", "-i", str(list_path),
                     "-map", "0:v:0", "-map", "0:a?", "-ignore_unknown", "-c", "copy", "-avoid_negative_ts", "make_zero"] + \
        _out_format(out_path) + ["-y", str(tmp_path)]


def cut_command_exact(video_path, segments, out_path, tmp_path, has_audio):
    """Exacto al fotograma: una entrada por segmento con seek exacto (-ss/-t antes de -i: sólo se decodifica
    cada tramo desde su keyframe) y filtro concat; recodifica. El demuxer concat NO sirve aquí: al
    recodificar no descarta los fotogramas entre el keyframe y el inpoint."""
    cmd = FFMPEG + ["-progress", "pipe:1", "-nostats"]
    for a, b in segments:
        cmd += ["-ss", "%.3f" % a, "-t", "%.3f" % (b - a), "-i", str(video_path)]
    n = len(segments)
    if has_audio:
        fc = "".join("[%d:v:0][%d:a:0]" % (i, i) for i in range(n)) + "concat=n=%d:v=1:a=1[v][a]" % n
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        fc = "".join("[%d:v:0]" % i for i in range(n)) + "concat=n=%d:v=1:a=0[v]" % n
        maps = ["-map", "[v]"]
    cmd += ["-filter_complex", fc] + maps + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "160k"]
    return cmd + _out_format(out_path) + ["-y", str(tmp_path)]


# ----------------------------------------------------------------------------------------------
# documento = un vídeo abierto (sondeo, caché, selección, posición de scroll)
# ----------------------------------------------------------------------------------------------
PANEL_INDENT = 14                      # píxeles de sangría por nivel del árbol del panel
EYE_OPEN, EYE_CLOSED = "view-reveal-symbolic", "view-conceal-symbolic"


def icon_or_text(name, text):
    """Imagen del tema de iconos si lo tiene; si no (temas viejos), un texto."""
    if Gtk.IconTheme.get_default().has_icon(name):
        return Gtk.Image.new_from_icon_name(name, Gtk.IconSize.MENU)
    return Gtk.Label(label=text)


def sub_markup(text):
    """Líneas secundarias del panel (ruta, duración…): letra pequeña para que quepan tres líneas por fila."""
    return '<span size="x-small">%s</span>' % GLib.markup_escape_text(text)


def short_path(d):
    """Carpeta con el home abreviado a ~."""
    d, home = str(d), str(HOME)
    if d == home:
        return "~"
    if d.startswith(home + os.sep):
        return "~" + d[len(home):]
    return d


class StateIcon(Gtk.DrawingArea):
    """Icono de cada fila del panel, a la derecha de las líneas 2.ª y 3.ª: un mosaico de 4×4 cuadritos = dieciseisavos
    de las miniaturas del vídeo que ya están en caché para el intervalo actual (negros, se van llenando de arriba
    abajo como la sábana); los que faltan sólo se pintan, en gris claro, mientras se está generando. Sin nada en
    caché y sin generar no se pinta nada. Aspa roja si el vídeo no se puede abrir o no da ningún fotograma (el
    texto del error va en el tooltip). Sólo se repinta cuando cambia de cuadrito, no por cada captura. Ocupa el
    alto que le den (el de las dos líneas) y se dibuja cuadrado y centrado."""
    GRID = 4
    CELLS = GRID * GRID
    GAP = 1.0
    SIZE = 19             # ancho mínimo: 4 cuadritos de 4 px y 3 huecos

    def __init__(self):
        super(StateIcon, self).__init__()
        self.quarters = 0     # cuadritos llenos (0..CELLS); el nombre viene de cuando eran cuartos
        self.working = False
        self.error = None
        self.set_size_request(self.SIZE, self.SIZE)
        self.set_valign(Gtk.Align.FILL)
        self.set_tooltip_text(self.describe())
        self.connect("draw", self.on_draw)

    def set_progress(self, done, total, working):
        """Devuelve True si cambia lo que se ve."""
        n = self.CELLS
        if total <= 0:
            q = 0
        elif done >= total:
            q = n
        else:
            q = min(n - 1, int(n * done / float(total)))
        working = bool(working) and q < n
        if (q, working, None) == (self.quarters, self.working, self.error):
            return False
        self.quarters, self.working, self.error = q, working, None
        self.set_tooltip_text(self.describe())
        self.queue_draw()
        return True

    def set_error(self, text):
        """Vídeo que no se puede abrir o del que ffmpeg no saca ningún fotograma: aspa roja. True si cambia."""
        if text == self.error:
            return False
        self.error = text
        self.set_tooltip_text(self.describe())
        self.queue_draw()
        return True

    def describe(self):
        if self.error is not None:
            return self.error
        pct = int(100 * self.quarters / self.CELLS)
        if self.quarters == self.CELLS:
            return "Miniaturas generadas"
        if self.working:
            return "Generando miniaturas… (%d %% o más)" % pct
        return "Miniaturas pendientes" if pct == 0 else "Miniaturas: %d %% o más en caché" % pct

    def state_name(self):
        if self.error is not None:
            return "error"
        pct = int(100 * self.quarters / self.CELLS)
        if self.quarters == self.CELLS:
            return "listas"
        if self.working:
            return "generando %d %%" % pct
        return "pendientes" if pct == 0 else "%d %%" % pct

    def on_draw(self, widget, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        s = float(min(w, h))                       # cuadrado y centrado en lo asignado
        cx, cy = w / 2.0, h / 2.0
        if self.error is not None:   # aspa roja, como las teselas sin fotograma del mosaico
            r = min(s / 2.0 - 1.5, 7.0)
            cr.set_source_rgb(0.93, 0.16, 0.16)
            cr.set_line_width(2.4)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.move_to(cx - r, cy - r)
            cr.line_to(cx + r, cy + r)
            cr.move_to(cx + r, cy - r)
            cr.line_to(cx - r, cy + r)
            cr.stroke()
            return False
        c = self.get_style_context().get_color(self.get_state_flags())
        n, g = self.GRID, self.GAP
        cell = math.floor((s - (n - 1) * g) / n)   # cuadritos de tamaño entero: bordes nítidos
        if cell < 2:
            return False
        span = n * cell + (n - 1) * g
        ox, oy = math.floor(cx - span / 2.0), math.floor(cy - span / 2.0)
        for i in range(self.CELLS):
            if i < self.quarters:
                cr.set_source_rgba(c.red, c.green, c.blue, c.alpha)          # hecho: color del texto (negro)
            elif self.working:
                cr.set_source_rgba(c.red, c.green, c.blue, c.alpha * 0.18)   # por hacer y generándose: gris claro
            else:
                continue                                                     # por hacer y parado: nada
            r, k = divmod(i, n)                                              # se llena por filas, de arriba abajo
            cr.rectangle(ox + k * (cell + g), oy + r * (cell + g), cell, cell)
            cr.fill()
        return False


class SegPanel(Gtk.Box):
    """Panel derecho (plegable): los tramos seleccionados en su orden de corte, cada uno con su primer y último
    fotograma, inicio y duración. Toda la tarjeta (menos ×) se arrastra para reordenar: las demás se deslizan para
    hacerle hueco y la arrastrada queda atenuada donde va a caer (el orden lo usan Cortar y LLC); × quita el tramo;
    clic en la tarjeta lleva el mosaico a ese instante. Cabecera: cuántos hay y la duración total. Sólo se
    reconstruye cuando cambian los tramos (agrupando lo que llegue en 120 ms) y las miniaturas se decodifican una
    vez a tamaño de tarjeta. Las tarjetas van en un Gtk.Fixed con la posición vertical animada por tick (un
    ListBox no puede deslizar sus filas)."""
    THUMB_H = 44
    WIDTH = 280
    GAP = 2            # separación vertical entre tarjetas
    ANIM_MS = 180      # deslizamiento de las tarjetas al hacer hueco
    TARGETS = [Gtk.TargetEntry.new("THUMBSHEET_SEGMENT", Gtk.TargetFlags.SAME_APP, 0)]

    def __init__(self, on_reorder, on_delete, on_goto):
        super(SegPanel, self).__init__(orientation=Gtk.Orientation.VERTICAL)
        self.on_reorder, self.on_delete, self.on_goto = on_reorder, on_delete, on_goto
        self.set_size_request(self.WIDTH, -1)
        self.header = Gtk.Label(label="Sin segmentos")
        self.header.set_xalign(0.0)
        hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        hb.get_style_context().add_class("ts-panel-header")
        hb.pack_start(self.header, True, True, 0)
        self.pack_start(hb, False, False, 0)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.fixed = Gtk.Fixed()
        self.fixed.connect("size-allocate", self._fixed_allocated)
        sw.add(self.fixed)
        # el hueco bajo la última tarjeta también acepta soltar (si no, soltar ahí cancelaría el reorden ya visible)
        sw.drag_dest_set(Gtk.DestDefaults.MOTION | Gtk.DestDefaults.DROP, self.TARGETS, Gdk.DragAction.MOVE)
        sw.connect("drag-motion", self._drag_motion_bg)
        sw.connect("drag-data-received", self._drag_received)
        self.sw = sw
        self.pack_start(sw, True, True, 0)
        self.cards = []           # tarjetas en su orden visual (cada una: seg, y, target_y, anim_from, anim_t0)
        self.pitch = self.THUMB_H + 8
        self._tick = None         # tick callback de la animación mientras alguna tarjeta no está en su sitio
        self._pix = {}            # (caché, t) -> pixbuf a tamaño de tarjeta
        self._wanted = set()      # instantes sin miniatura aún (se completan cuando el generador las saque)
        self._params = None
        self._sig = None
        self._id = None
        self._segs = []
        self._drag_card = None    # tarjeta que se está arrastrando (las demás se apartan en vivo)
        self._drag_orig = None    # tarjetas en el orden de partida, por si se cancela
        self._dropped = False
        self._press_card = None   # tarjeta con el botón pulsado y aún sin arrastrar (soltar ahí = clic = ir)

    def refresh(self, doc, segs, ts, S, dur, force=False):
        self._params = (doc, list(segs), ts, S, dur)
        if force:
            self._sig = None
        if self._id is None:
            self._id = GLib.timeout_add(120, self._rebuild)

    def tile_ready(self, t):
        if t in self._wanted and self._params is not None:
            self.refresh(*self._params, force=True)
        return False

    def tiles_ready(self, ts):
        if self._wanted and self._params is not None and self._wanted.intersection(ts):
            self.refresh(*self._params, force=True)

    def _rebuild(self):
        if self._drag_card is not None:   # mientras se arrastra no se recrean las tarjetas: se reintenta en 120 ms
            return True
        self._id = None
        doc, segs, ts, S, dur = self._params if self._params else (None, [], [], 1, 0.0)
        sig = (id(doc), tuple(segs))
        if sig == self._sig:
            return False
        self._sig = sig
        self._segs = list(segs)
        for card in self.cards:
            self.fixed.remove(card)
        self.cards = []
        self._wanted = set()
        total = sum(b - a for a, b in segs)
        self.header.set_text("Sin segmentos" if not segs else "%d segmento%s · %s" % (
            len(segs), "" if len(segs) == 1 else "s", fmt_time(total)))
        width = self.fixed.get_allocated_width()
        for k, (a, b) in enumerate(segs):
            card = self._make_card(doc, k, a, b, ts, S)
            card.set_size_request(width if width > 1 else self.WIDTH, -1)
            self.cards.append(card)
            self.fixed.put(card, 0, 0)
        self.fixed.show_all()
        if self.cards:
            self.pitch = max(self.THUMB_H + 8, self.cards[0].get_preferred_height()[1] + self.GAP)
        self._set_targets(animate=False)
        if DEBUG:
            self._geom_tries = 0
            GLib.timeout_add(60, self._log_geometry)
        return False

    def _fixed_allocated(self, fixed, alloc):
        # Gtk.Fixed da a cada hija su tamaño mínimo: a las tarjetas se les pide el ancho de la pila. Al crearlas se
        # usa el ancho ya asignado (_rebuild); si cambia después se corrige en un idle, porque un queue_resize
        # desde dentro del size-allocate no provoca otra pasada de layout (GTK limpia la marca al salir).
        if any(c.get_size_request()[0] != alloc.width for c in self.cards):
            GLib.idle_add(self._fit_width, alloc.width)

    def _fit_width(self, width):
        if self.fixed.get_allocated_width() == width:
            for card in self.cards:
                if card.get_size_request()[0] != width:
                    card.set_size_request(width, -1)
        return False

    def _thumb(self, doc, t):
        if doc is None or doc.cache_dir is None:
            return None
        key = (str(doc.cache_dir), t)
        pb = self._pix.get(key)
        if pb is not None:
            return pb
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(doc.cache_dir / ("%d.jpg" % t)), 2 * self.THUMB_H,
                                                         self.THUMB_H, True)
        except GLib.Error:
            self._wanted.add(t)
            return None
        if len(self._pix) > 400:
            self._pix.clear()
        self._pix[key] = pb
        return pb

    def _make_card(self, doc, k, a, b, ts, S):
        card = Gtk.EventBox()
        card.seg = (a, b)
        card.y = card.target_y = 0.0
        card.anim_from, card.anim_t0 = 0.0, None
        card.get_style_context().add_class("ts-card")
        card.set_tooltip_text("%s – %s\nArrastra para cambiar el orden de corte · clic: ir ahí en el mosaico"
                              % (fmt_time(a), fmt_time(b)))
        card.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, self.TARGETS, Gdk.DragAction.MOVE)
        card.connect("drag-begin", self._drag_begin)
        card.connect("drag-data-get", self._drag_data_get)
        card.connect("drag-end", self._drag_end)
        card.drag_dest_set(Gtk.DestDefaults.MOTION | Gtk.DestDefaults.DROP, self.TARGETS, Gdk.DragAction.MOVE)
        card.connect("drag-motion", self._drag_motion)
        card.connect("drag-data-received", self._drag_received)
        card.connect("button-press-event", self._card_pressed)
        card.connect("button-release-event", self._card_released)
        card.connect("enter-notify-event", self._card_enter)
        card.connect("leave-notify-event", self._card_leave)
        card.connect("realize", self._card_realized)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_start(6)
        box.set_margin_end(2)
        box.set_margin_top(2)
        box.set_margin_bottom(2)
        tiles = [t for t in ts if t < b and t + S > a]
        edges = ([tiles[0]] + ([tiles[-1]] if len(tiles) > 1 else [])) if tiles else []
        for t in edges:
            pb = self._thumb(doc, t)
            if pb is not None:
                img = Gtk.Image.new_from_pixbuf(pb)
            else:
                img = Gtk.Frame()
                img.set_size_request(int(self.THUMB_H * 16 / 9), self.THUMB_H)
            box.pack_start(img, False, False, 0)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        text.set_valign(Gtk.Align.CENTER)
        l1 = Gtk.Label()
        l1.set_xalign(0.0)
        l1.set_markup("<b>%d</b> · %s" % (k + 1, fmt_time(a)))
        l2 = Gtk.Label()
        l2.set_xalign(0.0)
        l2.get_style_context().add_class("dim-label")
        l2.set_markup(sub_markup("dura %s" % fmt_time(b - a)))
        text.pack_start(l1, False, False, 0)
        text.pack_start(l2, False, False, 0)
        box.pack_start(text, True, True, 0)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_valign(Gtk.Align.CENTER)
        close.set_tooltip_text("Quitar este tramo de la selección")
        close.connect("clicked", lambda *_: self.on_delete(a, b))
        box.pack_end(close, False, False, 0)
        card.add(box)
        card.close = close
        return card

    # --- colocación y animación ---
    def _set_targets(self, animate):
        """Cada tarjeta a la altura de su posición en la lista; con animate se desliza desde donde esté."""
        for k, card in enumerate(self.cards):
            ty = float(k * self.pitch)
            if card.target_y == ty and (card.y == ty or animate):
                continue
            card.target_y = ty
            if animate:
                card.anim_from, card.anim_t0 = card.y, None   # el instante de salida lo pone el primer tick
            else:
                card.y = ty
                self.fixed.move(card, 0, int(ty))
        if animate and self._tick is None and any(c.y != c.target_y for c in self.cards):
            self._anim_frames, self._anim_t0 = 0, time.monotonic()
            self._tick = self.add_tick_callback(self._on_tick)

    def _on_tick(self, widget, clock):
        now = clock.get_frame_time() / 1e6
        moving = False
        for card in self.cards:
            if card.y == card.target_y:
                continue
            if card.anim_t0 is None:
                card.anim_t0 = now
            p = min(1.0, (now - card.anim_t0) / (self.ANIM_MS / 1000.0))
            e = 1.0 - (1.0 - p) ** 3                       # ease-out: arranca rápido y frena al llegar
            ny = card.target_y if p >= 1.0 else card.anim_from + (card.target_y - card.anim_from) * e
            if int(round(ny)) != int(round(card.y)) or p >= 1.0:
                self.fixed.move(card, 0, int(round(ny)))
            card.y = ny
            if p < 1.0:
                moving = True
        self._anim_frames += 1
        if moving:
            return GLib.SOURCE_CONTINUE
        self._tick = None
        if DEBUG:
            log("seganim: %d fotogramas en %d ms" % (self._anim_frames, 1000 * (time.monotonic() - self._anim_t0)))
        return GLib.SOURCE_REMOVE

    # --- tarjeta: hover, cursor y clic ---
    def _card_realized(self, card):
        cur = Gdk.Cursor.new_from_name(card.get_display(), "grab")
        if cur is not None:
            card.get_window().set_cursor(cur)

    def _card_enter(self, card, event):
        if event.detail != Gdk.NotifyType.INFERIOR:
            card.set_state_flags(Gtk.StateFlags.PRELIGHT, False)
        return False

    def _card_leave(self, card, event):
        if event.detail != Gdk.NotifyType.INFERIOR:
            card.unset_state_flags(Gtk.StateFlags.PRELIGHT)
        return False

    def _card_pressed(self, card, event):
        if DEBUG:
            log("segcard: pulsada %s botón %d tipo %s" % (fmt_time(card.seg[0]), event.button, event.type.value_nick))
        if event.button == 1:
            self._press_card = card      # si la pulsación acaba en arrastre, _drag_begin lo anula: soltar no es clic
        return False

    def _card_released(self, card, event):
        pressed, self._press_card = self._press_card, None
        if DEBUG:
            log("segcard: soltada %s botón %d pulsada=%s arrastre=%s" % (fmt_time(card.seg[0]), event.button,
                                                                          pressed is card, self._drag_card is not None))
        if event.button == 1 and pressed is card and self._drag_card is None:
            self.on_goto(card.seg[0])
        return False

    # --- arrastrar y soltar para reordenar ---
    # La tarjeta arrastrada se queda en la lista, atenuada, y va cambiando de sitio según dónde esté el cursor;
    # las demás se deslizan para hacerle hueco, así se ve dónde va a quedar. La decisión se toma sobre la rejilla
    # de posiciones finales (no sobre las tarjetas a medio deslizar, que harían oscilar el orden): la casilla
    # bajo el cursor y su mitad. Al soltar se guarda el orden que se ve; si se cancela (Esc, soltar fuera) vuelve
    # al de partida.
    def _drag_begin(self, card, context):
        alloc = card.get_allocation()
        win = card.get_window()
        pos = win.get_device_position(context.get_device()) if win is not None else None
        hx, hy = (pos[1], pos[2]) if pos else (alloc.width // 2, alloc.height // 2)
        if DEBUG:
            log("segdrag: icono %dx%d, agarre %d,%d" % (alloc.width, alloc.height, hx, hy))
        # la imagen del icono son los píxeles de la tarjeta tal como se ve, con el punto de agarre bajo el cursor
        pb = Gdk.pixbuf_get_from_window(win, 0, 0, max(1, alloc.width), max(1, alloc.height)) if win else None
        if pb is not None:
            Gtk.drag_set_icon_pixbuf(context, pb, hx, hy)
        self._drag_card = card
        self._drag_orig = list(self.cards)
        self._dropped = False
        self._press_card = None
        card.get_style_context().add_class("ts-dragging")

    def _drag_data_get(self, card, context, data, info, time_):
        idx = self.cards.index(card) if card in self.cards else 0
        data.set(data.get_target(), 8, str(idx).encode("ascii"))

    def _place(self, dst):
        """Coloca la tarjeta arrastrada en la posición dst."""
        if self._drag_card not in self.cards:
            return
        cur = self.cards.index(self._drag_card)
        dst = max(0, min(len(self.cards) - 1, dst))
        if dst == cur:
            return
        self.cards.insert(dst, self.cards.pop(cur))
        self._set_targets(animate=True)
        if DEBUG:
            log("segdrag: posición %d de %d" % (dst + 1, len(self.cards)))

    def _motion_at(self, fy):
        """Cursor a la altura fy (coordenadas de la pila): casilla k y mitad; mitad superior = delante de esa
        tarjeta, inferior = detrás. Media casilla de histéresis: el orden sólo cambia al cruzar una mitad."""
        n = len(self.cards)
        if not n or self._drag_card is None:
            return
        cur = self.cards.index(self._drag_card)
        k = int(fy // self.pitch)
        frac = (fy - k * self.pitch) / float(self.pitch)
        if k < 0:
            dst = 0
        elif k >= n:
            dst = n - 1
        elif k == cur:
            return
        elif k < cur:
            dst = k if frac < 0.5 else k + 1
        else:
            dst = k if frac > 0.5 else k - 1
        self._place(dst)
        k = max(0, min(n - 1, k))
        self.sw.get_vadjustment().clamp_page(k * self.pitch, (k + 1) * self.pitch)   # una tarjeta a medio ver se descubre

    def _drag_motion(self, card, context, x, y, time_):
        if self._drag_card is None:
            return False
        pt = card.translate_coordinates(self.fixed, x, y)
        if pt:
            self._motion_at(pt[1])
        return True

    def _drag_motion_bg(self, sw, context, x, y, time_):
        if self._drag_card is None:
            return False
        pt = sw.translate_coordinates(self.fixed, x, y)
        if pt:
            self._motion_at(pt[1])
        return True

    def _drag_received(self, widget, context, x, y, data, info, time_):
        if self._drag_card is None:
            return
        self._dropped = True
        segs = [c.seg for c in self.cards]
        if segs != self._segs:
            self.on_reorder(segs)

    def _drag_end(self, card, context):
        card_, orig = self._drag_card, self._drag_orig
        self._drag_card = self._drag_orig = None
        if card_ is None:
            return
        card_.get_style_context().remove_class("ts-dragging")
        if not self._dropped and orig:
            self.cards = [c for c in orig if c in self.cards]
            self._set_targets(animate=True)

    def _log_geometry(self):
        top = self.get_toplevel()
        if any(c.get_allocation().width <= 1 for c in self.cards) and self._geom_tries < 20:
            self._geom_tries += 1      # GTK coloca las tarjetas en la fase de layout del siguiente fotograma: esperar
            return True

        def center(w):
            a = w.get_allocation()
            pt = w.translate_coordinates(top, 0, 0)
            return "%d,%d" % (pt[-2] + a.width // 2, pt[-1] + a.height // 2) if pt else "?"
        log("segpanel: %s · rows=%s · dels=%s" % (self.header.get_text(), ";".join(center(c) for c in self.cards),
                                                 ";".join(center(c.close) for c in self.cards)))
        return False


class Document(object):
    def __init__(self, path):
        self.path = pathlib.Path(path).resolve()
        self.info = None
        self.error = None                     # no se pudo abrir (sondeo): texto
        self.gen_error = None                 # ffmpeg no saca ningún fotograma: texto; fuera del segundo plano hasta reintentar
        self.cache_dir = None
        self.selection = Selection()
        self.scroll = 0.0
        self.bg_tried = None                  # intervalo para el que ya se intentó generar en segundo plano
        self.deleting = False                 # borrado en marcha (hilo aparte): fila insensible, no se genera
        self.row = None
        self.folder = None                    # carpeta del panel (raíz o subcarpeta) bajo la que está su fila
        self.hidden = False                   # oculto por el ojo de su carpeta (ya cortado): ni a la vista ni en 2.º plano
        self.lock = threading.Lock()

    def ensure_info(self):
        with self.lock:
            if self.info is not None or self.error is not None:
                return
            try:
                info = VideoInfo(self.path)
                cache_dir = CACHE_ROOT / info.key
                cache_dir.mkdir(parents=True, exist_ok=True)
                for leftover in cache_dir.glob(".*"):   # tramos/temporales de una ejecución interrumpida
                    if leftover.is_dir():
                        shutil.rmtree(str(leftover), ignore_errors=True)
                    else:
                        try:
                            leftover.unlink()
                        except OSError:
                            pass
            except Exception as e:  # noqa: BLE001
                # OSError/RuntimeError normalmente; cualquier otra cosa (salida rara de ffprobe, caché que no se
                # puede crear…) también deja el vídeo en error: el hilo de sondeo no puede morir (ver _busy)
                self.error = short_error(str(e), "no se pudo abrir")
                return
            try:
                self.selection = Selection.from_json(json.loads((cache_dir / "selection.json").read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                self.selection = Selection()
            self.cache_dir = cache_dir
            self.info = info

    def save_selection(self):
        if not self.cache_dir:
            return
        try:
            f = self.cache_dir / "selection.json"
            if self.selection:
                f.write_text(json.dumps(self.selection.to_json()), encoding="utf-8")
            elif f.exists():
                f.unlink()
        except OSError:
            pass

    @property
    def subtitle(self):
        if self.error:
            return "no se pudo abrir"
        if not self.info:
            return "analizando…"
        return "%s · %dx%d · %s" % (fmt_time(self.info.duration), self.info.width, self.info.height,
                                    human_size(self.info.size))


class Folder(object):
    """Carpeta del panel. Raíz (`root` es None): una que se añadió (argumento, botón de carpeta, arrastre) o la del
    vídeo que se añadió suelto; su fila lleva el ojo (ocultar los vídeos ya cortados) y la vigilancia (cargar los
    vídeos que aparezcan), y los dos valen para todo su árbol. Subcarpeta (`root` = su raíz): sólo se ve mientras le
    quede algún vídeo a la vista; su fila dice la ruta relativa a la raíz. Las carpetas sin vídeos no tienen fila."""
    def __init__(self, path, root=None, order=0):
        self.path = path
        self.root = root
        self.order = order                    # raíz: orden de llegada; con ella se ordena el panel y no cambia
        parts = path.relative_to(root.path).parts if root is not None else ()
        self.rel = ([natural_key(p) for p in parts], "/".join(parts))
        self.docs = []                        # vídeos directamente en ella, por orden de llegada
        self.seq = 0                          # vídeos que han entrado en ella (su clave de orden: el de llegada)
        self.subs = []                        # raíz: subcarpetas (Folder) en orden natural por ruta relativa
        self.hide_done = False                # ojo activo: los -cortado del árbol, ocultos
        self.watch = False                    # vigilancia del árbol activa
        self.monitors = {}                    # raíz: carpeta vigilada → Gio.FileMonitor
        self.watch_gen = 0                    # raíz: activaciones; el recorrido de una activación vieja se descarta
        self.row = None
        self.eye_btn = self.watch_btn = self.count_label = None

    @property
    def name(self):
        return self.path.name or str(self.path)

    def sort_key(self):
        """Clave de orden de la fila de carpeta (ver doc_key)."""
        r = self.root
        return (self.order, 0) if r is None else (r.order, 2, self.rel, 0)

    def doc_key(self, seq):
        """Clave de orden del vídeo número `seq` de esta carpeta: la raíz, luego sus vídeos directos por orden de
        llegada, luego cada subcarpeta (en orden natural por ruta) con los suyos."""
        r = self.root
        return (self.order, 1, seq) if r is None else (r.order, 2, self.rel, 1, seq)

    def all_docs(self):
        """Raíz: sus vídeos y los de sus subcarpetas, en el orden del panel."""
        for d in self.docs:
            yield d
        for s in self.subs:
            for d in s.docs:
                yield d


# ----------------------------------------------------------------------------------------------
# ventana
# ----------------------------------------------------------------------------------------------
def load_settings():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(d):
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(d, indent=1), encoding="utf-8")
    except OSError:
        pass


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0


class ThumbSheet(Gtk.Window):
    REGEN_DEBOUNCE_MS = 350
    FLASH_MS = 4000

    def __init__(self, video_paths):
        super(ThumbSheet, self).__init__()
        self.settings = load_settings()
        self.docs = []                        # todos los vídeos del panel, en su orden (las carpetas: self.roots)
        self._keys = []                       # clave de orden de cada uno (paralelo a docs)
        self._by_path = {}                    # ruta → Document: no repetir, y la vigilancia sabe qué hay cargado
        self.roots = []                       # carpetas raíz del panel (Folder), por orden de llegada
        self._own_outputs = set()             # lo que escribe esta app (-cortado): la vigilancia no lo carga
        self._geom_id = None                  # DEBUG: geometría pendiente de registrar
        self._scanning = 0                    # carpetas recorriéndose en segundo plano
        self._probing = None                  # documento que el hilo de sondeo está analizando ahora (ffprobe)
        self._probe_batch = 0                 # ficheros encolados al sondeo desde la última vez que se vació
        self._bar_logged = None               # DEBUG: último texto de la barra registrado
        self.current = None
        self.generator = None
        self._tick_id = None                  # refresco periódico del tiempo estimado
        self._status_id = None                # refresco agrupado del estado (llega por captura)
        self._coverage_busy = False           # recuento de caché de las filas en marcha (hilo aparte)
        self._coverage_again = False
        self._quarter = -1                    # cuadrito (0-15) del progreso ya reflejado en el icono de la fila
        self.gen_doc = None                   # vídeo que está generando self.generator (actual o 2.º plano)
        self._cut_pct = None
        self._regen_id = None
        self._status_base = ""
        self._progress = (0, 0)
        self._wheel_acc = {}
        self.set_default_size(int(self.settings.get("win_w", 1200)), int(self.settings.get("win_h", 800)))
        if self.settings.get("maximized"):
            self.maximize()
        for name in ("icon.png", "icon.svg"):
            ic = APP_DIR / name
            if ic.exists():
                try:
                    self.set_icon_from_file(str(ic))
                    break
                except GLib.Error:
                    pass

        interval = snap_interval(self.settings.get("interval", INTERVAL_DEF))
        try:
            cols = int(self.settings.get("cols", COLS_DEF))
        except (TypeError, ValueError):
            cols = COLS_DEF
        cols = max(COLS_MIN, min(COLS_MAX, cols))

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(vbox)

        # --- barra de herramientas ---
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_margin_start(10)
        bar.set_margin_end(10)
        bar.set_margin_top(6)
        bar.set_margin_bottom(6)
        vbox.pack_start(bar, False, False, 0)

        # el slider de intervalo recorre posiciones de INTERVALS (0..n-1), con marcas rotuladas
        self.interval_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, len(INTERVALS) - 1, 1)
        self.interval_scale.set_draw_value(False)
        self.interval_scale.set_round_digits(0)
        for i, sec in enumerate(INTERVALS):
            self.interval_scale.add_mark(i, Gtk.PositionType.BOTTOM, interval_mark(sec))
        self.interval_scale.set_hexpand(True)
        self.interval_scale.set_value(INTERVALS.index(interval))
        self.interval_scale.set_tooltip_text("Tiempo entre capturas: 1 s, 2 s, 5 s, 10 s, 20 s, 30 s, 1, 5 o 10 min (rueda = un paso; "
                                             "Ctrl+Mayús+rueda sobre el mosaico: arriba acerca, abajo aleja)")
        bar.pack_start(self.interval_scale, True, True, 0)

        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 6)

        self.tile_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, COLS_MIN, COLS_MAX, 1)
        self.tile_scale.set_draw_value(False)
        self.tile_scale.set_round_digits(0)
        for n in (3, 5, 10, 15, 20):
            self.tile_scale.add_mark(n, Gtk.PositionType.BOTTOM, str(n))
        self.tile_scale.set_hexpand(True)
        self.tile_scale.set_inverted(True)   # 20 por fila a la izquierda, 3 a la derecha: hacia la derecha, más grandes
        self.tile_scale.set_value(cols)
        self.tile_scale.set_tooltip_text("Teselas por fila, de 20 a 3: hacia la derecha, más grandes "
                                         "(rueda = un paso; Ctrl+rueda sobre el mosaico)")
        bar.pack_start(self.tile_scale, True, True, 0)

        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 6)

        self.snap_btn = Gtk.ToggleButton(label="Snap scroll")
        self.snap_btn.set_tooltip_text("Desplazar por filas enteras: la primera fila a la vista queda siempre completa y en el "
                                       "mismo sitio, así la rejilla no se mueve al hacer scroll (salvo filas de distinta altura, "
                                       "en vídeos que cambian de orientación)")
        self.snap_btn.set_active(bool(self.settings.get("snap", False)))
        bar.pack_start(self.snap_btn, False, False, 0)

        # derecha: [contador] [Cortar] [Full] [Eliminar] [Segmentos]
        self.del_btn = Gtk.Button(label="Eliminar")
        self.del_btn.get_style_context().add_class("destructive-action")
        self.del_btn.set_tooltip_text("Borrar el archivo de vídeo del disco (sin papelera), tras confirmar (Supr; Intro confirma)")
        self.del_btn.connect("clicked", self.on_delete)
        self.seg_btn = Gtk.ToggleButton(label="Segmentos")
        self.seg_btn.set_tooltip_text("Panel de segmentos: los tramos seleccionados en su orden de corte (arrastra para "
                                      "reordenar, × para quitar)")
        self.seg_btn.set_active(bool(self.settings.get("segpanel", True)))
        bar.pack_end(self.seg_btn, False, False, 0)
        bar.pack_end(self.del_btn, False, False, 0)
        self.cut_btn = Gtk.Button(label="Cortar")
        self.cut_btn.set_tooltip_text("Cortar los segmentos seleccionados y unirlos en <vídeo>-cortado (sin pérdida, en keyframes; "
                                      "o exacto recodificando). Tecla C; en el diálogo, Intro corta y borra el original")
        self.cut_btn.connect("clicked", self.on_cut)
        self._cut = None
        self.full_btn = Gtk.Button(label="Full")
        self.full_btn.set_tooltip_text("Dar por bueno el vídeo entero: lo renombra como <vídeo>-cortado (sin tocar el "
                                       "contenido, como si se hubiera cortado de principio a fin) y lo quita de la lista")
        self.full_btn.connect("clicked", self.on_full)
        bar.pack_end(self.full_btn, False, False, 0)
        bar.pack_end(self.cut_btn, False, False, 0)
        # mensaje arriba, barra de progreso abajo (generación en curso o en segundo plano, o corte)
        status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.status = Gtk.Label(label="")
        self.status.set_xalign(1.0)
        self.status.set_ellipsize(Pango.EllipsizeMode.START)
        self.status.set_width_chars(46)
        self.status.set_max_width_chars(46)   # ancho fijo: el mensaje cambia a menudo y los sliders no deben moverse
        status_box.pack_start(self.status, False, False, 0)
        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(True)
        self.progress.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.progress.set_valign(Gtk.Align.CENTER)
        status_box.pack_start(self.progress, False, False, 0)
        bar.pack_end(status_box, False, False, 4)

        # --- panel de ficheros + mosaico (con la vista ampliada superpuesta) ---
        self.overlay = Gtk.Overlay()
        vbox.pack_start(self.overlay, True, True, 0)
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.segpanel = SegPanel(self._seg_reorder, self._seg_delete, self._seg_goto)
        self.seg_revealer = Gtk.Revealer()
        self.seg_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_LEFT)
        self.seg_revealer.add(self.segpanel)
        self.seg_revealer.set_reveal_child(self.seg_btn.get_active())
        self.seg_btn.connect("toggled", lambda b: self.seg_revealer.set_reveal_child(b.get_active()))
        main = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        main.pack_start(self.paned, True, True, 0)
        main.pack_end(self.seg_revealer, False, False, 0)
        self.overlay.add(main)
        self.layer = PreviewLayer()
        self.layer.on_close = self.hide_preview
        self.layer.on_toggle_play = self.toggle_play
        self.layer.on_seek = self.preview_seek
        self.layer.on_wheel = self._preview_wheel
        self.overlay.add_overlay(self.layer)
        self.toast = Toast()
        self.overlay.add_overlay(self.toast)
        self._preview_key = None
        self.player = None
        self._player_doc = None
        self._tick_id = None
        self._seek_id = None
        self._seek_at = None    # (instante pedido, momento) del último seek: base de los pasos encadenados de flechas
                                # o rueda mientras el reproductor aún informa de la posición anterior
        # extracción de fotogramas completos: un solo hilo, atiende primero lo último pedido (LIFO) y
        # descarta lo que ya exista; así mantener pulsada una flecha no lanza decenas de ffmpeg
        self._full_lock = threading.Condition()
        self._full_jobs = []
        threading.Thread(target=self._full_loop, name="ts-full", daemon=True).start()
        side = Gtk.ScrolledWindow()
        side.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.BROWSE)
        self.listbox.set_sort_func(self._row_compare)   # carpetas y vídeos por su clave (Folder.doc_key)
        self.listbox.connect("row-selected", self.on_row_selected)
        self.listbox.connect("move-cursor", self._on_move_cursor)
        side.add(self.listbox)
        self.count_label = Gtk.Label(label="")
        self.count_label.set_xalign(0.0)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.get_style_context().add_class("ts-panel-header")
        header.pack_start(self.count_label, True, True, 0)
        add_btn = Gtk.Button.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON)
        add_btn.set_relief(Gtk.ReliefStyle.NONE)
        add_btn.set_tooltip_text("Añadir vídeos a la lista (Ctrl+O). Cada recorte se guarda junto a su original.")
        add_btn.connect("clicked", lambda *_: self.add_paths(choose_videos(self)))
        dir_btn = Gtk.Button.new_from_icon_name("folder-open-symbolic", Gtk.IconSize.BUTTON)
        dir_btn.set_relief(Gtk.ReliefStyle.NONE)
        dir_btn.set_tooltip_text("Añadir los vídeos de una carpeta y sus subcarpetas (Ctrl+Mayús+O)")
        dir_btn.connect("clicked", lambda *_: self.add_paths(choose_folders(self)))
        header.pack_end(add_btn, False, False, 0)
        header.pack_end(dir_btn, False, False, 0)
        self.listbox.get_style_context().add_class("ts-files")
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        panel.set_size_request(140, -1)
        panel.pack_start(header, False, False, 0)
        panel.pack_start(side, True, True, 0)
        self.paned.pack1(panel, False, False)
        self._update_count()
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.ALWAYS)
        self.scroller.set_overlay_scrolling(False)   # barra clásica, siempre visible, con el asa ancha (CSS)
        self.scroller.get_style_context().add_class("ts-sheet-scroll")
        self.sheet = Sheet()
        self.sheet.set_cols(cols)
        self.sheet.set_vadj(self.scroller.get_vadjustment())
        self.sheet.set_snap(self.snap_btn.get_active())
        self.snap_btn.connect("toggled", lambda b: self.sheet.set_snap(b.get_active()))
        self.sheet.on_drag_begin = self.on_drag_begin
        self.sheet.on_drag_apply = self.on_drag_apply
        self.sheet.on_drag_end = self.on_drag_end
        self.sheet.on_preview = self.show_preview
        self.sheet.on_marks_changed = self._marks_changed
        self._sel_snapshot = None
        self.scroller.add(self.sheet)
        self._marks_logged = None
        self.scroller.get_vscrollbar().connect_after("draw", self._draw_scroll_marks)   # marcas rojas de los segmentos
        self.paned.pack2(self.scroller, True, False)
        self.paned.set_position(int(self.settings.get("panel_w", 240)))

        self.interval_scale.connect("value-changed", self.on_interval_changed)
        self.interval_scale.connect("scroll-event", self._slider_scroll, 1)
        self.tile_scale.connect("value-changed", self.on_tile_changed)
        self.tile_scale.connect("scroll-event", self._slider_scroll, -1)   # rueda arriba = teselas más grandes
        # arrastrando con el ratón, el valor se aplica al soltar; con la rueda
        # o el teclado, que son pasos sueltos, al momento
        self._slider_drag = set()
        for sc in (self.interval_scale, self.tile_scale):
            sc.connect("button-press-event", self._slider_press)
            sc.connect("button-release-event", self._slider_release)
        self.scroller.connect("scroll-event", self.on_scroll)
        self.connect("key-press-event", self.on_key)
        self.connect("destroy", self.on_destroy)
        self.connect("window-state-event", self.on_window_state)
        self._maximized = bool(self.settings.get("maximized"))
        # arrastrar y soltar vídeos sobre la ventana
        self.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.connect("drag-data-received", self.on_drag_data)

        # sondeo de vídeos en un hilo (ffprobe), de uno en uno
        self._probe_q = queue.Queue()
        threading.Thread(target=self._probe_loop, name="ts-probe", daemon=True).start()

        self.show_all()
        self._load_current()
        self.add_paths(video_paths)
        self._gst_warm = False
        GLib.timeout_add(2500, self._warm_gst)   # con la ventana ya en marcha, precargar el reproductor aparte
        if DEBUG:
            GLib.timeout_add(1500, self._log_geometry)
            self._lag = [time.perf_counter(), 0.0, 0]   # último tic, retraso máximo, tics
            GLib.timeout_add(50, self._lag_tick)

    def _lag_tick(self):
        """DEBUG: cuánto llega tarde un temporizador de 50 ms = cuánto estuvo bloqueado el hilo de la interfaz."""
        now = time.perf_counter()
        late = now - self._lag[0] - 0.05
        self._lag[0] = now
        self._lag[1] = max(self._lag[1], late)
        if late > 0.08:
            snap = ", ".join("%s %d ms" % (k, 1000 * v) for k, v in sorted(_PROF.items(), key=lambda kv: -kv[1])[:4])
            log("LAG %d ms ahora · %s" % (1000 * late, snap))
        self._lag[2] += 1
        if self._lag[2] >= 40:   # cada ~2 s
            rep = _prof_report()
            if self._lag[1] > 0.03:
                log("lag: hilo de la interfaz bloqueado hasta %d ms · %s" % (1000 * self._lag[1], rep))
            elif rep:
                log("ui: %s" % rep)
            self._lag[1] = 0.0
            self._lag[2] = 0
        return True

    def _log_geometry(self):
        """Sólo con THUMBSHEET_DEBUG=1: coordenadas (relativas a la ventana) que usa el harness de pruebas."""
        def origin(w):
            pt = w.translate_coordinates(self, 0, 0)   # PyGObject devuelve (x, y) o (ok, x, y) según versión
            return int(pt[-2]), int(pt[-1])

        def center(w):
            x, y = origin(w)
            a = w.get_allocation()
            return "%d,%d" % (x + a.width // 2, y + a.height // 2)
        sx, sy = origin(self.sheet)
        rows = ";".join(center(d.row) for d in self.docs if d.row is not None and not d.hidden)
        first = self.roots[0] if self.roots else None   # ojo y vigilancia de la primera carpeta raíz
        vb = self.scroller.get_vscrollbar()
        bx, by = origin(vb)
        ba = vb.get_allocation()
        log("geometry: sheet=%d,%d cols=%d cell=%dx%d pad=%d gap=%d interval=%s tile=%s tilew=%d cut=%s del=%s full=%s "
            "snap=%s vbar=%d,%d,%d eye=%s watch=%s rows=%s" % (
                sx, sy, self.sheet.cols, self.sheet.cell_w, self.sheet.cell_h, Sheet.PAD, Sheet.GAP,
                center(self.interval_scale), center(self.tile_scale), self.tile_scale.get_allocation().width,
                center(self.cut_btn), center(self.del_btn), center(self.full_btn), center(self.snap_btn),
                bx + ba.width // 2, by, ba.height,
                center(first.eye_btn) if first else "-", center(first.watch_btn) if first else "-", rows))
        return False

    def _geometry_soon(self):
        """DEBUG: volver a registrar la geometría (el panel ha cambiado de filas) cuando GTK haya recolocado."""
        if DEBUG and self._geom_id is None:
            self._geom_id = GLib.timeout_add(300, self._geometry_now)

    def _geometry_now(self):
        self._geom_id = None
        self._log_geometry()
        return False

    def _log_panel(self):
        """DEBUG: el árbol del panel en una línea («raíz: a, b | sub: c»; ~ delante de lo oculto)."""
        def names(f):
            return ", ".join(("~" if d.hidden else "") + d.path.name for d in f.docs)
        parts = []
        for r in self.roots:
            parts.append("%s: %s" % (r.name, names(r)))
            for s in r.subs:
                shown = s.row is not None and s.row.get_visible()
                parts.append("%s%s: %s" % ("" if shown else "~", s.rel[1], names(s)))
        log("panel: " + " | ".join(parts))

    # ---- documentos --------------------------------------------------------------------------
    def add_paths(self, paths):
        """Ficheros se añaden ya, cada uno bajo su carpeta del panel (con fila de carpeta nueva si hace falta), detrás de
        los que ya tuviera; las carpetas se recorren (con subcarpetas) en un hilo y entran cuando acaba."""
        new = []
        dirs = []
        for path in paths:
            try:
                rp = pathlib.Path(path).resolve()
            except OSError:
                continue
            if rp.is_dir():
                dirs.append(rp)
                continue
            if not rp.is_file() or rp in self._by_path:
                continue
            doc = Document(rp)
            self._attach_doc(doc)
            new.append(doc)
            self._probe_batch += 1
            self._probe_q.put(doc)
        if new:
            for root in set(d.folder.root or d.folder for d in new):   # una pasada por raíz, no por fichero
                self._refresh_folder(root)
            self._update_count()
            self._pause_background()   # sondeo pendiente: que el segundo plano no lo frene
            self._refresh_status()     # la barra dice «Analizando k de n archivos…» mientras dura el sondeo
            if DEBUG:
                self._log_panel()
            self._geometry_soon()
        if self.current is None:
            first = next((d for d in new if not d.hidden), None)
            if first is not None:
                self.listbox.select_row(first.row)
        if dirs:
            self._scan_dirs(dirs)

    def _attach_doc(self, doc):
        """Mete `doc` en su carpeta del panel (raíz o subcarpeta; se crean si hacen falta) y en `self.docs`, en el
        orden del panel: dentro de cada carpeta, por orden de llegada (los de un recorrido vienen en orden natural;
        los que trae la vigilancia van al final, como una cola). Por bisección: ni se reordena la lista ni se
        recorre el panel."""
        folder = self._folder_for(doc.path.parent)
        doc.folder = folder
        folder.docs.append(doc)
        folder.seq += 1
        key = folder.doc_key(folder.seq)
        i = bisect.bisect(self._keys, key)
        self._keys.insert(i, key)
        self.docs.insert(i, doc)
        self._by_path[doc.path] = doc
        doc.row = self._make_row(doc)
        doc.row.key = key
        self.listbox.add(doc.row)   # la lista lo coloca por su clave
        doc.row.show_all()

    def _root_covering(self, d):
        """La raíz del panel más honda que contiene la carpeta `d` (o es ella), o None."""
        best = None
        for r in self.roots:
            if (r.path == d or r.path in d.parents) and (best is None or len(r.path.parts) > len(best.path.parts)):
                best = r
        return best

    def _add_root(self, d, dirs=None):
        """Fila de carpeta raíz para `d` (aunque no tenga vídeos: desde ella se vigila), si ninguna raíz la cubre ya.
        El ojo y la vigilancia se restauran de la configuración; `dirs` (las carpetas del recorrido que acaba de
        hacerse) ahorra volver a recorrerla para poner los vigilantes."""
        root = self._root_covering(d)
        if root is not None:
            return root
        root = Folder(d, None, len(self.roots))
        self.roots.append(root)
        saved = (self.settings.get("folders") or {}).get(str(d)) or {}
        root.hide_done = bool(saved.get("hide_done"))
        root.row = self._make_folder_row(root)
        self.listbox.add(root.row)
        root.row.show_all()
        self._refresh_folder(root)
        if saved.get("watch"):
            self._set_watch(root, True, dirs)
            if root.watch:
                root.watch_btn.set_active(True)   # _on_watch no hace nada: ya está activa
        return root

    def _folder_for(self, d):
        """Carpeta del panel en la que van los vídeos de la carpeta `d`: una raíz (la que la cubre, o una nueva) o una
        subcarpeta de esa raíz (con fila nueva si no la tenía)."""
        root = self._add_root(d)
        if root.path == d:
            return root
        for s in root.subs:
            if s.path == d:
                return s
        sub = Folder(d, root)
        i = bisect.bisect([s.rel for s in root.subs], sub.rel)
        root.subs.insert(i, sub)
        sub.row = self._make_folder_row(sub)
        self.listbox.add(sub.row)
        sub.row.show_all()
        return sub

    def _scan_dirs(self, dirs):
        """Busca los vídeos soportados de las carpetas en un hilo (una carpeta remota puede tardar) y los añade, cada
        carpeta con su fila de raíz (aunque esté vacía)."""
        self._scanning += 1
        self._update_count()
        self._refresh_status()
        log("carpetas: buscando vídeos en %s" % ", ".join(str(d) for d in dirs))

        def done(found, seen, dt):
            self._scanning -= 1

            def rel(f):
                for d in dirs:
                    if d in f.parents:
                        return str(f.relative_to(d))
                return str(f)
            names = ", ".join(rel(f) for f in found)
            log("carpetas: %d vídeos en %.1f s: %s" % (len(found), dt, names if len(names) <= 300 else names[:300] + "…"))
            for d in dirs:
                self._add_root(d, [x for x in seen if x == d or d in x.parents])
            if found:
                self.add_paths(found)
            else:
                self.toast.show_error("Sin vídeos soportados en %s" % ", ".join(d.name or str(d) for d in dirs))
            self._update_count()
            self._refresh_status()
            if DEBUG:
                self._log_panel()
            return False

        def work():
            t0 = time.time()
            found, seen = walk_videos(dirs)
            GLib.idle_add(done, found, seen, time.time() - t0)
        threading.Thread(target=work, name="ts-scan", daemon=True).start()

    def _update_count(self):
        n = len(self.docs)
        hidden = sum(1 for d in self.docs if d.hidden)
        text = "Sin archivos" if n == 0 else "1 archivo" if n == 1 else "%d archivos" % n
        if hidden:
            text += " · %d oculto%s" % (hidden, "" if hidden == 1 else "s")
        if self._scanning:
            text += " · buscando…"
        self.count_label.set_text(text)
        if DEBUG:
            log("archivos: %d%s" % (n, (" ocultos: %d" % hidden) if hidden else ""))

    def _make_row(self, doc):
        row = Gtk.ListBoxRow()
        row.doc = doc
        # dos líneas (nombre; duración·resolución·tamaño + icono de estado), sangradas bajo su carpeta: márgenes
        # mínimos y letra pequeña en la segunda
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_margin_start(6 + PANEL_INDENT * (1 if doc.folder.root is None else 2))
        box.set_margin_end(8)
        box.set_margin_top(1)
        box.set_margin_bottom(1)
        name = Gtk.Label(label=doc.path.name)
        name.set_xalign(0.0)
        name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        name.set_tooltip_text(str(doc.path))
        sub = Gtk.Label()
        sub.set_xalign(0.0)
        sub.get_style_context().add_class("dim-label")
        sub.set_ellipsize(Pango.EllipsizeMode.END)   # que una línea larga nunca fuerce el ancho del panel
        sub.set_markup(sub_markup(doc.subtitle))
        state = StateIcon()
        lower = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lower.pack_start(sub, True, True, 0)
        lower.pack_end(state, False, False, 0)
        box.pack_start(name, False, False, 0)
        box.pack_start(lower, False, False, 0)
        row.add(box)
        row.sub_label = sub
        row.state = state
        return row

    def _make_folder_row(self, folder):
        """Fila de carpeta: no se selecciona ni coge el foco. Raíz: ruta (~ por el home) en negrita, recuento y los dos
        botones; subcarpeta: su ruta relativa, atenuada y sangrada."""
        row = Gtk.ListBoxRow()
        row.doc = None
        row.folder = folder
        row.key = folder.sort_key()
        row.set_selectable(False)
        row.set_activatable(False)
        row.set_can_focus(False)
        row.get_style_context().add_class("ts-folder")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.set_margin_start(6 + (0 if folder.root is None else PANEL_INDENT))
        box.set_margin_end(2)
        box.pack_start(Gtk.Image.new_from_icon_name("folder-symbolic", Gtk.IconSize.MENU), False, False, 0)
        name = Gtk.Label()
        name.set_xalign(0.0)
        name.set_ellipsize(Pango.EllipsizeMode.START)   # si no cabe, que se vea el final: el nombre de la carpeta
        name.set_tooltip_text(str(folder.path))
        box.pack_start(name, True, True, 0)
        if folder.root is not None:
            name.set_text(folder.rel[1])
            name.get_style_context().add_class("dim-label")
            row.add(box)
            return row
        name.set_markup("<b>%s</b>" % GLib.markup_escape_text(short_path(folder.path)))
        count = Gtk.Label()
        count.get_style_context().add_class("dim-label")
        box.pack_start(count, False, False, 2)
        eye = Gtk.ToggleButton()
        eye.add(icon_or_text(EYE_OPEN, "ojo"))
        watch = Gtk.ToggleButton()
        watch.add(icon_or_text("view-refresh-symbolic", "vigilar"))
        for b in (eye, watch):
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.set_can_focus(False)              # las flechas siguen moviéndose por los vídeos
            b.set_focus_on_click(False)
        box.pack_end(watch, False, False, 0)
        box.pack_end(eye, False, False, 0)
        folder.eye_btn, folder.watch_btn, folder.count_label = eye, watch, count
        eye.set_active(folder.hide_done)
        eye.connect("toggled", self._on_eye, folder)
        watch.connect("toggled", self._on_watch, folder)
        self._folder_tips(folder)
        row.add(box)
        return row

    def _folder_tips(self, root):
        """Recuento y tooltips de la fila de una raíz según su estado, y el icono del ojo (abierto o tachado)."""
        n = sum(1 for _ in root.all_docs())
        hidden = sum(1 for d in root.all_docs() if d.hidden)
        root.count_label.set_markup(sub_markup(("%d vídeo%s" % (n, "" if n == 1 else "s")) +
                                               ((" · %d oculto%s" % (hidden, "" if hidden == 1 else "s")) if hidden else "")))
        img = root.eye_btn.get_child()
        if isinstance(img, Gtk.Image) and Gtk.IconTheme.get_default().has_icon(EYE_CLOSED):
            img.set_from_icon_name(EYE_CLOSED if root.hide_done else EYE_OPEN, Gtk.IconSize.MENU)
        root.eye_btn.set_tooltip_text(
            ("Mostrar también los vídeos ya cortados (%d ocultos)" % hidden) if root.hide_done else
            "Ocultar los vídeos ya cortados (<nombre>-cortado) de esta carpeta y sus subcarpetas; las subcarpetas en "
            "las que no quede ninguno a la vista desaparecen hasta que llegue otro vídeo")
        root.watch_btn.set_tooltip_text(
            ("Vigilando esta carpeta y sus subcarpetas (%d): los vídeos que aparezcan se cargan solos y se generan en "
             "segundo plano. Clic para dejar de vigilar" % len(root.monitors)) if root.watch else
            "Vigilar esta carpeta y sus subcarpetas: entran los vídeos que falten y, desde ahora, los que aparezcan "
            "(también en subcarpetas nuevas); todos se generan en segundo plano")

    def _refresh_folder(self, root):
        """Qué se ve de una raíz: con el ojo activo, los -cortado se ocultan (y si uno se estaba generando en segundo
        plano, se para); una subcarpeta sólo se muestra mientras le quede algún vídeo a la vista. Si el vídeo a la
        vista queda oculto se pasa al siguiente. Actualiza el recuento de la fila."""
        shown = False
        for d in root.all_docs():
            h = bool(root.hide_done and is_cut_name(d.path))
            if h == d.hidden:
                continue
            d.hidden = h
            if d.row is not None:
                d.row.set_visible(not h)
            shown = shown or not h
        for s in root.subs:
            if s.row is not None:
                s.row.set_visible(any(not d.hidden for d in s.docs))
        self._folder_tips(root)
        cur = self.current
        if cur is not None and cur.hidden:
            self._show_neighbor(cur)
        gen = self.gen_doc
        if gen is not None and gen.hidden and gen is not self.current and self.generator is not None \
                and not self.generator.finished:
            self._cancel_generator()   # se estaba generando en segundo plano un vídeo que acaba de ocultarse
            GLib.idle_add(self._start_next_background)
        if shown:
            self._refresh_row_states()   # los que vuelven a verse: su icono de caché
            if self.current is None:
                first = pick_next(self.docs, 0, self._usable)
                if first is not None:
                    self.listbox.select_row(first.row)

    def _on_eye(self, btn, root):
        root.hide_done = btn.get_active()
        self._save_folder_prefs(root)
        self._refresh_folder(root)
        self._update_count()
        log("ojo: %s: %s (%d ocultos)" % (root.name, "ocultar cortados" if root.hide_done else "mostrar todo",
                                          sum(1 for d in root.all_docs() if d.hidden)))
        if DEBUG:
            self._log_panel()
        self._geometry_soon()

    def _save_folder_prefs(self, root):
        """Ojo y vigilancia de cada raíz se recuerdan por ruta (settings «folders»); sin nada activo, se olvida."""
        prefs = self.settings.get("folders")
        if not isinstance(prefs, dict):
            prefs = self.settings["folders"] = {}
        key = str(root.path)
        if root.hide_done or root.watch:
            prefs[key] = {"hide_done": root.hide_done, "watch": root.watch}
        else:
            prefs.pop(key, None)

    @staticmethod
    def _row_compare(a, b):
        ka, kb = a.key, b.key
        return -1 if ka < kb else (1 if ka > kb else 0)

    @staticmethod
    def _usable(d):
        """Vídeo al que se puede pasar: con fila, a la vista y sin borrarse."""
        return d.row is not None and not d.hidden and not d.deleting

    def _on_move_cursor(self, box, step, count):
        """Flechas e Inicio/Fin en el panel: de vídeo a la vista en vídeo a la vista, saltando de un solo paso las filas
        de carpeta (GTK pararía el cursor en ellas aunque no se puedan seleccionar) y los ocultos."""
        cur = self.current
        if step == Gtk.MovementStep.DISPLAY_LINES and cur in self.docs:
            i = self.docs.index(cur)
            seq = self.docs[i + 1:] if count > 0 else self.docs[:i][::-1]
        elif step == Gtk.MovementStep.BUFFER_ENDS:
            seq = self.docs if count < 0 else self.docs[::-1]
        else:
            return
        target = next((d for d in seq if self._usable(d)), None)
        if target is not None and target is not cur:
            box.select_row(target.row)
            target.row.grab_focus()
        box.stop_emission_by_name("move-cursor")

    # ---- vigilancia de carpetas ------------------------------------------------------------------
    def _on_watch(self, btn, root):
        if btn.get_active() != root.watch:
            self._set_watch(root, btn.get_active())

    def _set_watch(self, root, on, dirs=None):
        """Vigilancia del árbol de una raíz: un vigilante (inotify, vía Gio) por carpeta, también en las vacías y en
        las que se creen después; no cuesta nada mientras no pasa nada. `dirs` son las carpetas del árbol si acaban
        de recorrerse (y sus vídeos ya están entrando); si no, se recorre en un hilo y, además de los vigilantes,
        entran los vídeos que hubiera sin cargar: vigilar una carpeta es tener en el panel todo lo que contiene."""
        root.watch = on
        root.watch_gen += 1
        self._save_folder_prefs(root)
        if not on:
            n = len(root.monitors)
            for m in root.monitors.values():
                m.cancel()
            root.monitors = {}
            log("vigilar: %s: desactivada (%d carpetas)" % (root.name, n))
            self._folder_tips(root)
            return
        if dirs is not None:
            self._watch_attach(root, dirs, root.watch_gen)
            return
        gen = root.watch_gen

        def ready(found, seen):
            self._watch_attach(root, seen, gen)
            if gen == root.watch_gen and root.watch:
                self._watch_found(root, found, "ya estaba")
            return False

        def work():
            found, seen = walk_videos([root.path])
            GLib.idle_add(ready, found, seen)
        threading.Thread(target=work, name="ts-watch", daemon=True).start()

    def _watch_attach(self, root, dirs, gen, grown=None):
        """Pone vigilante a las carpetas de `dirs` que no lo tengan (`grown`: subcarpeta nueva, sólo para el log)."""
        if gen != root.watch_gen or not root.watch:
            return False
        err = None
        for d in dirs:
            if d in root.monitors:
                continue
            try:
                m = Gio.File.new_for_path(str(d)).monitor_directory(Gio.FileMonitorFlags.NONE, None)
            except GLib.Error as e:
                err = e.message
                continue
            m.connect("changed", self._on_dir_event, root)
            root.monitors[d] = m
        if not root.monitors and err:
            log("vigilar: %s: no se pudo vigilar: %s" % (root.name, err))
            self._error("No se pudo vigilar la carpeta %s" % root.name, err)
            root.watch_btn.set_active(False)   # → _on_watch → _set_watch(False)
            return False
        if err:
            log("vigilar: %s: alguna carpeta no se pudo vigilar: %s" % (root.name, err))
        log("vigilar: %s: %s (%d carpetas)" % (root.name, ("carpeta nueva %s" % grown) if grown else "activada",
                                              len(root.monitors)))
        self._folder_tips(root)
        return False

    def _watch_drop(self, root, d):
        """Carpeta vigilada borrada (o movida fuera): fuera su vigilante y los de sus subcarpetas."""
        for x in [x for x in root.monitors if x == d or d in x.parents]:
            root.monitors.pop(x).cancel()
        if DEBUG:
            log("vigilar: %s: fuera %s (%d carpetas)" % (root.name, d.name, len(root.monitors)))
        self._folder_tips(root)

    def _watch_grow(self, root, d):
        """Subcarpeta nueva en un árbol vigilado: se recorre en un hilo, para vigilarla (y a sus subcarpetas) y cargar
        los vídeos con los que haya llegado (una carpeta movida aquí viene con ellos dentro)."""
        gen = root.watch_gen
        try:
            rel = d.relative_to(root.path)
        except ValueError:
            rel = d

        def grown(found, seen):
            if gen == root.watch_gen and root.watch:
                self._watch_attach(root, seen, gen, grown=rel)
                self._watch_found(root, found)
            return False

        def work():
            found, seen = walk_videos([d])
            GLib.idle_add(grown, found, seen)
        threading.Thread(target=work, name="ts-watch", daemon=True).start()

    def _watch_found(self, root, paths, why="nuevo vídeo"):
        """Vídeos de un árbol vigilado (aparecidos, o que ya estaban al activarla): los que no estén cargados (ni sean
        de esta app) entran por el camino de siempre: sondeo y, después, generación en segundo plano."""
        new = [p for p in paths if p not in self._by_path and p not in self._own_outputs]
        if not new:
            return
        for p in new:
            try:
                rel = p.relative_to(root.path)
            except ValueError:
                rel = p
            log("vigilar: %s %s" % (why, rel))
        self.add_paths(new)

    def _on_dir_event(self, monitor, f, other, ev, root):
        """Aviso de una carpeta vigilada. Vídeo nuevo cuando termina de escribirse (CHANGES_DONE_HINT: al cerrarlo, o
        a los 2 s de aparecer si llegó movido); carpeta nueva → vigilarla y recorrerla; carpeta borrada → fuera sus
        vigilantes. Lo que escribe esta app (-cortado) no se carga."""
        path = f.get_path()
        if not path or not root.watch:
            return
        p = pathlib.Path(path)
        if ev == Gio.FileMonitorEvent.DELETED:
            if p in root.monitors:
                self._watch_drop(root, p)
            return
        if p.name.startswith("."):
            return
        if ev == Gio.FileMonitorEvent.CREATED:
            if p.is_dir() and not p.is_symlink() and p not in root.monitors:
                self._watch_grow(root, p)
        elif ev == Gio.FileMonitorEvent.CHANGES_DONE_HINT and is_video_name(p.name) and p.is_file():
            self._watch_found(root, [p])

    def _probe_loop(self):
        while True:
            doc = self._probe_q.get()
            self._probing = doc
            try:
                doc.ensure_info()
            except Exception as e:  # noqa: BLE001  — por si acaso: si este hilo muriera, la cola quedaría parada
                log("sondeo: excepción con %s: %r" % (doc.path.name, e))
                if doc.info is None and doc.error is None:
                    doc.error = short_error(str(e), e.__class__.__name__)
            if doc.error:
                log("sondeo: %s: no se pudo abrir: %s" % (doc.path.name, doc.error))
            self._probing = None
            GLib.idle_add(self._doc_probed, doc)

    def _doc_probed(self, doc):
        if doc.row is not None:
            doc.row.sub_label.set_markup(sub_markup(doc.subtitle))
        if self._probe_q.qsize() == 0 and self._probing is None:
            self._probe_batch = 0      # sondeo al día: la próxima tanda cuenta desde cero
        if doc is self.current:
            self._load_current()
        else:
            self._start_next_background()
        self._refresh_row_states()
        self._refresh_status()
        return False

    def on_row_selected(self, listbox, row):
        self.show_document(row.doc if row is not None else None)

    def show_document(self, doc):
        if doc is self.current:
            return
        t0 = time.perf_counter()
        self._show_document(doc)
        if DEBUG:
            log("cambio: %.0f ms en total" % (1000 * (time.perf_counter() - t0)))

    def _show_document(self, doc):
        self.hide_preview()
        if self.current is not None:
            self.current.scroll = self.scroller.get_vadjustment().get_value()
        t0 = time.perf_counter()
        self._cancel_generator()
        t1 = time.perf_counter()
        self.current = doc
        self._load_current()
        if DEBUG:
            log("cambio: cancelar %.0f ms, cargar %.0f ms" % (1000 * (t1 - t0), 1000 * (time.perf_counter() - t1)))

    def _load_current(self):
        doc = self.current
        self.set_title("%s — %s" % (doc.path.name, APP) if doc else APP)
        if doc is None or doc.info is None:
            self.sheet.set_video(16.0 / 9.0, None)
            self._progress = (0, 0)
            self._refresh_status()
            self._update_buttons()
            if doc is None:
                GLib.idle_add(self._start_next_background)   # sin vídeo a la vista: que siga el resto
            return
        t0 = time.perf_counter()
        self.sheet.set_video(doc.info.aspect, doc.cache_dir)
        t1 = time.perf_counter()
        log("vídeo: %s %dx%d %s %.2ffps dur=%s gop=%s miniatura=%dx%d" % (
            doc.path.name, doc.info.width, doc.info.height, doc.info.codec, doc.info.fps,
            fmt_time(doc.info.duration), doc.info.gop, doc.info.thumb_w, doc.info.thumb_h))
        self.regenerate()
        if DEBUG:
            log("cambio: set_video %.0f ms, regenerate %.0f ms" % (1000 * (t1 - t0), 1000 * (time.perf_counter() - t1)))
        if doc.scroll:
            GLib.timeout_add(60, self._restore_scroll, doc)
        self._update_buttons()

    def _restore_scroll(self, doc):
        if doc is self.current:
            self.scroller.get_vadjustment().set_value(doc.scroll)
        return False

    def _start_generator(self, doc, foreground):
        """Lanza la generación de `doc` al intervalo actual. En primer plano alimenta el mosaico; en
        segundo plano sólo rellena la caché (el mosaico muestra otro vídeo)."""
        self._cancel_generator()
        doc.gen_error = None   # si había desistido, esta pasada lo reintenta
        S = self.current_interval()
        holder = []
        current = lambda: self.generator is holder[0]  # noqa: E731
        def fg_tile(t):
            self.sheet.tile_ready(t)
            self.segpanel.tile_ready(t)
            return False

        def fg_tiles(ts):
            self.sheet.tiles_ready(ts)
            self.segpanel.tiles_ready(ts)
        on_tile = fg_tile if foreground else (lambda t: False)
        on_tiles = fg_tiles if foreground else (lambda ts: None)
        on_failed = self.sheet.tile_failed if foreground else (lambda t: False)
        on_aspect = self.sheet.set_aspect if foreground else (lambda t, a: False)
        gen = Generator(doc.info, doc.cache_dir, S,
                        lambda t: on_tile(t) if current() else False,
                        lambda d, n: self.on_progress(d, n) if current() else False,
                        lambda: self.on_done() if current() else False,
                        lambda t: on_failed(t) if current() else False,
                        focus_fn=self.sheet.focus_range if foreground else None,
                        on_aspect=lambda t, a: on_aspect(t, a) if current() else False,
                        on_tiles=lambda ts: on_tiles(ts) if current() else None)
        holder.append(gen)
        self.generator = gen
        self.gen_doc = doc
        self._progress = (0, gen.total)
        self._quarter = -1
        self._refresh_row_states()
        return gen

    def _busy(self):
        """Hay una operación interactiva en marcha: generación del vídeo a la vista, corte, vista ampliada
        (fotogramas completos o reproductor), sondeo de ficheros recién añadidos o borrado. Mientras tanto, la
        generación de los demás vídeos espera para no quitarle máquina."""
        gen = self.generator
        why = None
        if gen is not None and not gen.finished and self.gen_doc is self.current:
            why = "generando el vídeo a la vista"
        elif self._cut is not None:
            why = "cortando"
        elif self.layer.get_visible():
            why = "vista ampliada abierta"
        elif self._probe_q.qsize():
            why = "sondeando ficheros"
        elif any(d.deleting for d in self.docs):
            why = "borrando"
        if why and DEBUG:
            log("segundo plano: espera (%s)" % why)
        return why is not None

    def _cancel_generator(self):
        """Para el generador en marcha. Si era de un vídeo que no está a la vista y no había acabado, ese vídeo
        vuelve a ser candidato a segundo plano: antes quedaba marcado como intentado para este intervalo y no se
        retomaba hasta cambiar de intervalo (al abrir ese vídeo sus teselas faltaban y se generaban al vuelo)."""
        gen = self.generator
        if gen is None:
            return
        gen.cancel()
        self.generator = None
        if not gen.finished and self.gen_doc is not None and self.gen_doc is not self.current:
            self.gen_doc.bg_tried = None

    def _pause_background(self):
        """Empieza una operación interactiva: si se estaba generando un vídeo que no está a la vista, se para (lo
        hecho queda en caché) y vuelve a ser candidato cuando la operación termine."""
        gen = self.generator
        if gen is not None and not gen.finished and self.gen_doc is not self.current:
            log("segundo plano: en pausa (%s)" % (self.gen_doc.path.name if self.gen_doc else "?"))
            self._cancel_generator()
            self._refresh_status()
            self._refresh_row_states()

    def _start_next_background(self):
        """Cuando no hay nada generándose, sigue con el siguiente vídeo del panel (en orden, dando la
        vuelta) que aún no tenga todas las capturas de este intervalo. Uno cada vez."""
        if self.generator is not None and not self.generator.finished:
            return False
        if not self.docs or self._busy():
            return False
        S = self.current_interval()
        start = self.gen_doc if self.gen_doc in self.docs else self.current
        i0 = self.docs.index(start) + 1 if start in self.docs else 0
        for k in range(len(self.docs)):
            doc = self.docs[(i0 + k) % len(self.docs)]
            if doc is self.current or doc.info is None or doc.bg_tried == S or doc.deleting or doc.gen_error or doc.hidden:
                continue   # gen_error: no da fotogramas; no se insiste hasta que se abra ese vídeo. hidden: ya cortado
            doc.bg_tried = S
            if doc_complete(doc, S):
                continue
            log("segundo plano: %s" % doc.path.name)
            gen = self._start_generator(doc, foreground=False)
            self._refresh_status()
            gen.start()
            return False
        return False

    def regenerate(self):
        self._regen_id = None
        doc = self.current
        if not doc or not doc.info:
            self._refresh_row_states()
            return False
        S = self.current_interval()
        for d in self.docs:
            if d.bg_tried != S:
                d.bg_tried = None   # intervalo nuevo: los demás vuelven a ser candidatos a segundo plano
        t0 = time.perf_counter()
        gen = self._start_generator(doc, foreground=True)
        t1 = time.perf_counter()
        center = self.sheet.center_time()   # None si es un vídeo recién abierto (plan vacío)
        self.sheet.set_plan(gen.timestamps)
        if DEBUG:
            log("cambio: _start_generator %.0f ms, set_plan %.0f ms" % (1000 * (t1 - t0), 1000 * (time.perf_counter() - t1)))
        # se marcan las teselas que tocan algún segmento; los segmentos guardados no cambian con la rejilla
        self.sheet.set_selected(doc.selection.tiles(gen.timestamps, S, doc.info.duration))
        if center is not None:
            self.sheet.scroll_to_time(center)   # cambio de intervalo: misma zona del vídeo a la vista
        else:
            self.scroller.get_vadjustment().set_value(0)
        t2 = time.perf_counter()
        gen.start()
        if DEBUG:
            log("cambio: gen.start %.0f ms" % (1000 * (time.perf_counter() - t2)))
        self._refresh_status()
        self._update_buttons()
        return False

    # ---- callbacks de generación ---------------------------------------------------------------
    def on_progress(self, done, total):
        t0 = time.perf_counter()
        self._progress = (min(done, total), total)
        self._refresh_status()
        if DEBUG:
            _prof("on_progress", time.perf_counter() - t0)
        q = int(StateIcon.CELLS * self._progress[0] / float(total)) if total else 0
        if q != self._quarter:   # el icono de la fila sólo cambia cuando se llena otro cuadrito (dieciseisavo)
            self._quarter = q
            self._refresh_row_states()
        return False

    def on_done(self):
        gen, doc = self.generator, self.gen_doc
        if gen is not None and gen.error is not None and doc is not None:
            # el vídeo no da ningún fotograma: aspa roja en el panel, fuera del segundo plano, y se sigue con el resto
            doc.gen_error = gen.error
            if doc is self.current:
                self.sheet.fail_all()
                self.toast.show_error("No se pudieron sacar fotogramas: %s" % gen.error)
        self._refresh_status()
        self._refresh_row_states()
        GLib.idle_add(self._start_next_background)
        return False

    # ---- estado y botones ----------------------------------------------------------------------
    def _segments(self):
        doc = self.current
        if not doc or not doc.info:
            return []
        return doc.selection.ordered()   # Cortar y LLC respetan el orden del panel de segmentos

    def _refresh_row_states(self):
        """Icono de la tercera línea de cada fila: cuartos en gris claro mientras se generan sus miniaturas (a la
        vista o en segundo plano), negros según están en caché para el intervalo actual, aspa roja si el vídeo no
        se puede abrir o no da fotogramas."""
        S = self.current_interval()
        busy = self.generator is not None and not self.generator.finished
        gen_doc = self.gen_doc if busy else None
        if gen_doc is not None and gen_doc.row is not None:
            done, total = self._progress
            if gen_doc.row.state.set_progress(done, total, True) and DEBUG:
                log("estado: %s → %s" % (gen_doc.path.name, gen_doc.row.state.state_name()))
        for d in self.docs:
            if d.row is None or d is gen_doc or not (d.error or d.gen_error):
                continue
            text = ("No se pudo abrir: %s" % d.error) if d.error else \
                ("No se pudieron sacar fotogramas: %s\nClic en el vídeo para reintentar." % d.gen_error)
            if d.row.state.set_error(text) and DEBUG:
                log("estado: %s → %s" % (d.path.name, d.row.state.state_name()))
        docs = [d for d in self.docs if d.row is not None and d.info is not None and d is not gen_doc and not d.gen_error
                and not d.hidden]   # ocultos: sin icono a la vista, sin listdir
        if not docs:
            return
        if self._coverage_busy:
            self._coverage_again = True
            return
        self._coverage_busy = True

        def apply(res):
            self._coverage_busy = False
            for d, (done, total) in res:
                if d.row is None or (self.generator is not None and not self.generator.finished and self.gen_doc is d):
                    continue
                if d.row.state.set_progress(done, total, False) and DEBUG:
                    log("estado: %s → %s" % (d.path.name, d.row.state.state_name()))
            if self._coverage_again:
                self._coverage_again = False
                self._refresh_row_states()
            return False

        def work():   # listdir de cada caché (miles de ficheros) fuera del hilo de la interfaz
            t0 = time.perf_counter()
            res = [(d, self._doc_coverage(d, S)) for d in docs]
            if DEBUG:
                _prof("cobertura(hilo)", time.perf_counter() - t0)
                _count("cobertura_docs", len(docs))
            GLib.idle_add(apply, res)
        threading.Thread(target=work, name="ts-coverage", daemon=True).start()

    @staticmethod
    def _doc_coverage(doc, S):
        """(hechas, total) de las miniaturas del intervalo S que ya están en caché (las imposibles cuentan).
        Un listdir por vídeo; se llama en los eventos de generación, no por captura."""
        ts = plan_timestamps(doc.info.duration, S)
        have = set()
        try:
            for name in os.listdir(str(doc.cache_dir)):
                if name.endswith(".jpg") and name[:-4].isdigit():
                    have.add(int(name[:-4]))
                elif name.endswith(".fail") and name[:-5].isdigit():
                    have.add(int(name[:-5]))
        except OSError:
            pass
        return sum(1 for t in ts if t in have), len(ts)

    def _ensure_ticker(self):
        """Mientras hay generación o corte, refresca la barra cada segundo para que la estimación avance
        aunque no llegue progreso."""
        if self._tick_id is None:
            self._tick_id = GLib.timeout_add(1000, self._tick)

    def _tick(self):
        if self._cut is None and (self.generator is None or self.generator.finished):
            self._tick_id = None
            return False
        self._refresh_status()
        return True

    def _refresh_status(self):
        """Agrupa las peticiones (llegan por cada captura): como mucho seis refrescos por segundo."""
        if self._status_id is None:
            self._status_id = GLib.timeout_add(160, self._refresh_status_now)

    def _refresh_status_now(self):
        t0 = time.perf_counter()
        try:
            return self._refresh_status_impl()
        finally:
            if DEBUG:
                _prof("status", time.perf_counter() - t0)

    def _refresh_status_impl(self):
        self._status_id = None
        doc = self.current
        if doc is None:
            base = "Sin vídeos · Ctrl+O o arrastra aquí" if not self.docs else ""
        elif doc.error:
            base = "No se pudo abrir: %s" % doc.error
        elif doc.info is None:
            base = "Analizando…"
        elif doc.gen_error:
            base = "No se pudieron sacar fotogramas: %s" % doc.gen_error
        else:
            base = "%d capturas · %s · %dx%d" % (len(plan_timestamps(doc.info.duration, self.current_interval())),
                                                 fmt_time(doc.info.duration), doc.info.width, doc.info.height)
        n = len(self._segments())
        if n:
            base += " · %d segmento%s" % (n, "" if n == 1 else "s")
        self._status_base = base
        self.status.set_text(base)
        self._refresh_segpanel()
        # barra: corte > generación (actual o en segundo plano) > nada
        if self._cut is not None:
            if self._cut_pct is None:
                self.progress.set_fraction(0.0)
                self.progress.set_text("Analizando keyframes…" if not self._cut["exact"] else "Preparando…")
            else:
                self.progress.set_fraction(self._cut_pct / 100.0)
                eta = eta_text(self._cut.get("t0"), self._cut_pct, 100 - self._cut_pct)
                if eta and not self._cut.get("eta_logged"):
                    self._cut["eta_logged"] = True
                    log("eta corte:%s" % eta)
                self.progress.set_text("Cortando… %d %%%s" % (self._cut_pct, eta))
            bar = self._cut.get("dlg_bar")
            if bar is not None:   # diálogo de cortar y borrar: mismo progreso
                bar.set_fraction(self.progress.get_fraction())
                bar.set_text(self.progress.get_text())
            self._ensure_ticker()
        elif self.generator is not None and not self.generator.finished and self.generator.total:
            gen = self.generator
            done, total = self._progress
            frac = min(1.0, done / float(total))
            self.progress.set_fraction(frac)
            eta = eta_text(gen.started, done - gen.initial_done, total - done)
            if eta and not gen.eta_logged:
                gen.eta_logged = True
                log("eta:%s" % eta)
            if self.gen_doc is self.current:
                self.progress.set_text("%d / %d capturas%s" % (min(done, total), total, eta))
            else:
                self.progress.set_text("2.º plano: %s · %d %%%s" % (self.gen_doc.path.name if self.gen_doc else "?",
                                                                    int(frac * 100), eta))
            self._ensure_ticker()
        else:
            pending = self._probe_q.qsize() + (1 if self._probing is not None else 0)
            if self._scanning:
                self.progress.set_fraction(0.0)
                self.progress.set_text("Buscando vídeos en %s…" % ("la carpeta" if self._scanning == 1 else "las carpetas"))
            elif pending:
                # mientras se analizan los recién añadidos (ffprobe, de uno en uno) el segundo plano espera: que se vea
                total = max(self._probe_batch, pending)
                self.progress.set_fraction((total - pending) / float(total))
                self.progress.set_text("Analizando %d de %d archivos…" % (total - pending + 1, total))
            else:
                self.progress.set_fraction(1.0 if self.docs else 0.0)
                self.progress.set_text("")
        if DEBUG and self.progress.get_text() != self._bar_logged:
            self._bar_logged = self.progress.get_text()
            log("barra: %s" % (self._bar_logged or "(vacía)"))

    def _flash(self, text):
        """Aviso de operación correcta (toast verde, 3 s)."""
        self.toast.show_ok(text)

    def _update_buttons(self):
        doc = self.current
        self.del_btn.set_sensitive(doc is not None and self._cut is None)
        self.full_btn.set_sensitive(doc is not None and self._cut is None and not doc.deleting)
        has_segs = bool(doc is not None and doc.info is not None and self._segments())
        self.cut_btn.set_sensitive(has_segs or self._cut is not None)
        self.cut_btn.set_label("Cancelar" if self._cut is not None else "Cortar")

    # ---- selección (clic / arrastre sobre teselas → segmentos) ----------------------------------
    def _grid(self):
        S = self.current_interval()
        return plan_timestamps(self.current.info.duration, S), S, self.current.info.duration

    def on_drag_begin(self):
        self._sel_snapshot = self.current.selection.copy() if self.current else None
        self._snap_tiles = set(self.sheet.selected)   # lo marcado al empezar: cada movimiento sólo suma o resta el rango

    def on_drag_apply(self, timestamps, mode):
        doc = self.current
        if doc is None or doc.info is None or self._sel_snapshot is None:
            return
        ts_all, S, dur = self._grid()
        sel = self._sel_snapshot.copy()
        for t in timestamps:
            a, b = Selection.tile_range(t, S, dur)
            if mode:
                sel.add(a, b)
            else:
                sel.remove(a, b)
        doc.selection = sel
        drag = set(timestamps)
        self.sheet.set_selected((self._snap_tiles | drag) if mode else (self._snap_tiles - drag))

    def on_drag_end(self):
        self._sel_snapshot = None
        if self.current is not None:
            self.current.save_selection()
        self._refresh_status()
        self._update_buttons()

    # ---- panel de segmentos -----------------------------------------------------------------------
    def _refresh_segpanel(self, force=False):
        doc = self.current
        if doc is None or doc.info is None:
            self.segpanel.refresh(None, [], [], 1, 0.0, force)
            return
        ts, S, dur = self._grid()
        self.segpanel.refresh(doc, doc.selection.ordered(), ts, S, dur, force)

    def _seg_reorder(self, segs):
        doc = self.current
        if doc is None:
            return
        doc.selection.set_order(segs)
        doc.save_selection()
        self._refresh_segpanel()

    def _seg_delete(self, a, b):
        doc = self.current
        if doc is None or doc.info is None:
            return
        doc.selection.remove(a, b)
        ts, S, dur = self._grid()
        self.sheet.set_selected(doc.selection.tiles(ts, S, dur))
        doc.save_selection()
        self._refresh_status()
        self._update_buttons()

    def _seg_goto(self, t):
        if DEBUG:
            log("seggoto: ir a %s" % fmt_time(t))
        self.sheet.scroll_to_time(t)

    def clear_selection(self):
        doc = self.current
        if doc is None or not doc.selection:
            return
        doc.selection = Selection()
        self.sheet.set_selected(set())
        doc.save_selection()
        self._refresh_status()
        self._update_buttons()
        self._refresh_mark()

    def select_all(self):
        """Ctrl+A: todo el vídeo como un único segmento."""
        doc = self.current
        if doc is None or doc.info is None:
            return
        doc.selection = Selection([(0.0, float(doc.info.duration))])
        ts, S, dur = self._grid()
        self.sheet.set_selected(doc.selection.tiles(ts, S, dur))
        doc.save_selection()
        self._refresh_status()
        self._update_buttons()
        self._refresh_mark()

    # ---- vista ampliada (clic derecho) -----------------------------------------------------------
    def show_preview(self, t):
        doc = self.current
        if doc is None or doc.info is None:
            return
        self._preview_key = (doc, t)
        self._pause_background()
        self._warm_gst()   # si aún no se ha precargado, ahora: lo normal es que play llegue después
        pb = None   # la caché del mosaico guarda superficies cairo; la vista ampliada carga el JPEG (es uno)
        thumb = doc.cache_dir / ("%d.jpg" % t)
        if pb is None and thumb.exists():
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file(str(thumb))
            except GLib.Error:
                pb = None
        self._stop_player()
        self.layer.show_still(pb, t, doc.path.name)
        self.layer.set_duration(doc.info.duration)
        self.layer.set_position(t)
        self.layer.show()
        self._refresh_mark()
        if DEBUG:
            log("ampliada: %s (%s)" % (fmt_time(t), "marcada" if self.layer.marked else "sin marcar"))
        full = doc.cache_dir / "full" / ("%d.jpg" % t)
        if full.exists():
            self._preview_loaded(doc, t, full)
        # vecinos primero (quedan debajo en la pila) y el actual el último: LIFO lo atiende antes
        ts = self.sheet.ts
        neighbours = [ts[i] for i in (self._tile_index(t) - 1, self._tile_index(t) + 1) if 0 <= i < len(ts)]
        self._request_full(doc, neighbours + ([] if full.exists() else [t]))

    def _tile_index(self, t):
        ts = self.sheet.ts
        if not ts:
            return -1
        return min(range(len(ts)), key=lambda k: abs(ts[k] - t))

    def _preview_step(self, delta):
        """Flechas izquierda/derecha (o rueda) en la vista ampliada: fotograma anterior/siguiente."""
        if self._preview_key is None or not self.sheet.ts:
            return
        doc, t = self._preview_key
        ts = self.sheet.ts
        i = max(0, min(len(ts) - 1, self._tile_index(t) + delta))
        if ts and ts[i] != t:
            self.show_preview(ts[i])

    def _preview_nudge(self, n, fine):
        """Flechas o rueda en la vista ampliada: n pasos (negativo = atrás). Con el vídeo cargado, cada paso es un
        intervalo (`fine`, Ctrl: un segundo), encadenados sobre el último seek pedido aunque el reproductor aún no
        lo refleje. En modo fotograma, tesela anterior/siguiente; con Ctrl, el vídeo en pausa en t ± n s (como el
        scrubber), que es el único modo de ver un instante que no es de la rejilla."""
        if self._preview_key is None:
            return
        dur = self.layer.duration
        if self._player_doc is not None:
            step = 1.0 if fine else self.current_interval()
            if self._seek_at is not None and time.monotonic() - self._seek_at[1] < 0.5:
                pos = self._seek_at[0]
            else:
                pos = self.player.position()
                if pos is None:
                    pos = self.layer.scale.get_value()
            self.preview_seek(max(0.0, min(dur, pos + n * step)))
        elif fine:
            self.preview_seek(max(0.0, min(dur, self._preview_key[1] + n)))
        else:
            self._preview_step(n)

    def _preview_wheel(self, event):
        """Rueda sobre la vista ampliada: abajo avanza y arriba retrocede, igual que las flechas (Ctrl: ±1 s)."""
        n = self._wheel_steps(event, "preview")
        if n:
            self._preview_nudge(-n, bool(event.state & Gdk.ModifierType.CONTROL_MASK))
        return True

    def _refresh_mark(self):
        """Recuadro rojo de la vista ampliada: en modo fotograma, si la tesela está marcada (como en el mosaico);
        con el vídeo cargado, si el instante actual (o el último seek pedido, hasta que el reproductor lo refleje)
        cae dentro de un segmento."""
        if self._preview_key is None or not self.layer.get_visible():
            return
        doc, t = self._preview_key
        if self._player_doc is not None:
            if self._seek_at is not None and time.monotonic() - self._seek_at[1] < 0.5:
                pos = self._seek_at[0]
            else:
                pos = self.player.position()
                if pos is None:
                    pos = self.layer.scale.get_value()
            on = doc.selection.contains(pos)
        else:
            on = t in self.sheet.selected
        if self.layer.set_marked(on) and DEBUG:
            log("marca: %s" % ("dentro de un segmento" if on else "fuera"))

    def _request_full(self, doc, timestamps):
        with self._full_lock:
            for t in timestamps:
                job = (doc, t)
                if job in self._full_jobs:
                    self._full_jobs.remove(job)
                self._full_jobs.append(job)
            del self._full_jobs[:-12]   # como mucho una docena pendientes
            self._full_lock.notify()

    def _full_loop(self):
        while True:
            with self._full_lock:
                while not self._full_jobs:
                    self._full_lock.wait()
                doc, t = self._full_jobs.pop()
            full = doc.cache_dir / "full" / ("%d.jpg" % t)
            if full.exists():
                GLib.idle_add(self._preview_loaded, doc, t, full)
                continue
            self._extract_full(doc, t, full)

    def _extract_full(self, doc, t, full):
        """Fotograma a resolución nativa, en un ffmpeg aparte (0,1–0,3 s); queda en la caché (full/)."""
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        tmp = full.with_name(".%d.tmp.jpg" % t)
        cmd = FFMPEG + FULL_DEC + ["-ss", str(t), "-i", str(doc.path), "-map", "0:v:0", "-an", "-sn", "-dn",
                                   "-frames:v", "1", "-q:v", "3", "-f", "image2", "-update", "1", "-y", str(tmp)]
        try:
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=FF_ENV,
                               timeout=60, preexec_fn=PREEXEC)
            ok = r.returncode == 0 and tmp.exists()
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        if ok:
            os.replace(str(tmp), str(full))
            GLib.idle_add(self._preview_loaded, doc, t, full)
        else:
            try:
                tmp.unlink()
            except OSError:
                pass

    def _preview_loaded(self, doc, t, full):
        if self._preview_key != (doc, t) or not self.layer.get_visible():
            return False
        alloc = self.layer.stack.get_allocation()
        w, h = max(64, alloc.width), max(64, alloc.height)
        try:
            # decodificado ya al tamaño en que se va a ver (libjpeg escala por DCT: barato y nítido)
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(full), w, h, True)
        except GLib.Error:
            return False
        self.layer.update_still(pb, t, doc.path.name)
        return False

    def hide_preview(self):
        self._preview_key = None
        self._stop_player()
        if self.layer.get_visible():
            self.layer.hide()
            GLib.idle_add(self._start_next_background)

    # ---- reproducción (GStreamer) dentro de la vista ampliada -----------------------------------
    def _warm_gst(self):
        if not self._gst_warm and self.player is None:
            self._gst_warm = True
            threading.Thread(target=gst_warmup, name="ts-gst", daemon=True).start()
        return False

    def _ensure_player(self):
        if self.player is not None:
            return self.player
        if not HAVE_GST:
            self._flash("Sin GStreamer: sudo apt install gir1.2-gstreamer-1.0 gstreamer1.0-gtk3 gstreamer1.0-libav")
            return None
        t0 = time.perf_counter()
        try:
            audio = os.environ.get("THUMBSHEET_AUDIO", "1") not in ("0", "off", "no")
            self.player = Player(audio=audio)
        except RuntimeError as e:
            self._flash(str(e))
            return None
        t1 = time.perf_counter()
        self.layer.attach_video(self.player.widget)
        if DEBUG:
            log("player: crear %.0f ms, incrustar %.0f ms" % (1000 * (t1 - t0), 1000 * (time.perf_counter() - t1)))
        self.player.on_state = self._on_player_state
        self.player.on_eos = self._on_player_eos
        self.player.on_error = self._on_player_error
        return self.player

    def _start_player(self, start_s, play):
        doc = self._preview_key[0] if self._preview_key else None
        if doc is None:
            return None
        p = self._ensure_player()
        if p is None:
            return None
        p.load(doc.path, start_s, play)
        self._player_doc = doc
        self._seek_at = None
        self.layer.show_video(doc.info.aspect if doc.info is not None else None)
        self.layer.set_position(start_s)
        self._refresh_mark()
        if self._tick_id is None:
            self._tick_id = GLib.timeout_add(100, self._player_tick)
        log("player: carga %s desde %.1f (%s)" % (doc.path.name, start_s, "play" if play else "pausa"))
        return p

    def _stop_player(self):
        if self.player is not None and self._player_doc is not None:
            self.player.stop()
        self._player_doc = None
        self._seek_at = None
        for attr in ("_tick_id", "_seek_id"):
            sid = getattr(self, attr)
            if sid:
                GLib.source_remove(sid)
                setattr(self, attr, None)
        self.layer.set_playing(False)

    def toggle_play(self):
        if self._preview_key is None:
            return
        if self._player_doc is None:
            self._start_player(self.layer.scale.get_value(), True)
        elif self.player.is_playing():
            self.player.pause()
            log("player: pausa en %.2f" % (self.player.position() or -1))
        else:
            self.player.play()

    def preview_seek(self, s):
        """Barra de progreso. En modo fotograma arranca el vídeo EN PAUSA en ese punto (scrubber); con el
        vídeo cargado, seek exacto con un pequeño debounce mientras se arrastra."""
        if self._preview_key is None:
            return
        if self._player_doc is None:
            self._start_player(s, False)
            return
        self._seek_at = (s, time.monotonic())
        if self._seek_id:
            GLib.source_remove(self._seek_id)
        self._seek_id = GLib.timeout_add(60, self._do_seek, s)

    def _do_seek(self, s):
        self._seek_id = None
        if self._player_doc is not None:
            self.player.seek(s)
            self.layer.set_position(s)
            self._refresh_mark()
            log("player: seek a %.2f" % s)
        return False

    def _player_tick(self):
        if self._player_doc is None:
            self._tick_id = None
            return False
        pos = self.player.position()
        if pos is not None:
            self.layer.set_position(pos)
        self._refresh_mark()
        return True

    def _on_player_state(self, playing):
        self.layer.set_playing(playing)
        if playing:
            log("player: reproduciendo")

    def _on_player_eos(self):
        self.layer.set_playing(False)
        log("player: fin del vídeo")

    def _on_player_error(self, message):
        self._flash("No se pudo reproducir: %s" % message)
        self._stop_player()
        self.layer.stack.set_visible_child_name("still")
        self._refresh_mark()

    # ---- cortar y unir ----------------------------------------------------------------------------
    def on_cut(self, *_):
        if self._cut is not None:
            self._cut_cancel()
            return
        doc = self.current
        segs = self._segments()
        if not doc or not doc.info or not segs:
            return
        out = cut_output_path(doc.path)
        self._own_outputs.add(out)   # si la carpeta está vigilada, el resultado no se carga como vídeo nuevo
        total = sum(b - a for a, b in segs)
        dlg = Gtk.Dialog(title="Cortar y unir", transient_for=self, modal=True)
        cancel_btn = dlg.add_button("_Cancelar", Gtk.ResponseType.CANCEL)
        only_btn = dlg.add_button("Cor_tar", Gtk.ResponseType.ACCEPT)
        del_btn = dlg.add_button(CUT_DEL_LABEL, RESP_CUT_DELETE)
        del_btn.set_tooltip_text("Corta y, si el resultado es correcto, borra el vídeo original del disco (sin papelera).\n"
                                 "Con el ratón hay que pulsarlo dos veces seguidas, para evitar borrados accidentales.\n"
                                 "Intro lo confirma directamente.")
        dlg.set_default_response(RESP_CUT_DELETE)
        armed = [0]   # id del temporizador que desarma "cortar y borrar" (0 = sin armar)
        # Intro = cortar y borrar el original, sin armar: abrir el diálogo (C o el botón Cortar) ya fue el primer
        # paso. Con Tab sobre Cancelar o Cortar, Intro pulsa ese botón, como siempre. Mientras corta, Intro no hace
        # nada (un segundo Intro no debe cancelar el corte); Esc o el botón Cancelar sí cancelan.
        phase = {"by_key": False, "running": False}

        def on_key(d, ev):
            if ev.keyval not in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
                return False
            if phase["running"]:
                return True
            if d.get_focus() in (cancel_btn, only_btn):
                return False
            phase["by_key"] = True
            log("cortar: Intro = cortar y borrar original")
            d.response(RESP_CUT_DELETE)
            return True
        dlg.connect("key-press-event", on_key)

        def disarm():
            armed[0] = 0
            del_btn.set_label(CUT_DEL_LABEL)
            del_btn.get_style_context().remove_class("destructive-action")
            return False

        def on_response(d, rid):
            if rid != RESP_CUT_DELETE or phase["by_key"]:
                return
            if armed[0]:                                  # segunda pulsación: la respuesta sigue su curso
                GLib.source_remove(armed[0])
                armed[0] = 0
                return
            armed[0] = GLib.timeout_add(CUT_DEL_ARM_MS, disarm)   # primera pulsación: sólo arma el botón
            del_btn.set_label("Confirmar: cortar y _borrar")
            del_btn.get_style_context().add_class("destructive-action")
            d.stop_emission_by_name("response")
        dlg.connect("response", on_response)
        box = dlg.get_content_area()
        box.set_spacing(8)
        for w in (box,):
            w.set_margin_start(14)
            w.set_margin_end(14)
            w.set_margin_top(10)
            w.set_margin_bottom(6)
        head = Gtk.Label()
        head.set_xalign(0.0)
        head.set_markup("<b>%d segmento%s · %s</b>  →  %s" % (
            len(segs), "" if len(segs) == 1 else "s", fmt_time(total), GLib.markup_escape_text(out.name)))
        box.pack_start(head, False, False, 0)
        lossless = Gtk.RadioButton.new_with_label(None, "Sin pérdida: copia los streams tal cual. Los cortes se mueven hacia fuera al "
                                                        "keyframe más cercano (no se pierde nada; puede sobrar algo). Rápido.")
        exact = Gtk.RadioButton.new_with_label_from_widget(lossless, "Exacto al fotograma: recodifica (H.264 CRF 20 + AAC). Lento, "
                                                                     "recomprime.")
        for rb in (lossless, exact):
            rb.get_child().set_line_wrap(True)
            rb.get_child().set_xalign(0.0)
            box.pack_start(rb, False, False, 0)
        if self.settings.get("cut_mode") == "exact":
            exact.set_active(True)
        content_widgets = [head, lossless, exact]   # lo que se oculta en modo progreso (el área de botones NO: cuelga del mismo box)
        if out.exists():
            warn = Gtk.Label(label="Ya existe %s: se sobrescribirá." % out.name)
            warn.set_xalign(0.0)
            box.pack_start(warn, False, False, 0)
            content_widgets.append(warn)
        keys = Gtk.Label(label="Intro: cortar y borrar el original · Alt+T: sólo cortar · Esc: cancelar")
        keys.set_xalign(0.0)
        keys.get_style_context().add_class("dim-label")
        box.pack_start(keys, False, False, 0)
        content_widgets.append(keys)
        box.show_all()
        log("cortar: diálogo %s" % doc.path.name)
        resp = dlg.run()
        use_exact = exact.get_active()
        if armed[0]:
            GLib.source_remove(armed[0])
        if resp not in (Gtk.ResponseType.ACCEPT, RESP_CUT_DELETE):
            dlg.destroy()
            return
        self.settings["cut_mode"] = "exact" if use_exact else "copy"
        self._cut = {"doc": doc, "segments": list(segs), "out": out, "exact": use_exact, "proc": None,
                     "cancel": threading.Event(), "expected": total, "delete": resp == RESP_CUT_DELETE}
        if resp == RESP_CUT_DELETE:
            # el diálogo sigue abierto mientras dura el corte (se puede cancelar); al empezar el borrado se cierra
            for w in content_widgets:
                w.hide()
            busy = Gtk.Label(label="Cortando %s…" % doc.path.name)
            busy.set_xalign(0.0)
            busy.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            bar = Gtk.ProgressBar()
            bar.set_show_text(True)
            box.pack_start(busy, False, False, 0)
            box.pack_start(bar, False, False, 4)
            busy.show()
            bar.show()
            for rid in (Gtk.ResponseType.ACCEPT, RESP_CUT_DELETE):
                dlg.get_widget_for_response(rid).hide()
            phase["running"] = True   # Intro ya no hace nada; Esc / Cancelar cancelan
            dlg.connect("response", lambda d, r: self._cut_cancel())
            self._cut["dlg"], self._cut["dlg_bar"] = dlg, bar
        else:
            dlg.destroy()
        self._cut_pct = None
        self._update_buttons()
        self._refresh_status()
        self._pause_background()
        threading.Thread(target=self._cut_worker, args=(self._cut,), name="ts-cut", daemon=True).start()

    def _cut_worker(self, job):
        doc, segs, out = job["doc"], job["segments"], job["out"]
        try:
            if job["exact"]:
                final = segs
            else:
                final = expand_to_keyframes(segs, doc.path, doc.info.duration, doc.info.gop)
            if job["cancel"].is_set():
                return
            job["final"] = final
            job["expected"] = sum(b - a for a, b in final)
            list_path = doc.cache_dir / ".cut-list.txt"
            tmp_out = out.with_name(out.name + ".part")
            if job["exact"]:
                cmd = cut_command_exact(doc.path, final, out, tmp_out, doc.info.has_audio)
            else:
                write_concat_list(list_path, doc.path, final)
                cmd = cut_command_copy(list_path, out, tmp_out)
            log("cut:", " ".join(cmd))
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=FF_ENV, preexec_fn=PREEXEC)
            job["proc"] = p
            if job["cancel"].is_set():
                p.terminate()
            for raw in p.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    try:
                        done = int(line.split("=", 1)[1]) / 1e6
                    except ValueError:
                        continue
                    GLib.idle_add(self._cut_progress, job, done)
            err = p.stderr.read().decode("utf-8", "replace").strip()
            p.wait()
            if list_path.exists():
                try:
                    list_path.unlink()
                except OSError:
                    pass
            if job["cancel"].is_set() or p.returncode != 0 or not tmp_out.exists():
                try:
                    tmp_out.unlink()
                except OSError:
                    pass
                if job["cancel"].is_set():
                    GLib.idle_add(self._cut_done, job, None, "cancelado")
                else:
                    GLib.idle_add(self._cut_done, job, None, err[-400:] or ("ffmpeg terminó con código %s" % p.returncode))
                return
            os.replace(str(tmp_out), str(out))
            dur = None
            try:
                r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=FF_ENV, timeout=60)
                dur = float(r.stdout.decode().strip() or "nan")
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            GLib.idle_add(self._cut_done, job, dur, None)
        except Exception as e:  # noqa: BLE001
            GLib.idle_add(self._cut_done, job, None, str(e))

    def _cut_progress(self, job, done):
        if self._cut is job and job["expected"] > 0:
            if job.get("t0") is None:
                job["t0"] = time.time()   # arranca con el primer progreso: la fase de keyframes no cuenta
            self._cut_pct = max(0, min(100, int(100 * done / job["expected"])))
            self._refresh_status()
        return False

    def _cut_cancel(self):
        job = self._cut
        if job is None:
            return
        job["cancel"].set()
        if job["proc"] is not None:
            try:
                job["proc"].terminate()
            except OSError:
                pass
        self.progress.set_text("Cancelando…")
        if job.get("dlg_bar") is not None:
            job["dlg_bar"].set_text("Cancelando…")

    def _cut_done(self, job, duration, error):
        if self._cut is not job:
            return False
        self._cut = None
        self._cut_pct = None
        if job.get("dlg") is not None:
            job["dlg"].destroy()   # terminó el corte: lo que sigue (borrar el original) va en segundo plano
        self._update_buttons()
        GLib.idle_add(self._start_next_background)
        self._refresh_status()
        out = job["out"]
        if error == "cancelado":
            self._flash("Corte cancelado")
            log("cut: cancelado")
        elif error:
            self._error("No se pudo cortar: %s" % out.name, error)
            log("cut: error", error)
        else:
            added = job["expected"] - sum(b - a for a, b in job["segments"])
            extra = " (+%.1f s por keyframes)" % added if added > 0.05 else ""
            txt = "Cortado: %s · %d segmento%s · %s%s" % (out.name, len(job["final"]), "" if len(job["final"]) == 1 else "s",
                                                          fmt_time(duration if duration is not None else job["expected"]), extra)
            log("cut: hecho %s dur=%s esperado=%.2f" % (out.name, duration, job["expected"]))
            self._refresh_output(out)
            if duration is not None and abs(duration - job["expected"]) > max(2.0, 0.03 * job["expected"]):
                self._error(txt, "La duración del resultado (%s) no coincide con la esperada (%s): revisa el fichero.%s" % (
                    fmt_time(duration), fmt_time(job["expected"]), " El original se conserva." if job["delete"] else ""))
            elif job["delete"]:
                self._delete_file(job["doc"], ok_text="%s · original borrado" % txt,
                                  err_text="%s · no se pudo borrar el original" % txt,
                                  ok_log="cut: original borrado %s" % job["doc"].path.name)
            else:
                self._flash(txt)
        return False

    def _refresh_output(self, out):
        """El fichero cortado NO se añade al panel. Pero si ya estaba cargado (se ha sobrescrito), se quita y
        se vuelve a añadir para que se sondee de nuevo: tamaño y fecha nuevos, otra caché."""
        old = next((d for d in self.docs if d.path == out), None)
        if old is None:
            return
        if self.generator is not None and self.gen_doc is old:
            self.generator.cancel()
            self.generator = None
        self._remove_doc(old, show_next=False)
        self.add_paths([str(out)])

    # ---- eliminar -------------------------------------------------------------------------------
    def on_delete(self, *_):
        doc = self.current
        if doc is None or doc.deleting or self._cut is not None:
            return
        try:
            size = human_size(doc.path.stat().st_size)
        except OSError:
            size = "?"
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                                buttons=Gtk.ButtonsType.NONE, text="¿Eliminar el archivo definitivamente?")
        dlg.format_secondary_text("%s\n%s\n\nSe borra del disco directamente, sin papelera: no se puede deshacer." % (doc.path, size))
        dlg.add_button("_Cancelar", Gtk.ResponseType.CANCEL)
        btn = dlg.add_button("_Eliminar", Gtk.ResponseType.ACCEPT)
        btn.get_style_context().add_class("destructive-action")
        # Intro acepta: además de la respuesta por defecto, el foco inicial va a «Eliminar» (GTK lo pondría en
        # el primer botón, «Cancelar», y Intro activa el botón con foco, no el de por defecto). Esc cancela.
        dlg.set_default_response(Gtk.ResponseType.ACCEPT)
        btn.grab_focus()
        log("eliminar: diálogo %s" % doc.path.name)
        resp = dlg.run()
        dlg.destroy()
        if resp != Gtk.ResponseType.ACCEPT:
            log("eliminar: cancelado")
            return
        self._delete_file(doc)

    def _delete_file(self, doc, ok_text=None, err_text=None, ok_log=None):
        """Borra el vídeo de `doc` del disco (directamente, sin papelera) y su caché, en un hilo aparte: el
        borrado del fichero y de miles de miniaturas puede tardar segundos en un disco lento y no debe
        congelar la ventana. La fila queda como «Eliminando…» e insensible y, si era el vídeo a la vista, se
        pasa ya al siguiente. Al terminar sale el toast y la fila desaparece; si falló, vuelve a estar
        disponible y el toast rojo dice por qué."""
        if doc.deleting:
            return
        doc.deleting = True
        log("eliminando: %s" % doc.path.name)
        if doc is self.current:
            self.hide_preview()
        if self.generator is not None and self.gen_doc is doc:
            self.generator.cancel()
            self.generator = None
        if doc.row is not None:
            doc.row.set_sensitive(False)
            doc.row.sub_label.set_markup(sub_markup("Eliminando…"))
        if doc is self.current:
            self._show_neighbor(doc)
        self._pause_background()
        ok_text = ok_text or ("Eliminado: %s" % doc.path.name)
        err_text = err_text or "No se pudo eliminar el archivo"

        def work():
            # que ningún ffmpeg nuestro tenga el vídeo abierto al borrarlo (ya tienen el SIGTERM; aquí sólo se espera):
            # en un montaje remoto (sftp/sshfs) borrar un fichero abierto puede fallar con «recurso ocupado»
            if not Generator.quiesce(doc.path, 6.0):
                log("borrar: %s: algún ffmpeg no ha terminado a tiempo" % doc.path.name)
            err = remove_with_retry(doc.path, 8.0)
            if err is None and doc.cache_dir:
                shutil.rmtree(str(doc.cache_dir), ignore_errors=True)
            GLib.idle_add(self._delete_done, doc, err, ok_text, err_text, ok_log)
        # no daemon: si se cierra la app a medias, el borrado confirmado termina igualmente
        threading.Thread(target=work, name="ts-delete", daemon=False).start()

    # ---- Full: el vídeo entero vale tal cual ----------------------------------------------------
    def on_full(self, *_):
        doc = self.current
        if doc is None or doc.deleting or self._cut is not None:
            return
        target = full_output_path(doc.path)
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.NONE, text="¿Dar por bueno el vídeo entero?")
        dlg.format_secondary_text("%s\n→ %s\n\nSólo cambia el nombre, como si se hubiera cortado de principio a fin: "
                                  "el contenido no se toca y el vídeo sale de la lista." % (doc.path, target.name))
        dlg.add_button("_Cancelar", Gtk.ResponseType.CANCEL)
        btn = dlg.add_button("_Renombrar", Gtk.ResponseType.ACCEPT)
        btn.get_style_context().add_class("suggested-action")
        dlg.set_default_response(Gtk.ResponseType.ACCEPT)   # Intro acepta (ver on_delete), Esc cancela
        btn.grab_focus()
        resp = dlg.run()
        dlg.destroy()
        if resp != Gtk.ResponseType.ACCEPT:
            return
        self._rename_full(doc, target)

    def _rename_full(self, doc, target):
        """Renombra el vídeo de `doc` a su nombre «cortado» en un hilo (misma espera y reintentos que Eliminar: que
        ningún ffmpeg lo tenga abierto, «ocupado» en montajes remotos) y lo quita de la lista. La caché de
        miniaturas se mueve a la clave del nombre nuevo (tamaño y fecha no cambian), así reabrirlo es inmediato."""
        if doc.deleting:
            return
        doc.deleting = True          # misma marca que Eliminar: fila insensible y fuera del segundo plano mientras tanto
        self._own_outputs.add(target)   # si la carpeta está vigilada, el renombrado no se carga como vídeo nuevo
        log("full: %s → %s" % (doc.path.name, target.name))
        if doc is self.current:
            self.hide_preview()
        if self.generator is not None and self.gen_doc is doc:
            self.generator.cancel()
            self.generator = None
        if doc.row is not None:
            doc.row.set_sensitive(False)
            doc.row.sub_label.set_markup(sub_markup("Renombrando…"))
        if doc is self.current:
            self._show_neighbor(doc)
        self._pause_background()
        self._update_buttons()

        def work():
            if not Generator.quiesce(doc.path, 6.0):
                log("full: %s: algún ffmpeg no ha terminado a tiempo" % doc.path.name)
            err = rename_with_retry(doc.path, target, 8.0)
            if err is None and doc.info is not None and doc.cache_dir is not None:
                try:
                    st = target.stat()
                    h = hashlib.sha1()
                    h.update(("%s|%d|%d|%d" % (target.resolve(), st.st_size, st.st_mtime_ns, THUMB_MAX)).encode("utf-8"))
                    new_dir = CACHE_ROOT / h.hexdigest()[:16]
                    if not new_dir.exists():
                        os.rename(str(doc.cache_dir), str(new_dir))
                        log("full: caché movida a %s" % new_dir.name)
                except OSError as e:
                    log("full: no se pudo mover la caché: %s" % e)
            GLib.idle_add(self._full_done, doc, target, err)
        threading.Thread(target=work, name="ts-full", daemon=False).start()

    def _full_done(self, doc, target, err):
        doc.deleting = False
        GLib.idle_add(self._start_next_background)
        if err:
            if doc.row is not None:
                doc.row.set_sensitive(True)
                doc.row.sub_label.set_markup(sub_markup(doc.subtitle))
            self._update_buttons()
            self._error("No se pudo renombrar el archivo", err)
            return False
        log("full: renombrado %s" % target.name)
        self._remove_doc(doc)
        self._flash("Renombrado: %s" % target.name)
        return False

    def _show_neighbor(self, doc):
        """Pasa a mostrar el vecino de `doc` (el siguiente vídeo a la vista; si no hay, el anterior) sin quitar su fila."""
        idx = self.docs.index(doc) if doc in self.docs else len(self.docs)
        nxt = pick_next(self.docs, idx + 1, lambda d: d is not doc and self._usable(d))
        if nxt is None:
            self.show_document(None)
        else:
            self.listbox.select_row(nxt.row)

    def _delete_done(self, doc, err, ok_text, err_text, ok_log):
        doc.deleting = False
        GLib.idle_add(self._start_next_background)
        if err:
            if doc.row is not None:
                doc.row.set_sensitive(True)
                doc.row.sub_label.set_markup(sub_markup(doc.subtitle))
            self._error(err_text, err)
            return False
        log(ok_log or ("eliminado: %s" % doc.path.name))
        self._remove_doc(doc)
        self._flash(ok_text)
        return False

    def _remove_doc(self, doc, show_next=True):
        """Quita `doc` del panel y de su carpeta (si era el último a la vista de una subcarpeta, la fila de ésta se
        esconde). Si era el que estaba a la vista, abre el siguiente vídeo a la vista, saltando filas de carpeta y
        ocultos (si no hay ninguno después, el anterior); con `show_next=False` deja el mosaico vacío."""
        if doc not in self.docs:
            return
        idx = self.docs.index(doc)
        del self.docs[idx]
        del self._keys[idx]
        self._by_path.pop(doc.path, None)
        folder = doc.folder
        row, doc.row = doc.row, None
        was_current = self.current is doc
        if was_current:
            self.current = None
        if row is not None:
            self.listbox.remove(row)
        if folder is not None:
            folder.docs.remove(doc)
            self._refresh_folder(folder.root or folder)
        self._update_count()
        if DEBUG:
            self._log_panel()
        self._geometry_soon()
        if not was_current:
            return
        nxt = pick_next(self.docs, idx, self._usable) if show_next else None
        if nxt is not None:
            self.listbox.select_row(nxt.row)
        else:
            self._load_current()

    def _error(self, text, secondary=""):
        """Aviso de error (toast rojo permanente, texto copiable)."""
        self.toast.show_error(("%s\n%s" % (text, secondary)) if secondary else text)

    # ---- controles --------------------------------------------------------------------------------
    def current_interval(self):
        i = int(round(self.interval_scale.get_value()))
        return INTERVALS[max(0, min(len(INTERVALS) - 1, i))]

    def on_interval_changed(self, scale):
        v = scale.get_value()
        if abs(v - round(v)) > 1e-6:
            scale.set_value(round(v))   # re-entra ya en una posición entera (clic en la pista, arrastre fino...)
            return
        if scale not in self._slider_drag:
            self._apply_slider(scale)

    def on_tile_changed(self, scale):
        if scale not in self._slider_drag:
            self._apply_slider(scale)

    def _slider_press(self, scale, event):
        self._slider_drag.add(scale)
        return False

    def _slider_release(self, scale, event):
        if scale in self._slider_drag:
            self._slider_drag.discard(scale)
            self._apply_slider(scale)
        return False

    def _apply_slider(self, scale):
        if scale is self.interval_scale:
            if self._regen_id:
                GLib.source_remove(self._regen_id)
            self._regen_id = GLib.timeout_add(self.REGEN_DEBOUNCE_MS, self.regenerate)
        else:
            self.sheet.set_cols(int(round(scale.get_value())))

    def _wheel_steps(self, event, key, sideways=False):
        """Pasos de rueda (+1 arriba / -1 abajo); con scroll suave acumula hasta completar un paso. Con
        `sideways`, el desplazamiento horizontal cuenta como vertical (izquierda = arriba): hay sistemas que
        convierten Mayús+rueda en scroll horizontal."""
        if event.direction == Gdk.ScrollDirection.UP or (sideways and event.direction == Gdk.ScrollDirection.LEFT):
            return 1
        if event.direction == Gdk.ScrollDirection.DOWN or (sideways and event.direction == Gdk.ScrollDirection.RIGHT):
            return -1
        if event.direction == Gdk.ScrollDirection.SMOOTH:
            _, dx, dy = event.get_scroll_deltas()
            if sideways and not dy:
                dy = dx
            acc = self._wheel_acc.get(key, 0.0) + dy
            if abs(acc) >= 1.0:
                self._wheel_acc[key] = 0.0
                return -1 if acc > 0 else 1
            self._wheel_acc[key] = acc
        return 0

    def _slider_scroll(self, scale, event, step):
        n = self._wheel_steps(event, scale)
        if n:
            scale.set_value(scale.get_value() + n * step)
        return True

    def _marks_changed(self):
        self.scroller.get_vscrollbar().queue_draw()

    def _draw_scroll_marks(self, vb, cr):
        """Tras el dibujado normal de la barra de scroll: una marca roja en el canal por cada tramo de filas con
        teselas marcadas, en su sitio proporcional al contenido (las bandas vienen ya calculadas del mosaico)."""
        bands = self.sheet.mark_bands()
        if DEBUG and len(bands) != self._marks_logged:
            self._marks_logged = len(bands)
            log("marcas: %d bandas" % len(bands))
        if not bands:
            return False
        adj = self.scroller.get_vadjustment()
        upper = adj.get_upper()
        if upper <= 0:
            return False
        rr = vb.get_range_rect()   # el canal, en coordenadas de la barra
        cr.set_source_rgba(0.93, 0.16, 0.16, 0.9)
        for y0, y1 in bands:
            a = rr.y + rr.height * y0 / upper
            b = rr.y + rr.height * y1 / upper
            cr.rectangle(rr.x + 3, a, max(1, rr.width - 6), max(2.0, b - a))
        cr.fill()
        return False

    def on_scroll(self, widget, event):
        """Rueda sobre el mosaico. Sin Ctrl: desplazar (GTK; con «Snap scroll», filas enteras). Zoom:
        Ctrl+rueda: tamaño de las teselas (arriba, más grandes). Ctrl+Mayús+rueda:
        intervalo (arriba acerca: intervalo más corto, más teselas; abajo aleja). Cada paso de rueda mueve un
        paso el slider correspondiente, con lo mismo que hace el slider (el intervalo se aplica con la misma
        espera antes de regenerar, y la zona del vídeo a la vista se conserva)."""
        if not event.state & Gdk.ModifierType.CONTROL_MASK:
            if not self.sheet.snap:
                return False
            # «Snap scroll»: la rueda mueve filas enteras (GTK desplazaría píxeles sueltos)
            n = self._wheel_steps(event, "snap")
            if n:
                self.sheet.snap_scroll(-n)   # rueda arriba (+1) = subir
            return True
        if event.state & Gdk.ModifierType.SHIFT_MASK:
            n = self._wheel_steps(event, "interval", sideways=True)
            if n:
                i = int(round(self.interval_scale.get_value()))
                j = max(0, min(len(INTERVALS) - 1, i - n))
                if j != i:
                    self.interval_scale.set_value(j)
            return True
        n = self._wheel_steps(event, "sheet")
        if n:
            self.tile_scale.set_value(self.tile_scale.get_value() - n)
        return True

    def on_key(self, widget, event):
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
        if event.keyval == Gdk.KEY_Escape:
            if self.toast.visible_error:
                self.toast.dismiss()
            elif self.layer.get_visible():
                self.hide_preview()
            else:
                self.clear_selection()
            return True
        if self.layer.get_visible():
            if event.keyval == Gdk.KEY_space:
                self.toggle_play()
                return True
            if event.keyval in (Gdk.KEY_Left, Gdk.KEY_Right):
                # ±intervalo (vídeo) o tesela anterior/siguiente (fotograma); con Ctrl, ±1 s
                self._preview_nudge(-1 if event.keyval == Gdk.KEY_Left else 1, bool(ctrl))
                return True
            if event.keyval in (Gdk.KEY_Home, Gdk.KEY_End):
                if self._player_doc is not None:
                    self.preview_seek(0.0 if event.keyval == Gdk.KEY_Home else max(0.0, self.layer.duration - 0.5))
                else:
                    self._preview_step(-10 ** 9 if event.keyval == Gdk.KEY_Home else 10 ** 9)
                return True
        if ctrl and event.keyval in (Gdk.KEY_q, Gdk.KEY_w):
            self.destroy()
            return True
        if ctrl and event.keyval == Gdk.KEY_o:
            self.add_paths(choose_videos(self))
            return True
        if ctrl and event.keyval == Gdk.KEY_O:   # Ctrl+Mayús+O
            self.add_paths(choose_folders(self))
            return True
        if ctrl and event.keyval == Gdk.KEY_a:
            self.select_all()
            return True
        if ctrl and event.keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self.tile_scale.set_value(self.tile_scale.get_value() - 1)
            return True
        if ctrl and event.keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self.tile_scale.set_value(self.tile_scale.get_value() + 1)
            return True
        # teclas sueltas (sin Ctrl/Alt/Super: Ctrl+C sigue copiando en el aviso de error). No hay campos de texto
        # en la ventana, así que no le quitan nada a nadie. Sólo actúan si el botón correspondiente lo haría.
        plain = not event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK | Gdk.ModifierType.SUPER_MASK)
        if plain and event.keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):   # Supr = Eliminar (con su diálogo)
            if self.del_btn.get_sensitive():
                self.on_delete()
            return True
        if plain and event.keyval in (Gdk.KEY_c, Gdk.KEY_C):   # C = Cortar (con su diálogo); nunca cancela un corte en marcha
            if self._cut is None and self.cut_btn.get_sensitive():
                self.on_cut()
            return True
        return False

    def on_drag_data(self, widget, context, x, y, data, info, time_):
        paths = []
        for uri in data.get_uris() or []:
            try:
                paths.append(GLib.filename_from_uri(uri)[0])
            except GLib.Error:
                # sftp://, smb://… desde el gestor de archivos: su ruta local vía gvfs (/run/user/…/gvfs/…), si está montado
                local = Gio.File.new_for_uri(uri).get_path()
                if local:
                    paths.append(local)
        self.add_paths(paths)
        Gtk.drag_finish(context, bool(paths), False, time_)

    def on_window_state(self, widget, event):
        self._maximized = bool(event.new_window_state & Gdk.WindowState.MAXIMIZED)
        return False

    def on_destroy(self, *_):
        self._stop_player()
        if self.generator:
            self.generator.cancel()
        w, h = self.get_size()
        settings = dict(self.settings)   # conserva cut_mode y lo demás que se guarda al cambiar
        settings.update({
            "interval": self.current_interval(),
            "cols": int(round(self.tile_scale.get_value())),
            "win_w": w, "win_h": h, "maximized": self._maximized,
            "panel_w": self.paned.get_position(), "segpanel": self.seg_btn.get_active(),
            "snap": self.snap_btn.get_active(),
        })
        save_settings(settings)
        Gtk.main_quit()


def choose_videos(parent=None):
    dlg = Gtk.FileChooserNative.new("Abrir vídeos", parent, Gtk.FileChooserAction.OPEN, "Abrir", "Cancelar")
    dlg.set_select_multiple(True)
    flt = Gtk.FileFilter()
    flt.set_name("Vídeos")
    flt.add_mime_type("video/*")
    for ext in VIDEO_EXT:
        flt.add_pattern("*" + ext)
    dlg.add_filter(flt)
    res = dlg.run()
    paths = list(dlg.get_filenames() or []) if res == Gtk.ResponseType.ACCEPT else []
    dlg.destroy()
    return paths


def choose_folders(parent=None):
    dlg = Gtk.FileChooserNative.new("Añadir carpetas de vídeos", parent, Gtk.FileChooserAction.SELECT_FOLDER,
                                    "Añadir", "Cancelar")
    dlg.set_select_multiple(True)
    res = dlg.run()
    paths = list(dlg.get_filenames() or []) if res == Gtk.ResponseType.ACCEPT else []
    dlg.destroy()
    return paths


def main(argv):
    # los hilos de trabajo casi siempre esperan a ffmpeg: ceder el GIL cada 2 ms en vez de 5 acorta los parones del
    # hilo de la interfaz cuando alguno hace trabajo Python (medido: bloqueo máximo 141 → 82 ms) sin coste apreciable
    sys.setswitchinterval(float(os.environ.get("THUMBSHEET_SWITCH", "0.002")))
    GLib.set_prgname(APP)
    GLib.set_application_name(APP)
    Gdk.set_program_class(APP)
    css = Gtk.CssProvider()
    css.load_from_data(b"""
        .ts-preview-bar { background-color: #1a1a1c; padding: 4px 8px; }
        .ts-preview-bar label, .ts-preview-bar button { color: #e8e8ec; }
        .ts-black { background-color: #000000; }
        .ts-toast { border-radius: 8px; padding: 10px 14px; box-shadow: 0 2px 10px rgba(0,0,0,0.55); }
        .ts-toast-ok { background-color: #2e7d32; }
        .ts-toast-err { background-color: #c62828; }
        .ts-toast label, .ts-toast button { color: #ffffff; }
        .ts-toast label selection { background-color: #ffffff; color: #c62828; }
        .dim-label { opacity: 0.8; }
        .ts-sheet-scroll scrollbar.vertical slider { min-width: 16px; min-height: 56px; border-radius: 10px; }
        .ts-sheet-scroll scrollbar.vertical { background-color: #1c1c1f; border: none; }   /* sin la raya clara de Adwaita */
        .ts-files row { padding: 1px 0; }
        .ts-files row.ts-folder { background-color: alpha(@theme_fg_color, 0.06); }
        .ts-card:hover { background-color: alpha(@theme_fg_color, 0.07); }
        .ts-card.ts-dragging { opacity: 0.3; }   /* hueco de la tarjeta arrastrada */
        .ts-panel-header { padding: 1px 2px 1px 8px; background-color: alpha(@theme_fg_color, 0.06);
                           border-bottom: 1px solid alpha(@theme_fg_color, 0.15); }
        scale marks { color: alpha(currentColor, 0.8); }
        progressbar { color: alpha(@theme_fg_color, 0.8); }
    """)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            print("falta %s en el PATH (sudo apt install ffmpeg)" % tool, file=sys.stderr)
            return 2
    paths = []
    for arg in argv[1:]:
        if arg.startswith("file://"):
            try:
                arg = GLib.filename_from_uri(arg)[0]
            except GLib.Error:
                continue
        if os.path.isfile(arg) or os.path.isdir(arg):   # las carpetas se recorren (con subcarpetas) al abrir
            paths.append(arg)
        else:
            print("no existe: %s" % arg, file=sys.stderr)
    if not paths:
        if len(argv) > 1:
            return 2
        paths = choose_videos()
        if not paths:
            return 0
    win = ThumbSheet(paths)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda *_: (win.destroy(), False)[1])
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
