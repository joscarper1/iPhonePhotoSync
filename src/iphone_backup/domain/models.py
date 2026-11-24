"""Modelos y máquina de estados. Sin dependencias de I/O."""
from __future__ import annotations

import posixpath
from dataclasses import dataclass
from enum import Enum


class ItemStatus(str, Enum):
    DISCOVERED = "discovered"      # visto en el iPhone, aún no copiado
    STAGED = "staged"              # copiado a _Staging con hash calculado
    VERIFIED = "verified"          # releído y comparado
    COMMITTED = "committed"        # renombrado al destino final y registrado
    FAILED = "failed"
    SKIPPED_DUP = "skipped_dup"    # contenido ya respaldado
    QUARANTINED = "quarantined"    # requiere decisión (p. ej. hash no coincide)


class VerifyStatus(str, Enum):
    NOT_VERIFIED = "not_verified"
    VERIFIED_FULL = "verified_full"          # SHA-1 del iPhone == SHA-1 leído, SHA-256 destino == SHA-256 leído
    VERIFIED_DEST_ONLY = "verified_dest_only"  # solo se comprobó destino (sin hash de origen)
    MISMATCH = "mismatch"


class SessionStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    FAILED = "failed"


class DateSource(str, Enum):
    EXIF = "exif"
    QUICKTIME_KEYS = "quicktime_keys"
    QUICKTIME_UTC = "quicktime_utc"
    FILE_MTIME = "file_mtime"
    UNKNOWN = "unknown"


class PairRole(str, Enum):
    STILL = "still"
    MOTION = "motion"


_ALLOWED: dict[ItemStatus, frozenset[ItemStatus]] = {
    ItemStatus.DISCOVERED: frozenset({ItemStatus.STAGED, ItemStatus.FAILED, ItemStatus.SKIPPED_DUP}),
    ItemStatus.STAGED: frozenset(
        {ItemStatus.VERIFIED, ItemStatus.FAILED, ItemStatus.QUARANTINED, ItemStatus.SKIPPED_DUP,
         ItemStatus.DISCOVERED}  # DISCOVERED: recuperación tras interrupción
    ),
    ItemStatus.VERIFIED: frozenset({ItemStatus.COMMITTED, ItemStatus.FAILED, ItemStatus.DISCOVERED}),
    ItemStatus.COMMITTED: frozenset(),
    ItemStatus.FAILED: frozenset({ItemStatus.DISCOVERED}),       # reintento
    ItemStatus.SKIPPED_DUP: frozenset(),
    ItemStatus.QUARANTINED: frozenset({ItemStatus.DISCOVERED}),  # tras decisión del usuario
}


class InvalidTransition(Exception):
    pass


def can_transition(old: ItemStatus, new: ItemStatus) -> bool:
    return new in _ALLOWED[old]


def check_transition(old: ItemStatus, new: ItemStatus) -> None:
    if not can_transition(old, new):
        raise InvalidTransition(f"Transición no permitida: {old.value} -> {new.value}")


@dataclass(frozen=True)
class DeviceInfo:
    udid: str
    product_type: str | None = None
    ios_version: str | None = None
    trusted: bool = True


@dataclass(frozen=True)
class DeviceFile:
    """Archivo visible en el iPhone. ``path`` es POSIX absoluto (p. ej. /DCIM/100APPLE/IMG_0001.HEIC)."""

    path: str
    size: int
    mtime_ns: int
    birthtime_ns: int | None = None

    @property
    def name(self) -> str:
        return posixpath.basename(self.path)

    @property
    def ext(self) -> str:
        return posixpath.splitext(self.path)[1].lower()

    @property
    def folder(self) -> str:
        return posixpath.dirname(self.path)

    @property
    def fingerprint(self) -> str:
        """Huella rápida (solo optimización para omitir lecturas; NO prueba de contenido)."""
        return f"{self.size}:{self.mtime_ns}"
