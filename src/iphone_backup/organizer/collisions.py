"""Resolución determinista de colisiones de nombre. Nunca sobrescribe.

Se ejecuta en el momento de confirmar (cuando ya se conoce el SHA-256). Un grupo de archivos
(foto + MOV + AAE de un Live Photo) comparte base y sufijo: se renombran en bloque.
"""
from __future__ import annotations

import os
from typing import Callable, Sequence


class NameResolutionError(Exception):
    pass


def resolve_group_names(original_names: Sequence[str], key: str, taken: Callable[[str], bool],
                        max_attempts: int = 1000) -> list[str]:
    """Devuelve los nombres finales del grupo.

    * Si TODOS los nombres originales están libres, se conservan.
    * Si alguno choca, todo el grupo recibe el mismo sufijo ``__<key8>`` (y ``_2``, ``_3``… si hiciera falta).
    ``taken(nombre)`` debe comparar sin distinguir mayúsculas (NTFS/exFAT) e incluir lo ya planificado.
    ``key`` es un identificador estable (SHA-256 o pair_id) y el resultado solo depende de él.
    """
    names = list(original_names)
    if len({n.casefold() for n in names}) != len(names):
        raise NameResolutionError("Nombres duplicados dentro del mismo grupo")
    if not any(taken(n) for n in names):
        return names
    token = key.lower()[:8]
    for attempt in range(1, max_attempts + 1):
        suffix = f"__{token}" if attempt == 1 else f"__{token}_{attempt}"
        candidates = []
        for n in names:
            stem, ext = os.path.splitext(n)
            candidates.append(f"{stem}{suffix}{ext}")
        if not any(taken(c) for c in candidates):
            return candidates
    raise NameResolutionError("No se pudo resolver la colisión de nombres")
