"""Configuración de «Apoyar este proyecto» para ESTA aplicación. Es lo único que hay que editar al reutilizar el componente.

Con ``url`` vacía (o sea de otro dominio) el proveedor no se muestra, y si ninguno es válido el menú no aparece.
"""
SUPPORT = {
    "enabled": True,
    "projectName": "iPhonePhotoSync",
    "language": "es",
    # "title": "", "description": "",              # opcionales: vacío = texto por defecto del idioma
    "providers": {
        "githubSponsors": {"enabled": False, "url": ""},      # activar y poner https://github.com/sponsors/<usuario>
        "koFi": {"enabled": True, "url": "https://ko-fi.com/joscarper"},
    },
    "donationOptions": {"oneTime": True, "recurring": True},
    "appearance": {"showProviderLogos": True, "showFooterMessage": True, "displayMode": "modal"},
    # "texts": {"footer": "..."},                  # cualquier clave de support/texts.py
}
