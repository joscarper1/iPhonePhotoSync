"""Puerto de geocodificación inversa (coordenadas -> lugar). Solo recibe coordenadas, nunca archivos."""
from __future__ import annotations

from typing import Protocol

from iphonephotosync.domain.place import Place


class Geocoder(Protocol):
    def lookup(self, lat: float, lon: float) -> Place | None:
        """Lugar para las coordenadas, o None si no hay uno confiable (no se inventa)."""
