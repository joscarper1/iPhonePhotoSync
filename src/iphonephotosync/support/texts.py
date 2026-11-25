"""Textos por idioma. Cualquier clave se puede sustituir desde la configuración (``texts``) y se puede añadir otro idioma."""
from __future__ import annotations

TEXTS: dict[str, dict[str, str]] = {
    "es": {
        "menu_help": "&Ayuda",
        "menu_item": "Apoyar este proyecto",
        "title": "¿Te gusta esta herramienta?",
        "description": "Este proyecto es gratuito. Tu apoyo voluntario ayuda a mantenerlo, mejorar las funciones "
                       "existentes y desarrollar nuevas herramientas.",
        "one_time": "Hacer una contribución puntual",
        "recurring": "Convertirte en patrocinador mensual",
        "open_hint": "Se abre en tu navegador, en el sitio de {provider}.",
        "footer": "Cada aportación cuenta. ¡Gracias por apoyar el software independiente!",
        "privacy": "Este programa no recibe ni guarda datos de pagos; todo se gestiona en el sitio del proveedor.",
        "close": "Cerrar",
    },
    "en": {
        "menu_help": "&Help",
        "menu_item": "Support this project",
        "title": "Enjoying this tool?",
        "description": "This project is free to use. Your voluntary support helps maintain it, improve existing "
                       "features and develop new tools.",
        "one_time": "Make a one-time contribution",
        "recurring": "Become a monthly sponsor",
        "open_hint": "Opens in your browser, on {provider}'s site.",
        "footer": "Every contribution makes a difference. Thank you for supporting independent software development!",
        "privacy": "This app never receives or stores payment data; everything is handled on the provider's site.",
        "close": "Close",
    },
}
DEFAULT_LANGUAGE = "es"


def resolve_texts(language: str, overrides: dict[str, str] | None = None) -> dict[str, str]:
    base = dict(TEXTS[DEFAULT_LANGUAGE])
    base.update(TEXTS.get((language or "").lower()[:2], {}))
    base.update({k: v for k, v in (overrides or {}).items() if isinstance(v, str) and v.strip() and k in base})
    return base
