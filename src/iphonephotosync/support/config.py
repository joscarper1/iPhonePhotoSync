"""Configuración y resolución de enlaces. Python puro (sin Qt): se puede probar y reutilizar sin interfaz."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from iphonephotosync.support.providers import ONE_TIME, PROVIDERS, RECURRING, Provider
from iphonephotosync.support.texts import resolve_texts

DISPLAY_MODES = ("modal", "window")      # modal: bloquea la ventana principal; window: ventana no bloqueante


@dataclass(frozen=True)
class SupportConfig:
    enabled: bool = True
    project_name: str = ""
    title: str = ""                       # vacío -> texto por idioma
    description: str = ""
    language: str = "es"
    providers: dict[str, dict] = field(default_factory=dict)       # {"koFi": {"enabled": True, "url": "https://..."}}
    one_time: bool = True
    recurring: bool = True
    show_provider_logos: bool = True
    show_footer_message: bool = True
    display_mode: str = "modal"
    texts: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "SupportConfig":
        """Acepta la estructura documentada (camelCase). Valores desconocidos o inválidos se ignoran, nunca fallan."""
        d = d if isinstance(d, dict) else {}
        opts, look = d.get("donationOptions") or {}, d.get("appearance") or {}
        mode = look.get("displayMode")
        prov = d.get("providers")
        return cls(
            enabled=d.get("enabled", True) is True,
            project_name=str(d.get("projectName") or "").strip(),
            title=str(d.get("title") or "").strip(), description=str(d.get("description") or "").strip(),
            language=str(d.get("language") or "es"),
            providers={k: v for k, v in prov.items() if isinstance(v, dict)} if isinstance(prov, dict) else {},
            one_time=opts.get("oneTime", True) is True, recurring=opts.get("recurring", True) is True,
            show_provider_logos=look.get("showProviderLogos", True) is True,
            show_footer_message=look.get("showFooterMessage", True) is True,
            display_mode=mode if mode in DISPLAY_MODES else "modal",
            texts=d.get("texts") if isinstance(d.get("texts"), dict) else {})


@dataclass(frozen=True)
class Link:
    provider: Provider
    kind: str                             # ONE_TIME | RECURRING
    url: str


@dataclass(frozen=True)
class SupportModel:
    """Lo que realmente se puede mostrar: solo enlaces válidos de proveedores habilitados."""
    config: SupportConfig
    texts: dict[str, str]
    links: tuple[Link, ...]

    @property
    def available(self) -> bool:
        return self.config.enabled and bool(self.links)

    def links_for(self, kind: str) -> list[Link]:
        return [lk for lk in self.links if lk.kind == kind]

    @property
    def kinds(self) -> list[str]:
        return [k for k in (ONE_TIME, RECURRING) if self.links_for(k)]

    @property
    def heading(self) -> str:
        return self.config.title or self.texts["title"]

    @property
    def body(self) -> str:
        return self.config.description or self.texts["description"]


def build_model(config: SupportConfig) -> SupportModel:
    links: list[Link] = []
    for pid, provider in PROVIDERS.items():                 # los proveedores desconocidos de la configuración se ignoran
        cfg = config.providers.get(pid) or {}
        if cfg.get("enabled") is not True:
            continue
        base = provider.validate(cfg.get("url"))
        if base is None:                                    # vacío, malformado o de otro dominio: no se muestra
            continue
        for kind, wanted in ((ONE_TIME, config.one_time), (RECURRING, config.recurring)):
            if wanted:
                links.append(Link(provider, kind, provider.url_for(base, kind)))
    return SupportModel(config, resolve_texts(config.language, config.texts), tuple(links))
