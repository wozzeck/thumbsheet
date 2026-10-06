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
import hashlib
import json
import math
import os
import pathlib
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
import cairo  # noqa: E402
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango, PangoCairo  # noqa: E402

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
INTERVALS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600]     # valores del slider de intervalo (s)
INTERVAL_DEF = 30
COLS_MIN, COLS_MAX, COLS_DEF = 3, 20, 6                     # teselas por fila
PIX_BUDGET = int(os.environ.get("THUMBSHEET_PIX_MB", "64")) * 1024 * 1024
DEBUG = os.environ.get("THUMBSHEET_DEBUG") == "1"

VIDEO_EXT = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".mpg", ".mpeg", ".ts", ".m2ts",
             ".wmv", ".flv", ".3gp", ".ogv", ".vob")


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
    """Se ejecuta en el hijo justo antes del exec: pide al kernel que le mande SIGTERM si el padre
    (esta app) muere, sea como sea. El exec de nice/ionice/ffmpeg lo conserva. Si el padre ya murió
    entre el fork y este prctl (carrera clásica de PDEATHSIG), el hijo se va directamente."""
    try:
        _LIBC.prctl(1, signal.SIGTERM, 0, 0, 0)   # PR_SET_PDEATHSIG = 1
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
FFMPEG = NICE + ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
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
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=FF_ENV)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode("utf-8", "replace").strip() or "ffprobe falló")
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

    @staticmethod
    def selftest(info, device, workdir):
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
class Generator(object):
    """Genera las capturas de un intervalo dado. Avisa por GLib.idle_add: on_tile(t), on_progress(done, total),
    on_done(). Cancelable (mata los ffmpeg en marcha)."""

    GPU_WORKERS = 2

    def __init__(self, info, cache_dir, interval, on_tile, on_progress, on_done):
        self.info = info
        self.cache_dir = cache_dir
        self.S = snap_interval(interval)
        self.on_tile, self.on_progress, self.on_done = on_tile, on_progress, on_done
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.procs = set()
        self.tasks = collections.deque()
        self.pending = 0
        self.done_count = 0
        self.finished = False
        self.gpu_ok = False
        self.threads = []
        self.started = time.time()

        D = info.duration
        self.timestamps = [t for t in range(0, int(math.floor(D)) + 1, self.S) if t <= D - 0.5] or [0]
        self.total = len(self.timestamps)
        self.workers = default_workers()
        gop = info.gop if info.gop is not None else max(400.0 / info.fps, 20.0)
        # coste por captura: seek ≈ decodificar GOP/2 + arranque (~1 s de vídeo equivalente); tramos ≈ S
        self.mode = "seek" if (gop / 2.0 + 1.0) < self.S else "range"
        log("plan: S=%d capturas=%d gop=%.1fs fps=%.2f modo=%s workers=%d" % (
            self.S, self.total, gop, info.fps, self.mode, self.workers))

    # --- API ---
    def start(self):
        cached = set()
        try:
            for name in os.listdir(str(self.cache_dir)):
                if name.endswith(".jpg") and name[:-4].isdigit():
                    cached.add(int(name[:-4]))
        except OSError:
            pass
        missing = [t for t in self.timestamps if t not in cached]
        self.done_count = self.total - len(missing)
        for t in self.timestamps:
            if t in cached:
                self.on_tile(t)
        self._progress()
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

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            self.tasks.clear()
            procs = list(self.procs)
        for p in procs:
            try:
                p.terminate()
            except OSError:
                pass

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
            with self.lock:
                task = self.tasks.popleft() if self.tasks else None
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
            if not ok and kind == "gpu":
                # la GPU ha fallado con este tramo: lo devuelve a la cola para la CPU y se retira
                log("gpu: fallo en tramo, se desactiva")
                self.gpu_ok = False
                with self.lock:
                    self.tasks.appendleft(task)
                return
            with self.lock:
                self.pending -= 1
                last = self.pending == 0
            if last:
                GLib.idle_add(self._finish)

    def _run(self, cmd, timeout=None):
        if self.cancelled.is_set():
            return None
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, preexec_fn=PREEXEC, env=FF_ENV)
        with self.lock:
            self.procs.add(p)
        return p

    def _wait(self, p):
        try:
            _, err = p.communicate()
        finally:
            with self.lock:
                self.procs.discard(p)
        if p.returncode != 0 and not self.cancelled.is_set():
            log("ffmpeg rc=%s: %s" % (p.returncode, err.decode("utf-8", "replace").strip()[-300:]))
        return p.returncode == 0

    def _out_opts(self, n, vf, out):
        opts = ["-map", "0:v:0", "-an", "-sn", "-dn", "-vf", vf, "-frames:v", str(n), "-q:v", "4",
                "-f", "image2"]
        if n == 1 and "%d" not in out:
            opts += ["-update", "1"]
        return opts + ["-y", out]

    def _do_seek(self, t):
        final = self.cache_dir / ("%d.jpg" % t)
        tmp = self.cache_dir / (".%d.tmp.jpg" % t)
        vf = "scale=%d:%d" % (self.info.thumb_w, self.info.thumb_h)
        cmd = FFMPEG + SW_DEC + ["-ss", str(t), "-i", str(self.info.path)] + self._out_opts(1, vf, str(tmp))
        p = self._run(cmd)
        if p is None:
            return False
        ok = self._wait(p) and tmp.exists()
        if ok:
            os.replace(str(tmp), str(final))
            self._tile_ready(t)
        else:
            try:
                tmp.unlink()
            except OSError:
                pass
            self._tile_failed(t)
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
        if dev:
            vf = "%s,scale_vaapi=w=%d:h=%d,hwdownload,format=nv12" % (fps, self.info.thumb_w, self.info.thumb_h)
            dec = Gpu.dec_opts(dev)
        else:
            vf = "%s,scale=%d:%d" % (fps, self.info.thumb_w, self.info.thumb_h)
            dec = SW_DEC
        cmd = FFMPEG + dec + ["-ss", str(a), "-t", "%.1f" % dur, "-i", str(self.info.path)] + \
            self._out_opts(n, vf, str(tmpdir / "%d.jpg"))
        p = self._run(cmd)
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
            ok = self._wait(p)
            if self.cancelled.is_set():
                return False
            if dev is not None and not (ok and published == n):
                # la GPU sólo cuenta como OK si ha producido todo; si no, la CPU rehace lo que falte
                return False
            for i in range(published, n):
                self._tile_failed(ts[i])
            return ok
        finally:
            shutil.rmtree(str(tmpdir), ignore_errors=True)

    def _publish(self, src, t):
        try:
            os.replace(str(src), str(self.cache_dir / ("%d.jpg" % t)))
        except OSError:
            self._tile_failed(t)
            return
        self._tile_ready(t)

    def _tile_ready(self, t):
        with self.lock:
            self.done_count += 1
        GLib.idle_add(self.on_tile, t)
        self._progress()

    def _tile_failed(self, t):
        with self.lock:
            self.done_count += 1
        self._progress()

    def _progress(self):
        GLib.idle_add(self.on_progress, self.done_count, self.total)

    def _finish(self):
        if self.finished or self.cancelled.is_set():
            return False
        self.finished = True
        log("fin: %d capturas en %.1fs (%s)" % (self.total, time.time() - self.started, self.mode))
        self.on_done()
        return False


# ----------------------------------------------------------------------------------------------
# caché de pixbufs (LRU por bytes) y cargador en segundo plano
# ----------------------------------------------------------------------------------------------
class PixCache(object):
    def __init__(self, budget):
        self.budget = budget
        self.items = collections.OrderedDict()   # t -> pixbuf
        self.bytes = 0
        self.lock = threading.Lock()

    def get(self, t):
        with self.lock:
            pb = self.items.get(t)
            if pb is not None:
                self.items.move_to_end(t)
            return pb

    def put(self, t, pb):
        size = pb.get_byte_length()
        with self.lock:
            old = self.items.pop(t, None)
            if old is not None:
                self.bytes -= old.get_byte_length()
            self.items[t] = pb
            self.bytes += size
            while self.bytes > self.budget and len(self.items) > 1:
                _, victim = self.items.popitem(last=False)
                self.bytes -= victim.get_byte_length()

    def clear(self):
        with self.lock:
            self.items.clear()
            self.bytes = 0


class Loader(object):
    """Hilo que decodifica miniaturas al tamaño pedido. Atiende primero lo último solicitado (lo que
    está en pantalla) y descarta lo que ya no se ve."""

    def __init__(self, cache, visible_fn, redraw_fn):
        self.cache = cache
        self.visible_fn = visible_fn      # () -> (set de t visibles o None=todo)
        self.redraw_fn = redraw_fn
        self.cv = threading.Condition()
        self.queue = collections.OrderedDict()   # t -> (path, width)
        self.redraw_pending = False
        th = threading.Thread(target=self._run, name="ts-loader", daemon=True)
        th.start()

    def request(self, t, path, width):
        with self.cv:
            self.queue.pop(t, None)
            self.queue[t] = (path, width)
            self.cv.notify()

    def cancel_all(self):
        with self.cv:
            self.queue.clear()

    def _run(self):
        while True:
            with self.cv:
                while not self.queue:
                    self.cv.wait()
                t, (path, width) = self.queue.popitem(last=True)
            vis = self.visible_fn()
            if vis is not None and t not in vis:
                continue
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), width, -1, True)
            except GLib.Error:
                continue
            self.cache.put(t, pb)
            with self.cv:
                if not self.redraw_pending:
                    self.redraw_pending = True
                    GLib.idle_add(self._redraw)

    def _redraw(self):
        with self.cv:
            self.redraw_pending = False
        self.redraw_fn()
        return False


# ----------------------------------------------------------------------------------------------
# el mosaico
# ----------------------------------------------------------------------------------------------
class Sheet(Gtk.DrawingArea):
    GAP = 6
    PAD = 8
    SETTLE_MS = 160

    def __init__(self):
        super(Sheet, self).__init__()
        self.cache_dir = None
        self.ts = []
        self.index = {}
        self.ready = set()
        self.failed_all = False
        self.aspect = 16.0 / 9.0
        self.cols_wanted = COLS_DEF
        self.cols = COLS_DEF
        self.cell_w = 160
        self.cell_h = 90
        self.vadj = None                      # ajuste vertical del ScrolledWindow (lo pone la ventana)
        self.cache = PixCache(PIX_BUDGET)
        self.visible = (0, -1)
        self.vis_lock = threading.Lock()
        self.settled = True
        self._settle_id = None
        self.loader = Loader(self.cache, self._visible_set, self.queue_draw)
        self.font = Pango.FontDescription("Sans 9")
        self.selected = set()                 # teselas marcadas: vista derivada de los segmentos del documento
        self.on_drag_begin = None             # () -> None            : la ventana guarda una instantánea
        self.on_drag_apply = None             # (timestamps, mode)    : aplica sobre la instantánea
        self.on_drag_end = None               # () -> None            : persistir / refrescar
        self.on_preview = None                # (t) -> None           : clic derecho = ampliar a ventana completa
        self._drag = None
        self._anchor_t = None                 # última tesela pulsada con el botón izquierdo (para Shift+clic)
        self.connect("draw", self.on_draw)
        self.connect("size-allocate", lambda *_: self._relayout())
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
        if selected != self.selected:
            self.selected = selected
            self.queue_draw()

    # --- selección: clic = alternar una tesela; clic y arrastrar = aplicar a un rango contiguo ---
    def _tile_at(self, x, y, loose=False):
        if not self.ts:
            return None
        cx, cy = x - self.PAD, y - self.PAD
        if cx < 0 or cy < 0:
            if not loose:
                return None
            cx, cy = max(0, cx), max(0, cy)
        col, rx = divmod(int(cx), self.cell_w + self.GAP)
        row, ry = divmod(int(cy), self.cell_h + self.GAP)
        if loose:
            col = min(col, self.cols - 1)
        elif rx >= self.cell_w or ry >= self.cell_h or col >= self.cols:
            return None
        i = row * self.cols + col
        if i >= len(self.ts):
            return len(self.ts) - 1 if loose else None
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
        self._relayout()

    def tile_ready(self, t):
        if t in self.index:
            self.ready.add(t)
            self._redraw_tile(self.index[t])
        return False

    # --- posición de lectura: la tesela del centro del visor sigue en el centro al recomponer ---
    def center_time(self):
        """Instante de la tesela que está en el centro del visor (None si no hay plan)."""
        if not self.ts or self.vadj is None:
            return None
        y = self.vadj.get_value() + self.vadj.get_page_size() / 2.0
        rows = int(math.ceil(len(self.ts) / float(self.cols)))
        row = int((y - self.PAD) // (self.cell_h + self.GAP))
        row = max(0, min(rows - 1, row))
        i = min(len(self.ts) - 1, row * self.cols + self.cols // 2)
        return self.ts[i]

    def scroll_to_time(self, t):
        """Deja centrada la tesela más cercana a t. Se aplica ya (por si el alto no cambió) y otra vez en
        idle, cuando el ScrolledWindow ya conoce la nueva altura y no recorta el valor."""
        if t is None or not self.ts or self.vadj is None:
            return
        i = min(range(len(self.ts)), key=lambda k: abs(self.ts[k] - t))

        def apply(final=False):
            row = i // self.cols
            y = self.PAD + row * (self.cell_h + self.GAP) + self.cell_h / 2.0
            page = self.vadj.get_page_size()
            upper = self.vadj.get_upper()
            self.vadj.set_value(max(0.0, min(max(0.0, upper - page), y - page / 2.0)))
            if final:
                log("scroll: centrada t=%d (cols=%d), centro real ahora t=%s" % (self.ts[i], self.cols, self.center_time()))
            return False
        apply()
        GLib.idle_add(apply, True)

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
    def _relayout(self):
        width = max(1, self.get_allocated_width())
        avail = max(1, width - 2 * self.PAD)
        cols = max(1, self.cols_wanted)
        cell_w = max(24, (avail - (cols - 1) * self.GAP) // cols)
        cell_h = max(8, int(round(cell_w / self.aspect)))
        rows = int(math.ceil(len(self.ts) / float(cols))) if self.ts else 0
        total_h = 2 * self.PAD + rows * cell_h + max(0, rows - 1) * self.GAP
        changed = (cols, cell_w, cell_h) != (self.cols, self.cell_w, self.cell_h)
        self.cols, self.cell_w, self.cell_h = cols, cell_w, cell_h
        if self.get_size_request()[1] != total_h:
            self.set_size_request(-1, total_h)
        if changed:
            self.queue_draw()

    def _tile_rect(self, i):
        r, c = divmod(i, self.cols)
        x = self.PAD + c * (self.cell_w + self.GAP)
        y = self.PAD + r * (self.cell_h + self.GAP)
        return x, y, self.cell_w, self.cell_h

    def _redraw_tile(self, i):
        x, y, w, h = self._tile_rect(i)
        self.queue_draw_area(x, y, w, h)

    def _visible_set(self):
        with self.vis_lock:
            a, b = self.visible
        if b < a:
            return set()
        return set(self.ts[max(0, a):b + 1])

    # --- dibujo ---
    def on_draw(self, widget, cr):
        x1, y1, x2, y2 = cr.clip_extents()
        cr.set_source_rgb(0.11, 0.11, 0.12)
        cr.paint()
        if not self.ts:
            return False
        row_h = self.cell_h + self.GAP
        r0 = max(0, int((y1 - self.PAD) // row_h))
        r1 = int((y2 - self.PAD) // row_h)
        i0 = r0 * self.cols
        i1 = min(len(self.ts) - 1, (r1 + 1) * self.cols - 1)
        # lo visible + una pantalla por delante y por detrás para que el scroll no muestre huecos
        margin = self.cols * max(1, int(math.ceil((y2 - y1) / float(row_h))))
        with self.vis_lock:
            self.visible = (max(0, i0 - margin), min(len(self.ts) - 1, i1 + margin))
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(self.font)
        for i in range(i0, i1 + 1):
            self._draw_tile(cr, layout, i)
        # prefetch fuera de pantalla (sin dibujar)
        for i in list(range(max(0, i0 - margin), i0)) + list(range(i1 + 1, min(len(self.ts), i1 + 1 + margin))):
            t = self.ts[i]
            if t in self.ready and self.cache.get(t) is None:
                self.loader.request(t, self.cache_dir / ("%d.jpg" % t), self.cell_w)
        return False

    def _draw_tile(self, cr, layout, i):
        t = self.ts[i]
        x, y, w, h = self._tile_rect(i)
        pb = self.cache.get(t) if t in self.ready else None
        if pb is None:
            cr.set_source_rgb(0.18, 0.18, 0.2)
            cr.rectangle(x, y, w, h)
            cr.fill()
            if t in self.ready:
                self.loader.request(t, self.cache_dir / ("%d.jpg" % t), w)
        else:
            pw, ph = pb.get_width(), pb.get_height()
            if pw != w and self.settled:
                self.loader.request(t, self.cache_dir / ("%d.jpg" % t), w)
            if abs(pw - w) <= 1 and abs(ph - h) <= 1:
                # tamaño exacto (±1 px de redondeo): copia directa, recortada a la celda
                cr.save()
                cr.rectangle(x, y, w, h)
                cr.clip()
                Gdk.cairo_set_source_pixbuf(cr, pb, x, y)
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
                Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
                cr.get_source().set_filter(cairo.FILTER_BILINEAR)
                cr.rectangle(0, 0, pw, ph)
                cr.fill()
                cr.restore()
        # etiqueta de tiempo
        if w >= 56:
            layout.set_text(fmt_time(t), -1)
            tw, th = layout.get_pixel_size()
            bx, by = x + 4, y + h - th - 6
            cr.set_source_rgba(0, 0, 0, 0.6)
            self._rounded(cr, bx, by, tw + 8, th + 2, 3)
            cr.fill()
            cr.set_source_rgb(0.95, 0.95, 0.95)
            cr.move_to(bx + 4, by + 1)
            PangoCairo.show_layout(cr, layout)
        # recuadro rojo de selección
        if t in self.selected:
            lw = 4 if w >= 120 else 3
            cr.set_source_rgb(0.93, 0.16, 0.16)
            cr.set_line_width(lw)
            cr.rectangle(x + lw / 2.0, y + lw / 2.0, w - lw, h - lw)
            cr.stroke()

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
            sc = min(aw / float(pw), ah / float(ph))
            dw, dh = pw * sc, ph * sc
            cr.save()
            cr.translate((aw - dw) / 2.0, (ah - dh) / 2.0)
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


class Player(object):
    """playbin + gtksink embebido en la capa de la vista ampliada. Los avisos del bus llegan en el hilo GTK
    (add_signal_watch) y se reparten por callbacks: on_state(playing), on_eos(), on_error(mensaje)."""

    FLAG_AUDIO = 1 << 1   # GST_PLAY_FLAG_AUDIO

    def __init__(self, audio=True):
        global _GST_READY
        if not HAVE_GST:
            raise RuntimeError("GStreamer no disponible")
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
        self.playbin.set_state(Gst.State.NULL)
        self.playbin.set_property("uri", pathlib.Path(path).as_uri())
        self.loaded_path = pathlib.Path(path)
        self.want_play = play
        self._pending = (start_s, play)
        self._prerolled = False
        self.playbin.set_state(Gst.State.PAUSED)

    def _on_message(self, bus, msg):
        t = msg.type
        if t == Gst.MessageType.ASYNC_DONE:
            if not self._prerolled:
                self._prerolled = True
                start_s, play = self._pending or (None, False)
                self._pending = None
                if start_s is not None:
                    self.seek(start_s)
                if play:
                    self.playbin.set_state(Gst.State.PLAYING)
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
    """Capa de la vista ampliada: arriba el fotograma fijo o el vídeo; abajo play/pausa, barra de progreso
    (arrastrable: hace de scrubber), tiempo y cerrar."""

    def __init__(self):
        super(PreviewLayer, self).__init__(orientation=Gtk.Orientation.VERTICAL)
        self.on_close = None
        self.on_toggle_play = None
        self.on_seek = None
        self.duration = 1.0
        self._dragging = False
        self._updating = False
        self.stack = Gtk.Stack()
        self.stack.set_homogeneous(True)
        self.still = Preview()
        self.still.set_no_show_all(False)   # Preview nace con no_show_all (era la capa entera); aquí es un hijo normal
        self.still.on_close = lambda: self.on_close() if self.on_close else None
        self.stack.add_named(self.still, "still")
        self.video_box = Gtk.EventBox()
        self.video_box.get_style_context().add_class("ts-black")
        self.video_box.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.video_box.connect("button-press-event", lambda w, e: (self.on_toggle_play() if self.on_toggle_play else None) or True)
        self.stack.add_named(self.video_box, "video")
        self.pack_start(self.stack, True, True, 0)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.get_style_context().add_class("ts-preview-bar")
        self.play_btn = Gtk.Button()
        self.play_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic", Gtk.IconSize.BUTTON)
        self.play_btn.add(self.play_icon)
        self.play_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.play_btn.set_tooltip_text("Reproducir / pausar (espacio). Flechas: ±intervalo")
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
        self.still.set_frame(pixbuf, t, label)
        self.stack.set_visible_child_name("still")
        self.set_playing(False)

    def show_video(self):
        self.stack.set_visible_child_name("video")

    def attach_video(self, widget):
        self.video_box.add(widget)
        widget.show()

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

    def __init__(self, segments=None):
        self.segments = self._normalize(segments or [])

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
        return Selection(list(self.segments))

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

    @staticmethod
    def tile_range(t, interval, duration):
        return float(t), min(float(t + interval), float(duration))

    def tiles(self, timestamps, interval, duration):
        """Teselas marcadas para esta rejilla."""
        out = set()
        for t in timestamps:
            a, b = self.tile_range(t, interval, duration)
            for x, y in self.segments:
                if x < b and y > a:
                    out.add(t)
                    break
        return out

    def to_json(self):
        return {"segments": [[_num(a), _num(b)] for a, b in self.segments]}

    @classmethod
    def from_json(cls, data):
        if isinstance(data, dict):
            return cls([(a, b) for a, b in data.get("segments") or []])
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


def cut_output_path(video_path):
    video_path = pathlib.Path(video_path)
    ext = video_path.suffix.lower() if video_path.suffix.lower() in COPY_CONTAINERS else ".mkv"
    return video_path.with_name(video_path.stem + "-cortado" + ext)


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
        out.append((na, min(nb, float(duration))))
    return Selection._normalize(out)


def write_concat_list(list_path, video_path, segments):
    quoted = str(video_path).replace("'", "'\\''")
    lines = ["ffconcat version 1.0"]
    for a, b in segments:
        lines += ["file '%s'" % quoted, "inpoint %.3f" % a, "outpoint %.3f" % b]
    pathlib.Path(list_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def cut_command_copy(list_path, out_path):
    """Sin pérdida: demuxer concat con inpoint/outpoint (en keyframes) y copia de streams."""
    return FFMPEG + ["-progress", "pipe:1", "-nostats", "-f", "concat", "-safe", "0", "-i", str(list_path),
                     "-map", "0:v:0", "-map", "0:a?", "-ignore_unknown", "-c", "copy", "-avoid_negative_ts", "make_zero",
                     "-y", str(out_path)]


def cut_command_exact(video_path, segments, out_path, has_audio):
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
    return cmd + ["-y", str(out_path)]


# ----------------------------------------------------------------------------------------------
# documento = un vídeo abierto (sondeo, caché, selección, posición de scroll)
# ----------------------------------------------------------------------------------------------
class Document(object):
    def __init__(self, path):
        self.path = pathlib.Path(path).resolve()
        self.info = None
        self.error = None
        self.cache_dir = None
        self.selection = Selection()
        self.scroll = 0.0
        self.row = None
        self.lock = threading.Lock()

    def ensure_info(self):
        with self.lock:
            if self.info is not None or self.error is not None:
                return
            try:
                info = VideoInfo(self.path)
            except (OSError, RuntimeError) as e:
                self.error = str(e) or "no se pudo abrir"
                return
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
        return "%s · %dx%d" % (fmt_time(self.info.duration), self.info.width, self.info.height)


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
        self.docs = []
        self.current = None
        self.generator = None
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

        bar.pack_start(Gtk.Label(label="Intervalo"), False, False, 0)
        # el slider de intervalo recorre posiciones de INTERVALS (0..n-1), con marcas rotuladas
        self.interval_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, len(INTERVALS) - 1, 1)
        self.interval_scale.set_draw_value(False)
        self.interval_scale.set_round_digits(0)
        for i, sec in enumerate(INTERVALS):
            self.interval_scale.add_mark(i, Gtk.PositionType.BOTTOM, interval_mark(sec))
        self.interval_scale.set_hexpand(True)
        self.interval_scale.set_value(INTERVALS.index(interval))
        self.interval_scale.set_tooltip_text("Tiempo entre capturas: 1 s, 2 s, 5 s, 10 s, 15 s, 30 s, 1, 2, 5 o 10 min (rueda = un paso)")
        bar.pack_start(self.interval_scale, True, True, 0)
        self.interval_label = Gtk.Label(label="")
        self.interval_label.set_width_chars(6)
        self.interval_label.set_xalign(0.0)
        bar.pack_start(self.interval_label, False, False, 0)

        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 6)

        bar.pack_start(Gtk.Label(label="Tamaño"), False, False, 0)
        self.tile_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, COLS_MIN, COLS_MAX, 1)
        self.tile_scale.set_draw_value(False)
        self.tile_scale.set_round_digits(0)
        for n in (3, 5, 10, 15, 20):
            self.tile_scale.add_mark(n, Gtk.PositionType.BOTTOM, str(n))
        self.tile_scale.set_hexpand(True)
        self.tile_scale.set_value(cols)
        self.tile_scale.set_tooltip_text("Teselas por fila (3–20; rueda = ±1; Ctrl+rueda sobre el mosaico)")
        bar.pack_start(self.tile_scale, True, True, 0)
        self.tile_label = Gtk.Label(label="")
        self.tile_label.set_width_chars(10)
        self.tile_label.set_xalign(0.0)
        bar.pack_start(self.tile_label, False, False, 0)

        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 6)

        # derecha: [contador] [LLC] [Eliminar]
        self.del_btn = Gtk.Button(label="Eliminar")
        self.del_btn.get_style_context().add_class("destructive-action")
        self.del_btn.set_tooltip_text("Borrar el archivo de vídeo del disco (sin papelera), tras confirmar")
        self.del_btn.connect("clicked", self.on_delete)
        bar.pack_end(self.del_btn, False, False, 0)
        self.llc_btn = Gtk.Button(label="LLC")
        self.cut_btn = Gtk.Button(label="Cortar")
        self.cut_btn.set_tooltip_text("Cortar los segmentos seleccionados y unirlos en <vídeo>-cortado (sin pérdida, en keyframes; "
                                      "o exacto recodificando)")
        self.cut_btn.connect("clicked", self.on_cut)
        self._cut = None
        self.llc_btn.set_tooltip_text("Guardar un proyecto de LosslessCut (<vídeo>-proj.llc, junto al vídeo) con un segmento "
                                      "por cada racha de teselas seleccionadas")
        self.llc_btn.connect("clicked", self.on_llc)
        bar.pack_end(self.llc_btn, False, False, 0)
        bar.pack_end(self.cut_btn, False, False, 0)
        self.status = Gtk.Label(label="")
        self.status.set_xalign(1.0)
        self.status.set_ellipsize(Pango.EllipsizeMode.START)
        self.status.set_width_chars(24)
        self.status.set_max_width_chars(46)   # los mensajes largos se recortan, no estrechan los sliders
        bar.pack_end(self.status, False, False, 4)

        # --- panel de ficheros + mosaico (con la vista ampliada superpuesta) ---
        self.overlay = Gtk.Overlay()
        vbox.pack_start(self.overlay, True, True, 0)
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.overlay.add(self.paned)
        self.layer = PreviewLayer()
        self.layer.on_close = self.hide_preview
        self.layer.on_toggle_play = self.toggle_play
        self.layer.on_seek = self.preview_seek
        self.overlay.add_overlay(self.layer)
        self.toast = Toast()
        self.overlay.add_overlay(self.toast)
        self._preview_key = None
        self.player = None
        self._player_doc = None
        self._tick_id = None
        self._seek_id = None
        # extracción de fotogramas completos: un solo hilo, atiende primero lo último pedido (LIFO) y
        # descarta lo que ya exista; así mantener pulsada una flecha no lanza decenas de ffmpeg
        self._full_lock = threading.Condition()
        self._full_jobs = []
        threading.Thread(target=self._full_loop, name="ts-full", daemon=True).start()
        side = Gtk.ScrolledWindow()
        side.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        side.set_size_request(140, -1)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.BROWSE)
        self.listbox.connect("row-selected", self.on_row_selected)
        side.add(self.listbox)
        self.paned.pack1(side, False, False)
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.ALWAYS)
        self.sheet = Sheet()
        self.sheet.set_cols(cols)
        self.sheet.vadj = self.scroller.get_vadjustment()
        self.sheet.on_drag_begin = self.on_drag_begin
        self.sheet.on_drag_apply = self.on_drag_apply
        self.sheet.on_drag_end = self.on_drag_end
        self.sheet.on_preview = self.show_preview
        self._sel_snapshot = None
        self.scroller.add(self.sheet)
        self.paned.pack2(self.scroller, True, False)
        self.paned.set_position(int(self.settings.get("panel_w", 240)))

        self.interval_scale.connect("value-changed", self.on_interval_changed)
        self.interval_scale.connect("scroll-event", self._slider_scroll, 1)
        self.tile_scale.connect("value-changed", self.on_tile_changed)
        self.tile_scale.connect("scroll-event", self._slider_scroll, 1)
        self.scroller.connect("scroll-event", self.on_scroll)
        self.connect("key-press-event", self.on_key)
        self.connect("destroy", self.on_destroy)
        self.connect("window-state-event", self.on_window_state)
        self._maximized = bool(self.settings.get("maximized"))
        # arrastrar y soltar vídeos sobre la ventana
        self.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.connect("drag-data-received", self.on_drag_data)
        self._update_labels()

        # sondeo de vídeos en un hilo (ffprobe), de uno en uno
        self._probe_q = queue.Queue()
        threading.Thread(target=self._probe_loop, name="ts-probe", daemon=True).start()

        self.show_all()
        self._load_current()
        self.add_paths(video_paths)
        if DEBUG:
            GLib.timeout_add(1500, self._log_geometry)

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
        rows = ";".join(center(d.row) for d in self.docs if d.row is not None)
        log("geometry: sheet=%d,%d cols=%d cell=%dx%d pad=%d gap=%d interval=%s tile=%s tilew=%d llc=%s cut=%s del=%s rows=%s" % (
            sx, sy, self.sheet.cols, self.sheet.cell_w, self.sheet.cell_h, Sheet.PAD, Sheet.GAP,
            center(self.interval_scale), center(self.tile_scale), self.tile_scale.get_allocation().width,
            center(self.llc_btn), center(self.cut_btn), center(self.del_btn), rows))
        return False

    # ---- documentos --------------------------------------------------------------------------
    def add_paths(self, paths):
        first_new = None
        for path in paths:
            try:
                rp = pathlib.Path(path).resolve()
            except OSError:
                continue
            if not rp.is_file() or any(d.path == rp for d in self.docs):
                continue
            doc = Document(rp)
            self.docs.append(doc)
            doc.row = self._make_row(doc)
            self.listbox.add(doc.row)
            doc.row.show_all()
            self._probe_q.put(doc)
            first_new = first_new or doc
        if first_new is not None and self.current is None:
            self.listbox.select_row(first_new.row)

    def _make_row(self, doc):
        row = Gtk.ListBoxRow()
        row.doc = doc
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(5)
        box.set_margin_bottom(5)
        name = Gtk.Label(label=doc.path.name)
        name.set_xalign(0.0)
        name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        name.set_tooltip_text(str(doc.path))
        sub = Gtk.Label()
        sub.set_xalign(0.0)
        sub.get_style_context().add_class("dim-label")
        sub.set_markup("<small>%s</small>" % GLib.markup_escape_text(doc.subtitle))
        box.pack_start(name, False, False, 0)
        box.pack_start(sub, False, False, 0)
        row.add(box)
        row.sub_label = sub
        return row

    def _probe_loop(self):
        while True:
            doc = self._probe_q.get()
            doc.ensure_info()
            GLib.idle_add(self._doc_probed, doc)

    def _doc_probed(self, doc):
        if doc.row is not None:
            doc.row.sub_label.set_markup("<small>%s</small>" % GLib.markup_escape_text(doc.subtitle))
        if doc is self.current:
            self._load_current()
        return False

    def on_row_selected(self, listbox, row):
        self.show_document(row.doc if row is not None else None)

    def show_document(self, doc):
        if doc is self.current:
            return
        self.hide_preview()
        if self.current is not None:
            self.current.scroll = self.scroller.get_vadjustment().get_value()
        if self.generator:
            self.generator.cancel()
            self.generator = None
        self.current = doc
        self._load_current()

    def _load_current(self):
        doc = self.current
        self.set_title("%s — %s" % (doc.path.name, APP) if doc else APP)
        if doc is None or doc.info is None:
            self.sheet.set_video(16.0 / 9.0, None)
            self._progress = (0, 0)
            self._refresh_status()
            self._update_buttons()
            return
        self.sheet.set_video(doc.info.aspect, doc.cache_dir)
        log("vídeo: %s %dx%d %s %.2ffps dur=%s gop=%s miniatura=%dx%d" % (
            doc.path.name, doc.info.width, doc.info.height, doc.info.codec, doc.info.fps,
            fmt_time(doc.info.duration), doc.info.gop, doc.info.thumb_w, doc.info.thumb_h))
        self.regenerate()
        if doc.scroll:
            GLib.timeout_add(60, self._restore_scroll, doc)
        self._update_buttons()

    def _restore_scroll(self, doc):
        if doc is self.current:
            self.scroller.get_vadjustment().set_value(doc.scroll)
        return False

    def regenerate(self):
        self._regen_id = None
        doc = self.current
        if not doc or not doc.info:
            return False
        if self.generator:
            self.generator.cancel()
        S = self.current_interval()
        holder = []
        current = lambda: self.generator is holder[0]  # noqa: E731
        gen = Generator(doc.info, doc.cache_dir, S, self.sheet.tile_ready,
                        lambda d, n: self.on_progress(d, n) if current() else False,
                        lambda: self.on_done() if current() else False)
        holder.append(gen)
        self.generator = gen
        center = self.sheet.center_time()   # None si es un vídeo recién abierto (plan vacío)
        self.sheet.set_plan(gen.timestamps)
        # se marcan las teselas que tocan algún segmento; los segmentos guardados no cambian con la rejilla
        self.sheet.set_selected(doc.selection.tiles(gen.timestamps, S, doc.info.duration))
        if center is not None:
            self.sheet.scroll_to_time(center)   # cambio de intervalo: misma zona del vídeo a la vista
        else:
            self.scroller.get_vadjustment().set_value(0)
        gen.start()
        self._refresh_status()
        self._update_buttons()
        return False

    # ---- callbacks de generación ---------------------------------------------------------------
    def on_progress(self, done, total):
        self._progress = (min(done, total), total)
        self._refresh_status()
        return False

    def on_done(self):
        self._refresh_status()
        return False

    # ---- estado y botones ----------------------------------------------------------------------
    def _segments(self):
        doc = self.current
        if not doc or not doc.info or not self.generator:
            return []
        return list(doc.selection.segments)

    def _refresh_status(self):
        doc = self.current
        if doc is None:
            base = "Sin vídeos · Ctrl+O o arrastra aquí" if not self.docs else ""
        elif doc.error:
            base = "No se pudo abrir: %s" % doc.error
        elif doc.info is None:
            base = "Analizando…"
        elif self.generator and not self.generator.finished:
            base = "%d / %d capturas" % self._progress
        else:
            base = "%d capturas · %s · %dx%d" % (self.generator.total if self.generator else 0,
                                                 fmt_time(doc.info.duration), doc.info.width, doc.info.height)
        n = len(self._segments())
        if n:
            base += " · %d segmento%s" % (n, "" if n == 1 else "s")
        self._status_base = base
        self.status.set_text(base)

    def _flash(self, text):
        """Aviso de operación correcta (toast verde, 3 s)."""
        self.toast.show_ok(text)

    def _update_buttons(self):
        doc = self.current
        self.del_btn.set_sensitive(doc is not None and self._cut is None)
        has_segs = bool(doc is not None and doc.info is not None and self._segments())
        self.llc_btn.set_sensitive(has_segs)
        self.cut_btn.set_sensitive(has_segs or self._cut is not None)
        self.cut_btn.set_label("Cancelar" if self._cut is not None else "Cortar")

    # ---- selección (clic / arrastre sobre teselas → segmentos) ----------------------------------
    def _grid(self):
        return self.generator.timestamps, self.generator.S, self.current.info.duration

    def on_drag_begin(self):
        self._sel_snapshot = self.current.selection.copy() if self.current else None

    def on_drag_apply(self, timestamps, mode):
        doc = self.current
        if doc is None or doc.info is None or not self.generator or self._sel_snapshot is None:
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
        self.sheet.set_selected(sel.tiles(ts_all, S, dur))

    def on_drag_end(self):
        self._sel_snapshot = None
        if self.current is not None:
            self.current.save_selection()
        self._refresh_status()
        self._update_buttons()

    def clear_selection(self):
        doc = self.current
        if doc is None or not doc.selection:
            return
        doc.selection = Selection()
        self.sheet.set_selected(set())
        doc.save_selection()
        self._refresh_status()
        self._update_buttons()

    # ---- vista ampliada (clic derecho) -----------------------------------------------------------
    def show_preview(self, t):
        doc = self.current
        if doc is None or doc.info is None:
            return
        self._preview_key = (doc, t)
        pb = self.sheet.cache.get(t)
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
        full = doc.cache_dir / "full" / ("%d.jpg" % t)
        if full.exists():
            self._preview_loaded(doc, t, full)
        # vecinos primero (quedan debajo en la pila) y el actual el último: LIFO lo atiende antes
        ts = self.generator.timestamps if self.generator else []
        neighbours = [ts[i] for i in (self._tile_index(t) - 1, self._tile_index(t) + 1) if 0 <= i < len(ts)]
        self._request_full(doc, neighbours + ([] if full.exists() else [t]))

    def _tile_index(self, t):
        ts = self.generator.timestamps if self.generator else []
        if not ts:
            return -1
        return min(range(len(ts)), key=lambda k: abs(ts[k] - t))

    def _preview_step(self, delta):
        """Flechas izquierda/derecha en la vista ampliada: fotograma anterior/siguiente."""
        if self._preview_key is None or not self.generator:
            return
        doc, t = self._preview_key
        ts = self.generator.timestamps
        i = max(0, min(len(ts) - 1, self._tile_index(t) + delta))
        if ts and ts[i] != t:
            self.show_preview(ts[i])

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
        self.layer.still.set_frame(pb, t, doc.path.name)
        return False

    def hide_preview(self):
        self._preview_key = None
        self._stop_player()
        if self.layer.get_visible():
            self.layer.hide()

    # ---- reproducción (GStreamer) dentro de la vista ampliada -----------------------------------
    def _ensure_player(self):
        if self.player is not None:
            return self.player
        if not HAVE_GST:
            self._flash("Sin GStreamer: sudo apt install gir1.2-gstreamer-1.0 gstreamer1.0-gtk3 gstreamer1.0-libav")
            return None
        try:
            audio = os.environ.get("THUMBSHEET_AUDIO", "1") not in ("0", "off", "no")
            self.player = Player(audio=audio)
        except RuntimeError as e:
            self._flash(str(e))
            return None
        self.layer.attach_video(self.player.widget)
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
        self.layer.show_video()
        self.layer.set_position(start_s)
        if self._tick_id is None:
            self._tick_id = GLib.timeout_add(100, self._player_tick)
        log("player: carga %s desde %.1f (%s)" % (doc.path.name, start_s, "play" if play else "pausa"))
        return p

    def _stop_player(self):
        if self.player is not None and self._player_doc is not None:
            self.player.stop()
        self._player_doc = None
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
        if self._seek_id:
            GLib.source_remove(self._seek_id)
        self._seek_id = GLib.timeout_add(60, self._do_seek, s)

    def _do_seek(self, s):
        self._seek_id = None
        if self._player_doc is not None:
            self.player.seek(s)
            self.layer.set_position(s)
            log("player: seek a %.2f" % s)
        return False

    def _player_tick(self):
        if self._player_doc is None:
            self._tick_id = None
            return False
        pos = self.player.position()
        if pos is not None:
            self.layer.set_position(pos)
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

    # ---- LLC ------------------------------------------------------------------------------------
    def on_llc(self, *_):
        doc = self.current
        segs = self._segments()
        if not doc or not segs:
            return
        proj = llc_project_path(doc.path)
        if proj.exists():
            dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                    buttons=Gtk.ButtonsType.NONE, text="Ya existe un proyecto de LosslessCut para este vídeo")
            dlg.format_secondary_text("%s\n\n¿Sobrescribirlo con los %d segmentos seleccionados?" % (proj.name, len(segs)))
            dlg.add_button("_Cancelar", Gtk.ResponseType.CANCEL)
            dlg.add_button("_Sobrescribir", Gtk.ResponseType.ACCEPT)
            dlg.set_default_response(Gtk.ResponseType.CANCEL)
            resp = dlg.run()
            dlg.destroy()
            if resp != Gtk.ResponseType.ACCEPT:
                return
        try:
            write_llc_project(doc.path, segs)
        except OSError as e:
            self._error("No se pudo guardar el proyecto LLC", "%s\n%s" % (proj, e))
            return
        self._flash("Proyecto LLC guardado: %s · %d segmento%s" % (proj.name, len(segs), "" if len(segs) == 1 else "s"))

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
        total = sum(b - a for a, b in segs)
        dlg = Gtk.Dialog(title="Cortar y unir", transient_for=self, modal=True)
        dlg.add_button("_Cancelar", Gtk.ResponseType.CANCEL)
        dlg.add_button("Cor_tar", Gtk.ResponseType.ACCEPT)
        dlg.set_default_response(Gtk.ResponseType.ACCEPT)
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
        if out.exists():
            warn = Gtk.Label(label="Ya existe %s: se sobrescribirá." % out.name)
            warn.set_xalign(0.0)
            box.pack_start(warn, False, False, 0)
        box.show_all()
        resp = dlg.run()
        use_exact = exact.get_active()
        dlg.destroy()
        if resp != Gtk.ResponseType.ACCEPT:
            return
        self.settings["cut_mode"] = "exact" if use_exact else "copy"
        self._cut = {"doc": doc, "segments": list(segs), "out": out, "exact": use_exact, "proc": None,
                     "cancel": threading.Event(), "expected": total}
        self._update_buttons()
        self.status.set_text("Analizando keyframes…" if not use_exact else "Preparando…")
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
            if job["exact"]:
                cmd = cut_command_exact(doc.path, final, out, doc.info.has_audio)
            else:
                write_concat_list(list_path, doc.path, final)
                cmd = cut_command_copy(list_path, out)
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
            if job["cancel"].is_set():
                try:
                    out.unlink()
                except OSError:
                    pass
                GLib.idle_add(self._cut_done, job, None, "cancelado")
                return
            if p.returncode != 0 or not out.exists():
                GLib.idle_add(self._cut_done, job, None, err[-400:] or ("ffmpeg terminó con código %s" % p.returncode))
                return
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
            pct = max(0, min(100, int(100 * done / job["expected"])))
            self.status.set_text("Cortando… %d %%" % pct)
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
        self.status.set_text("Cancelando…")

    def _cut_done(self, job, duration, error):
        if self._cut is not job:
            return False
        self._cut = None
        self._update_buttons()
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
            if duration is not None and abs(duration - job["expected"]) > max(2.0, 0.03 * job["expected"]):
                self._error(txt, "La duración del resultado (%s) no coincide con la esperada (%s): revisa el fichero." % (
                    fmt_time(duration), fmt_time(job["expected"])))
            else:
                self._flash(txt)
            log("cut: hecho %s dur=%s esperado=%.2f" % (out.name, duration, job["expected"]))
            self.add_paths([str(out)])
        return False

    # ---- eliminar -------------------------------------------------------------------------------
    def on_delete(self, *_):
        doc = self.current
        if doc is None:
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
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        resp = dlg.run()
        dlg.destroy()
        if resp != Gtk.ResponseType.ACCEPT:
            return
        self.hide_preview()
        if self.generator:
            self.generator.cancel()
            self.generator = None
        try:
            os.remove(str(doc.path))
        except OSError as e:
            self._error("No se pudo eliminar el archivo", "%s\n%s" % (doc.path, e))
            return
        if doc.cache_dir:
            shutil.rmtree(str(doc.cache_dir), ignore_errors=True)
        self._remove_doc(doc)
        self._flash("Eliminado: %s" % doc.path.name)

    def _remove_doc(self, doc):
        idx = self.docs.index(doc)
        self.docs.remove(doc)
        row = doc.row
        doc.row = None
        if self.current is doc:
            self.current = None
        if row is not None:
            self.listbox.remove(row)
        if self.docs:
            nxt = self.docs[min(idx, len(self.docs) - 1)]
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

    def _update_labels(self):
        self.interval_label.set_text(fmt_interval(self.current_interval()))
        self.tile_label.set_text("%d por fila" % int(round(self.tile_scale.get_value())))

    def on_interval_changed(self, scale):
        v = scale.get_value()
        if abs(v - round(v)) > 1e-6:
            scale.set_value(round(v))   # re-entra ya en una posición entera (clic en la pista, arrastre fino...)
            return
        self._update_labels()
        if self._regen_id:
            GLib.source_remove(self._regen_id)
        self._regen_id = GLib.timeout_add(self.REGEN_DEBOUNCE_MS, self.regenerate)

    def on_tile_changed(self, scale):
        self._update_labels()
        self.sheet.set_cols(int(round(scale.get_value())))

    def _wheel_steps(self, event, key):
        """Pasos de rueda (+1 arriba / -1 abajo); con scroll suave acumula hasta completar un paso."""
        if event.direction == Gdk.ScrollDirection.UP:
            return 1
        if event.direction == Gdk.ScrollDirection.DOWN:
            return -1
        if event.direction == Gdk.ScrollDirection.SMOOTH:
            _, dx, dy = event.get_scroll_deltas()
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

    def on_scroll(self, widget, event):
        if not event.state & Gdk.ModifierType.CONTROL_MASK:
            return False
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
            if event.keyval in (Gdk.KEY_Left, Gdk.KEY_Right, Gdk.KEY_Home, Gdk.KEY_End):
                if self._player_doc is not None:
                    # con vídeo cargado: saltar un intervalo (o al principio / final)
                    S = self.generator.S if self.generator else INTERVAL_DEF
                    dur = self.layer.duration
                    pos = self.player.position()
                    if pos is None:
                        pos = self.layer.scale.get_value()
                    target = {Gdk.KEY_Left: pos - S, Gdk.KEY_Right: pos + S, Gdk.KEY_Home: 0.0,
                              Gdk.KEY_End: max(0.0, dur - 0.5)}[event.keyval]
                    self.preview_seek(max(0.0, min(dur, target)))
                elif event.keyval == Gdk.KEY_Home:
                    self._preview_step(-10 ** 9)
                elif event.keyval == Gdk.KEY_End:
                    self._preview_step(10 ** 9)
                else:
                    self._preview_step(-1 if event.keyval == Gdk.KEY_Left else 1)
                return True
        if ctrl and event.keyval in (Gdk.KEY_q, Gdk.KEY_w):
            self.destroy()
            return True
        if ctrl and event.keyval == Gdk.KEY_o:
            self.add_paths(choose_videos(self))
            return True
        if ctrl and event.keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self.tile_scale.set_value(self.tile_scale.get_value() - 1)
            return True
        if ctrl and event.keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self.tile_scale.set_value(self.tile_scale.get_value() + 1)
            return True
        return False

    def on_drag_data(self, widget, context, x, y, data, info, time_):
        paths = []
        for uri in data.get_uris() or []:
            try:
                paths.append(GLib.filename_from_uri(uri)[0])
            except GLib.Error:
                pass
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
        save_settings({
            "interval": self.current_interval(),
            "cols": int(round(self.tile_scale.get_value())),
            "win_w": w, "win_h": h, "maximized": self._maximized,
            "panel_w": self.paned.get_position(),
        })
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


def main(argv):
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
        if os.path.isfile(arg):
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
