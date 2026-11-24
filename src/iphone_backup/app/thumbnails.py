"""Miniaturas de lo pendiente de respaldar (solo lectura del iPhone; se guardan únicamente en el disco local).

Orden de preferencia (medido con un iPhone real, ver docs/viability_report.md):
  1. la miniatura que el propio iPhone ya guarda (15-70 ms, ya con la rotación correcta);
  2. generarla desde el original: foto (Pillow; HEIC con pillow-heif) o primer fotograma de un vídeo (PyAV,
     leyendo solo ~1-3 MB aunque el vídeo pese GB);
  3. si todo falla, ``None``: la interfaz muestra un icono "sin vista previa" (nunca se inventa una imagen).
Los errores de conexión con el iPhone SÍ se propagan (para detener la carga); cualquier otro fallo da ``None``.
"""
from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass
from pathlib import Path

from iphone_backup.device.remotefile import RemoteFile
from iphone_backup.domain.models import DeviceFile
from iphone_backup.ports.device import DeviceDisconnected, DeviceLocked, DeviceNotFound, TrustRequired

log = logging.getLogger(__name__)

SIZE = 320                        # lado máximo de la miniatura guardada
VIDEO_EXTS = {".mov", ".mp4", ".m4v"}
MAX_ORIGINAL_PHOTO = 80 * 1024 * 1024          # no se baja un original gigante solo para una vista previa
FATAL = (DeviceDisconnected, DeviceLocked, DeviceNotFound, TrustRequired)


@dataclass
class Thumb:
    data: bytes                   # JPEG
    origin: str                   # 'cache' | 'native' | 'original'


def _jpeg(im, size: int = SIZE) -> bytes:
    from PIL import Image
    im = im.convert("RGB")
    im.thumbnail((size, size), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=80)
    return out.getvalue()


class ThumbnailService:
    def __init__(self, source, cache_dir: Path | None, udid: str = ""):
        self.source = source
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.udid = udid
        self.native_hits = self.original_hits = self.cache_hits = self.misses = 0
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:  # noqa: BLE001 - sin pillow-heif los HEIC solo tendrán la miniatura nativa
            pass

    # ------------------------------------------------------------------ caché local
    def _key(self, f: DeviceFile) -> Path | None:
        if self.cache_dir is None:
            return None
        h = hashlib.sha1(f"{self.udid}|{f.path}|{f.fingerprint}|{SIZE}".encode()).hexdigest()
        return self.cache_dir / f"{h}.jpg"

    def _from_cache(self, f: DeviceFile) -> bytes | None:
        p = self._key(f)
        try:
            return p.read_bytes() if p and p.is_file() else None
        except OSError:
            return None

    def _to_cache(self, f: DeviceFile, data: bytes) -> None:
        p = self._key(f)
        if not p:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(p)
        except OSError as e:
            log.warning("No se pudo guardar la miniatura: %s", e)

    # ------------------------------------------------------------------ API
    def get(self, f: DeviceFile) -> Thumb | None:
        cached = self._from_cache(f)
        if cached:
            self.cache_hits += 1
            return Thumb(cached, "cache")
        data = origin = None
        try:
            data = self._native(f)
            origin = "native"
            if data is None:
                data = self._from_original(f)
                origin = "original"
        except FATAL:
            raise
        except Exception as e:  # noqa: BLE001 - una vista previa que falla no debe romper nada
            log.info("Sin vista previa para %s: %s", f.name, e)
            data = None
        if data is None:
            self.misses += 1
            return None
        if origin == "native":
            self.native_hits += 1
        else:
            self.original_hits += 1
        self._to_cache(f, data)
        return Thumb(data, origin)

    # ------------------------------------------------------------------ fuentes
    def _native(self, f: DeviceFile) -> bytes | None:
        reader = getattr(self.source, "read_thumbnail", None)
        if reader is None:
            return None
        try:
            raw = reader(f.path)
        except FATAL:
            raise
        except Exception as e:  # noqa: BLE001 - miniatura nativa ilegible: se intenta con el original
            log.info("Miniatura nativa no disponible para %s: %s", f.name, e)
            return None
        if not raw:
            return None
        try:
            from PIL import Image, ImageOps
            with Image.open(io.BytesIO(raw)) as im:
                im = ImageOps.exif_transpose(im)
                return _jpeg(im)
        except Exception as e:  # noqa: BLE001
            log.info("Miniatura nativa corrupta para %s: %s", f.name, e)
            return None

    def _from_original(self, f: DeviceFile) -> bytes | None:
        if f.ext.lower() in VIDEO_EXTS:
            return self._video_first_frame(f)
        if f.size > MAX_ORIGINAL_PHOTO:
            return None
        return self._photo(f)

    def _photo(self, f: DeviceFile) -> bytes | None:
        from PIL import Image, ImageOps
        with self.source.open_read(f.path) as h:
            rf = RemoteFile(h)
            with Image.open(rf.buffered()) as im:
                if im.format == "JPEG":
                    im.draft("RGB", (SIZE * 2, SIZE * 2))       # decodifica a escala reducida: mucho más rápido
                im = ImageOps.exif_transpose(im)
                return _jpeg(im)

    def _video_first_frame(self, f: DeviceFile) -> bytes | None:
        import av
        from PIL import Image
        with self.source.open_read(f.path) as h:
            rf = RemoteFile(h)
            with av.open(rf.buffered(), mode="r") as c:
                if not c.streams.video:
                    return None
                for frame in c.decode(video=0):
                    im = frame.to_image()
                    rot = getattr(frame, "rotation", 0) or 0       # matriz de giro: vídeos verticales de iPhone
                    if rot:
                        im = im.rotate(rot, expand=True)
                    return _jpeg(im)
        return None
