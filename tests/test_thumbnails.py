"""Miniaturas: nativa primero, luego el original (foto / primer fotograma), si no, None; todo en solo lectura."""
import io

import pytest

PIL = pytest.importorskip("PIL")
av = pytest.importorskip("av")
np = pytest.importorskip("numpy")
from PIL import Image

from iphone_backup.app.thumbnails import SIZE, ThumbnailService
from iphone_backup.device.simulated import FaultPlan, SimulatedDeviceSource
from iphone_backup.ports.device import DeviceDisconnected, DeviceReadError, thumbnail_dir_for


def jpeg_bytes(w, h, color=(200, 30, 30), size=None):
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, "JPEG")
    return out.getvalue()


def make_video(path, w=128, h=72, rotation=0, frames=12):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = av.open(str(path), "w", format="mov")
    s = c.add_stream("libx264", rate=30)
    s.width, s.height, s.pix_fmt = w, h, "yuv420p"
    if rotation:
        s.set_display_rotation(rotation)
    img = np.zeros((h, w, 3), "uint8")
    img[:, : w // 2] = (255, 0, 0)                      # mitad izquierda roja: permite comprobar el giro
    for _ in range(frames):
        for p in s.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
            c.mux(p)
    for p in s.encode():
        c.mux(p)
    c.close()


@pytest.fixture
def phone(tmp_path):
    root = tmp_path / "iphone"
    d = root / "DCIM/100APPLE"
    d.mkdir(parents=True)
    Image.new("RGB", (1600, 1200), (10, 200, 10)).save(d / "IMG_0001.JPG", quality=90)    # sin miniatura nativa
    Image.new("RGB", (640, 480), (10, 10, 200)).save(d / "IMG_0002.PNG")
    Image.new("RGB", (800, 600), (200, 200, 10)).save(d / "IMG_0003.JPG")                  # con miniatura nativa
    nat = root / "PhotoData/Thumbnails/V2/DCIM/100APPLE/IMG_0003.JPG"
    nat.mkdir(parents=True)
    (nat / "5005.JPG").write_bytes(jpeg_bytes(480, 360, (1, 2, 3)))
    make_video(d / "IMG_0004.MOV")
    make_video(d / "IMG_0005.MOV", w=128, h=72, rotation=90)
    (d / "IMG_0006.JPG").write_bytes(b"esto no es una imagen")
    return root


def files_by_name(src):
    return {f.name: f for f in src.list_files()}


def decode(th):
    return Image.open(io.BytesIO(th.data))


def test_native_thumbnail_is_preferred_and_cached_locally(phone, tmp_path):
    src = SimulatedDeviceSource(phone)
    svc = ThumbnailService(src, tmp_path / "cache", "U")
    f = files_by_name(src)["IMG_0003.JPG"]
    before = src.bytes_read
    th = svc.get(f)
    assert th.origin == "native" and decode(th).size[0] <= SIZE and src.bytes_read == before   # no tocó el original
    assert svc.get(f).origin == "cache"                                          # segunda vez: del disco local
    assert svc.native_hits == 1 and svc.cache_hits == 1
    assert len(list((tmp_path / "cache").glob("*.jpg"))) == 1


def test_falls_back_to_the_original_for_photos(phone, tmp_path):
    src = SimulatedDeviceSource(phone)
    svc = ThumbnailService(src, tmp_path / "c", "U")
    for name, (w, h) in {"IMG_0001.JPG": (1600, 1200), "IMG_0002.PNG": (640, 480)}.items():
        th = svc.get(files_by_name(src)[name])
        assert th.origin == "original"
        im = decode(th)
        assert max(im.size) == SIZE and abs(im.size[0] / im.size[1] - w / h) < 0.02        # conserva la proporción


def test_video_first_frame_reads_little_and_respects_rotation(phone, tmp_path):
    src = SimulatedDeviceSource(phone)
    svc = ThumbnailService(src, tmp_path / "c", "U")
    fl = files_by_name(src)
    flat = decode(svc.get(fl["IMG_0004.MOV"]))
    assert flat.size[0] > flat.size[1]                                           # 128x72 apaisado
    assert flat.getpixel((5, flat.size[1] // 2))[0] > 150                        # lado izquierdo rojo
    rot = decode(svc.get(fl["IMG_0005.MOV"]))
    assert rot.size[1] > rot.size[0]                                             # con giro de 90°: sale vertical
    # convención de la matriz de giro de libav (antihorario): el lado izquierdo rojo pasa a quedar ABAJO
    assert rot.getpixel((rot.size[0] // 2, rot.size[1] - 4))[0] > 150 and rot.getpixel((rot.size[0] // 2, 3))[0] < 100


def test_unreadable_files_give_none_not_an_invented_image(phone, tmp_path):
    src = SimulatedDeviceSource(phone)
    svc = ThumbnailService(src, tmp_path / "c", "U")
    assert svc.get(files_by_name(src)["IMG_0006.JPG"]) is None and svc.misses == 1
    assert list((tmp_path / "c").glob("*.jpg")) == []                            # lo fallido no se guarda como si fuera válido


def test_corrupt_native_thumbnail_falls_back_to_the_original(phone, tmp_path):
    (phone / "PhotoData/Thumbnails/V2/DCIM/100APPLE/IMG_0003.JPG/5005.JPG").write_bytes(b"corrupta")
    src = SimulatedDeviceSource(phone)
    th = ThumbnailService(src, None, "U").get(files_by_name(src)["IMG_0003.JPG"])
    assert th.origin == "original"


def test_device_loss_propagates_so_loading_can_stop(phone, tmp_path):
    src = SimulatedDeviceSource(phone)
    f = files_by_name(src)["IMG_0001.JPG"]
    src.faults.disconnect_after_bytes = 10
    with pytest.raises(DeviceDisconnected):
        ThumbnailService(src, None, "U").get(f)


def test_cache_is_per_file_version(phone, tmp_path):
    src = SimulatedDeviceSource(phone)
    svc = ThumbnailService(src, tmp_path / "c", "U")
    f = files_by_name(src)["IMG_0001.JPG"]
    svc.get(f)
    import dataclasses
    changed = dataclasses.replace(f, mtime_ns=f.mtime_ns + 1)                    # el archivo cambió en el iPhone
    assert svc.get(changed).origin == "original"                                 # no reutiliza la miniatura vieja


def test_only_the_thumbnail_folder_outside_dcim_is_readable():
    assert thumbnail_dir_for("/DCIM/100APPLE/IMG_1.HEIC") == "/PhotoData/Thumbnails/V2/DCIM/100APPLE/IMG_1.HEIC"
    for bad in ["/PhotoData/x/y", "/DCIM/../etc/passwd", "/DCIM/100APPLE", "/DCIM/a/b/c", "/DCIM//IMG.JPG", "DCIM/a/b"]:
        with pytest.raises(DeviceReadError):
            thumbnail_dir_for(bad)
    src = SimulatedDeviceSource(__import__("pathlib").Path("."))
    with pytest.raises(DeviceReadError):
        src.read_thumbnail("/PhotoData/Mutations/x")                              # nada fuera de lo permitido
