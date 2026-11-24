"""Confirmación de Live Photos por ContentIdentifier.

Mecanismo: el HEIC/JPEG (Apple MakerNote ``ContentIdentifier``) y el MOV (``Keys:ContentIdentifier``)
comparten el mismo UUID. Un par solo se considera CONFIRMADO si ambos lo declaran y coinciden.
Si no se confirma, ambos archivos se copian igualmente; solo se anota el motivo.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PairVerdict:
    confirmed: bool
    pair_id: str | None
    reason: str


def confirm_pair(still_cid: str | None, motion_cid: str | None) -> PairVerdict:
    if still_cid and motion_cid:
        if still_cid.lower() == motion_cid.lower():
            return PairVerdict(True, still_cid.lower(), "ContentIdentifier coincide")
        return PairVerdict(False, None, "ContentIdentifier distinto: mismo nombre pero no son un Live Photo")
    if not still_cid and not motion_cid:
        return PairVerdict(False, None, "Ninguno declara ContentIdentifier")
    return PairVerdict(False, None, "Solo uno de los archivos declara ContentIdentifier")
