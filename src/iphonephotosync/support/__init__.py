"""Componente reutilizable «Apoyar este proyecto» (donaciones voluntarias por enlaces externos).

Sin backend, sin pagos propios, sin datos de donantes. Uso:

    from iphonephotosync.support import build_model, SupportConfig, install_support_menu
    install_support_menu(main_window, SupportConfig.from_dict(MI_CONFIG))

Para reutilizarlo en otra aplicación Qt, copiar esta carpeta y pasar otra configuración.
"""
from iphonephotosync.support.config import SupportConfig, SupportModel, build_model



def install_support_menu(window, config):
    """Ver ``support.menu.install_support_menu``. Qt se importa aquí para que config/proveedores no lo requieran."""
    from iphonephotosync.support.menu import install_support_menu as _install
    return _install(window, config)


__all__ = ["SupportConfig", "SupportModel", "build_model", "install_support_menu"]
