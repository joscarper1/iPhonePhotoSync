"""Definición de proveedores de apoyo. Para añadir uno nuevo basta una entrada en ``PROVIDERS``.

Cada proveedor fija a qué dominio y forma de ruta puede apuntar su enlace: nada fuera de eso se abre jamás.
No hay URLs personales aquí; las pone cada aplicación en su configuración.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

ONE_TIME, RECURRING = "one_time", "recurring"
_PLACEHOLDER = re.compile(r"^(usuario|username|user|your-?name|tu-?usuario)$", re.I)


@dataclass(frozen=True)
class Provider:
    id: str
    label: str                       # nombre visible (identificación del proveedor)
    icon: str                        # glifo Unicode discreto (sin imágenes ni recursos externos)
    hosts: tuple[str, ...]           # únicos dominios permitidos
    path: re.Pattern                 # forma de ruta válida del perfil
    frequency: dict[str, str] | None = None   # parámetro ``frequency`` por tipo, si el proveedor lo admite

    def validate(self, url: str) -> str | None:
        """Devuelve la URL normalizada (https, sin parámetros) o ``None`` si está vacía, malformada o fuera del dominio."""
        if not isinstance(url, str) or not url.strip() or any(c.isspace() or ord(c) < 32 for c in url.strip()):
            return None
        try:
            p = urlsplit(url.strip())
            host, port = (p.hostname or "").lower(), p.port
        except ValueError:
            return None
        if p.scheme != "https" or p.username or p.password or port or p.query or p.fragment:
            return None
        if host.removeprefix("www.") not in self.hosts:
            return None
        m = self.path.fullmatch(p.path)
        if not m or _PLACEHOLDER.match(m.group("user")):
            return None
        return f"https://{self.hosts[0]}{p.path.rstrip('/')}"

    def url_for(self, base: str, kind: str) -> str:
        """URL final para una modalidad. Solo se añade ``frequency`` si el proveedor lo documenta."""
        if self.frequency and kind in self.frequency:
            return f"{base}?frequency={self.frequency[kind]}"
        return base


PROVIDERS: dict[str, Provider] = {
    "githubSponsors": Provider(
        "githubSponsors", "GitHub Sponsors", "♥", ("github.com",),
        re.compile(r"/sponsors/(?P<user>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/?"),
        frequency={ONE_TIME: "one-time", RECURRING: "recurring"}),
    "koFi": Provider(
        "koFi", "Ko-fi", "☕", ("ko-fi.com",),
        re.compile(r"/(?P<user>[A-Za-z0-9_.-]{1,64})/?")),
}
