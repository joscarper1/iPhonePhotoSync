"""Lugar resuelto a partir de coordenadas."""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class PlaceLevel(IntEnum):
    CITY = 0      # más detallado
    REGION = 1
    COUNTRY = 2   # menos detallado


@dataclass(frozen=True)
class Place:
    country_code: str
    country: str
    region: str | None
    city: str | None
    city_id: int | None
    distance_km: float
    level: PlaceLevel
    ambiguous_city_name: bool = False     # existe otra ciudad con el mismo nombre en el mismo país
    ambiguous_in_region: bool = False     # ... y también en la misma región

    def folder_name(self) -> str:
        """Nombre de carpeta legible: 'Barcelona, España' / 'Cataluña, España' / 'España'."""
        if self.level is PlaceLevel.CITY and self.city:
            name = self.city
            if self.ambiguous_city_name and self.region:
                name = f"{name} ({self.region})"
            if self.ambiguous_in_region and self.city_id:
                name = f"{name} [{self.city_id}]"
            return f"{name}, {self.country}"
        if self.level is PlaceLevel.REGION and self.region:
            return f"{self.region}, {self.country}"
        return self.country

    def coarsened(self, cap: PlaceLevel) -> "Place":
        """Reduce el detalle hasta como mucho ``cap`` (p. ej. cap=REGION descarta la ciudad)."""
        if self.level >= cap:
            return self
        if cap is PlaceLevel.REGION and self.region:
            return Place(self.country_code, self.country, self.region, None, None, self.distance_km,
                         PlaceLevel.REGION)
        return Place(self.country_code, self.country, None, None, None, self.distance_km, PlaceLevel.COUNTRY)
