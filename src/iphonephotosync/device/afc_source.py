"""Adaptador real: iPhone por USB mediante AFC (pymobiledevice3 sobre usbmuxd).

* Interfaz síncrona (``DeviceSource``) sobre la API asíncrona de pymobiledevice3: un hilo
  dedicado ejecuta el bucle de eventos y cada operación se envía con timeout.
* SOLO LECTURA: este módulo únicamente invoca ``listdir``, ``stat``, ``fopen(..., 'r')``,
  ``pread``, ``fclose`` y ``get_file_hash`` de AFC. Además restringe toda ruta a ``/DCIM``; la única excepción
  es la LECTURA de la miniatura que el iPhone guarda para un elemento (``/PhotoData/Thumbnails/V2/DCIM/<c>/<f>``,
  ruta validada por ``thumbnail_dir_for``).
* Los errores de pymobiledevice3 se traducen a ``DeviceError`` y subclases.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import posixpath
import logging
import threading
import time
from contextlib import AsyncExitStack
from datetime import datetime
from typing import Any, Coroutine, Iterator

from iphonephotosync.domain.models import DeviceFile, DeviceInfo
from iphonephotosync.ports.device import (
    MAX_THUMBNAIL_BYTES, PHOTOS_DB_DIR, PHOTOS_DB_FILES, DeviceDisconnected, DeviceError, DeviceLocked, DeviceNotFound, DeviceReadError,
    TrustRequired, thumbnail_dir_for,
)

DCIM_ROOT = "/DCIM"
log = logging.getLogger(__name__)


class DeviceResourcesExhausted(DeviceDisconnected):
    """AFC respondió NO_RESOURCES: el servicio del iPhone no acepta más aperturas.
    Se trata como pérdida de conexión (la sesión se detiene y es reanudable); NUNCA como fallo del archivo."""


def dt_to_ns(dt: datetime) -> int:
    """datetime (resolución µs, como lo entrega pymobiledevice3) -> ns. Redondea a µs para que
    la huella sea estable entre ejecuciones y no dependa del ruido de coma flotante."""
    return round(dt.timestamp() * 1_000_000) * 1000


def map_exception(exc: BaseException) -> Exception:
    """Traduce excepciones de pymobiledevice3 / red a los errores del puerto."""
    from pymobiledevice3 import exceptions as E

    if isinstance(exc, DeviceError):
        return exc
    msg = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, E.AfcException):          # antes que OSError: AfcException hereda de OSError
        if "NO_RESOURCES" in str(exc) or getattr(exc, "status", None) == 3:
            return DeviceResourcesExhausted(msg)  # el iPhone se quedó sin manejadores: no es culpa del archivo
        return DeviceReadError(msg)
    if isinstance(exc, (E.PasswordRequiredError, E.PasscodeRequiredError)):
        return DeviceLocked(msg)
    if isinstance(exc, E.PairingError):          # incluye diálogo pendiente / rechazado
        return TrustRequired(msg)
    if isinstance(exc, (E.NotPairedError, E.NotTrustedError)):
        return TrustRequired(msg)
    if isinstance(exc, (E.NoDeviceConnectedError, E.DeviceNotFoundError)):
        return DeviceNotFound(msg)
    if isinstance(exc, (E.ConnectionTerminatedError, E.MuxException, ConnectionError, OSError,
                        EOFError, asyncio.IncompleteReadError, asyncio.TimeoutError)):
        return DeviceDisconnected(msg)
    return DeviceError(msg)


class _LoopThread:
    """Bucle asyncio en un hilo propio; ``call`` bloquea con timeout y traduce errores."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="afc-loop", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro: Coroutine[Any, Any, Any], timeout: float) -> Any:
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise DeviceDisconnected(f"Sin respuesta del iPhone en {timeout:.0f} s") from None
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise map_exception(exc) from exc

    def stop(self) -> None:
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        if not self.loop.is_running():
            self.loop.close()


def _check_path(path: str) -> str:
    norm = posixpath.normpath(path)
    if not norm.startswith(DCIM_ROOT + "/") or ".." in norm.split("/"):
        raise DeviceReadError(f"Ruta fuera de /DCIM no permitida: {path}")
    return norm


class _AfcHandle:
    def __init__(self, src: "AfcDeviceSource", handle: int, size: int):
        self._src, self._h, self.size, self._closed = src, handle, size, False

    def read_at(self, offset: int, length: int) -> bytes:
        return self._src._call(self._src._afc.pread(self._h, length, offset))

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._src._call(self._src._afc.fclose(self._h), timeout=15)
            except DeviceError as e:
                self._src.close_failures += 1      # posible fuga de manejadores: se cuenta y se registra
                log.warning("fclose falló (%d acumulados): %s", self._src.close_failures, e)

    def __enter__(self) -> "_AfcHandle":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class AfcDeviceSource:
    def __init__(self, serial: str | None = None, pair_timeout: float = 60.0, op_timeout: float = 120.0,
                 autopair: bool = True):
        self._serial, self._pair_timeout, self._op_timeout, self._autopair = serial, pair_timeout, op_timeout, autopair
        self._runner: _LoopThread | None = None
        self._stack: AsyncExitStack | None = None
        self._afc: Any = None
        self._info: DeviceInfo | None = None
        self._listing_errors: list[tuple[str, str]] = []
        self.close_failures = 0

    # --- conexión ---
    def connect(self) -> "AfcDeviceSource":
        self._runner = _LoopThread()
        try:
            self._runner.call(self._aconnect(), timeout=self._pair_timeout + 30)
        except BaseException:
            self.close()
            raise
        return self

    async def _aconnect(self) -> None:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService

        self._stack = AsyncExitStack()
        try:
            lockdown = await create_using_usbmux(serial=self._serial, autopair=self._autopair,
                                                 pair_timeout=self._pair_timeout)
            await self._stack.enter_async_context(lockdown)
            values = getattr(lockdown, "all_values", {}) or {}
            self._info = DeviceInfo(udid=str(values.get("UniqueDeviceID") or self._serial or "unknown"),
                                    product_type=values.get("ProductType"),
                                    ios_version=values.get("ProductVersion"))
            self._afc = await self._stack.enter_async_context(AfcService(lockdown))
        except BaseException:
            await self._stack.aclose()
            raise

    @classmethod
    def for_testing(cls, afc: Any, info: DeviceInfo, op_timeout: float = 10.0) -> "AfcDeviceSource":
        """Inyecta un AFC falso (pruebas sin dispositivo)."""
        self = cls(op_timeout=op_timeout)
        self._runner, self._afc, self._info = _LoopThread(), afc, info
        return self

    def close(self) -> None:
        if self._runner:
            if self._stack:
                try:
                    self._runner.call(self._stack.aclose(), timeout=10)
                except DeviceError:
                    pass
            self._runner.stop()
            self._runner = None

    def __enter__(self) -> "AfcDeviceSource":
        return self if self._runner else self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    def _call(self, coro: Coroutine[Any, Any, Any], timeout: float | None = None) -> Any:
        if self._runner is None:
            coro.close()
            raise DeviceDisconnected("Conexión cerrada")
        return self._runner.call(coro, timeout or self._op_timeout)

    # --- interfaz DeviceSource (solo lectura) ---
    def info(self) -> DeviceInfo:
        if self._info is None:
            raise DeviceNotFound("No conectado")
        return self._info

    def listing_errors(self) -> list[tuple[str, str]]:
        return list(self._listing_errors)

    async def _enumerate(self) -> tuple[list[DeviceFile], list[tuple[str, str]]]:
        from pymobiledevice3.exceptions import AfcException

        files: list[DeviceFile] = []
        errors: list[tuple[str, str]] = []

        async def add(path: str) -> bool:
            """Devuelve True si es directorio."""
            try:
                st = await self._afc.stat(path)
            except AfcException as e:
                errors.append((path, str(e)))
                return False
            if st.get("st_ifmt") == "S_IFDIR":
                return True
            birth = st.get("st_birthtime")
            files.append(DeviceFile(path=path, size=int(st["st_size"]), mtime_ns=dt_to_ns(st["st_mtime"]),
                                    birthtime_ns=dt_to_ns(birth) if birth else None))
            return False

        for top in sorted(await self._afc.listdir(DCIM_ROOT)):
            top_path = posixpath.join(DCIM_ROOT, top)
            if not await add(top_path):
                continue
            try:
                children = await self._afc.listdir(top_path)
            except AfcException as e:
                errors.append((top_path, str(e)))
                continue
            for name in sorted(children):
                await add(posixpath.join(top_path, name))
        files.sort(key=lambda f: f.path)
        return files, errors

    def list_files(self) -> Iterator[DeviceFile]:
        files, self._listing_errors = self._call(self._enumerate(), timeout=max(self._op_timeout, 600))
        return iter(files)

    def open_read(self, path: str) -> _AfcHandle:
        norm = _check_path(path)
        size = int(self._call(self._afc.stat(norm))["st_size"])
        for attempt in range(3):                   # NO_RESOURCES puede ser transitorio (cierres en vuelo)
            try:
                handle = self._call(self._afc.fopen(norm, "r"))
                break
            except DeviceResourcesExhausted:
                if attempt == 2:
                    raise
                time.sleep(1.0 + attempt)
        return _AfcHandle(self, handle, size)

    async def _read_thumbnail(self, d: str) -> bytes | None:
        from pymobiledevice3.exceptions import AfcFileNotFoundError

        try:
            names = sorted(await self._afc.listdir(d))
        except AfcFileNotFoundError:
            return None
        if not names:
            return None
        p = posixpath.join(d, names[0])
        size = int((await self._afc.stat(p))["st_size"])
        if size <= 0 or size > MAX_THUMBNAIL_BYTES:
            return None
        h = await self._afc.fopen(p, "r")                    # solo lectura
        try:
            return await self._afc.pread(h, size, 0)
        finally:
            await self._afc.fclose(h)

    async def _copy_photos_db(self, dest_dir: str) -> str | None:
        import os
        from pymobiledevice3.exceptions import AfcFileNotFoundError

        os.makedirs(dest_dir, exist_ok=True)
        main = None
        for name in PHOTOS_DB_FILES:                          # lista cerrada: nada más de /PhotoData se puede leer
            p = f"{PHOTOS_DB_DIR}/{name}"
            try:
                size = int((await self._afc.stat(p))["st_size"])
            except AfcFileNotFoundError:
                if name == PHOTOS_DB_FILES[0]:
                    return None
                continue
            h = await self._afc.fopen(p, "r")                 # solo lectura
            try:
                with open(os.path.join(dest_dir, name), "wb") as out:
                    off = 0
                    while off < size:
                        block = await self._afc.pread(h, min(4 * 1024 * 1024, size - off), off)
                        if not block:
                            raise DeviceReadError(f"Lectura corta de {name} en {off}/{size}")
                        out.write(block)
                        off += len(block)
            finally:
                await self._afc.fclose(h)
            if name == PHOTOS_DB_FILES[0]:
                main = os.path.join(dest_dir, name)
        return main

    def copy_photos_database(self, dest_dir):
        """Copia Photos.sqlite(+wal/shm) del iPhone a ``dest_dir`` (local). Devuelve la ruta o None si no existe."""
        from pathlib import Path
        r = self._call(self._copy_photos_db(str(dest_dir)), timeout=900)
        return Path(r) if r else None

    def read_thumbnail(self, path: str) -> bytes | None:
        """Miniatura que el propio iPhone guarda para un elemento de /DCIM (solo lectura; ruta validada)."""
        return self._call(self._read_thumbnail(thumbnail_dir_for(path)), timeout=30)

    def source_sha1(self, path: str) -> bytes | None:
        norm = _check_path(path)
        try:
            return self._call(self._afc.get_file_hash(norm))
        except DeviceReadError as e:
            if "AfcFileNotFoundError" in str(e) or "no such file" in str(e).lower():
                raise
            return None   # el dispositivo no ofrece hash: la capa superior lo registrará como no disponible
