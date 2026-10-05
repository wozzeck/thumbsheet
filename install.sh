#!/usr/bin/env bash
# Instala thumbsheet para el usuario actual (sin sudo): comprueba dependencias, enlaza el lanzador en
# ~/.local/bin y registra la entrada de menú / asociación "Abrir con" para vídeos.
set -euo pipefail
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"

missing=()
command -v ffmpeg  >/dev/null || missing+=(ffmpeg)
command -v ffprobe >/dev/null || missing+=(ffmpeg)
python3 - <<'PY' 2>/dev/null || missing+=(python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0)
import gi, cairo
gi.require_version("Gtk", "3.0"); gi.require_version("GdkPixbuf", "2.0"); gi.require_version("PangoCairo", "1.0")
from gi.repository import Gtk, GdkPixbuf, PangoCairo
gi.require_foreign("cairo")
PY
if ((${#missing[@]})); then
  echo "Faltan dependencias. Instálalas con:"
  echo "  sudo apt install $(printf '%s\n' "${missing[@]}" | sort -u | tr '\n' ' ')"
  exit 1
fi
# reproducción en la vista ampliada (opcional): GStreamer con gtksink
python3 - <<'PY' 2>/dev/null || echo "Aviso: sin GStreamer no hay reproducción en la vista ampliada (sí miniaturas y fotogramas). Para tenerla:
  sudo apt install gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gstreamer1.0-gtk3 gstreamer1.0-libav gstreamer1.0-plugins-good"
import gi; gi.require_version("Gst", "1.0"); from gi.repository import Gst; Gst.init(None)
import sys; sys.exit(0 if Gst.ElementFactory.find("gtksink") and Gst.ElementFactory.find("playbin") else 1)
PY
# aceleración por GPU (opcional): avisa si hay nodo DRM pero ningún driver VAAPI
if ls /dev/dri/renderD* >/dev/null 2>&1 && ! ls /usr/lib/*/dri/*_drv_video.so >/dev/null 2>&1; then
  echo "Aviso: hay GPU pero no se ve ningún driver VAAPI; para acelerar la decodificación:"
  echo "  sudo apt install intel-media-va-driver   # Intel Broadwell o posterior"
  echo "  sudo apt install i965-va-driver          # Intel anteriores (Sandy/Ivy/Haswell)"
  echo "  sudo apt install mesa-va-drivers         # AMD"
fi

mkdir -p "$BIN" "$APPS"
ln -sfn "$DIR/thumbsheet" "$BIN/thumbsheet"
sed -e "s|@BIN@|$BIN/thumbsheet|" -e "s|@ICON@|$DIR/icon.svg|" "$DIR/thumbsheet.desktop" > "$APPS/thumbsheet.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
echo "Instalado: $BIN/thumbsheet  (menú: thumbsheet; clic derecho en un vídeo → Abrir con → thumbsheet)"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "Aviso: $BIN no está en el PATH de esta shell." ;; esac
