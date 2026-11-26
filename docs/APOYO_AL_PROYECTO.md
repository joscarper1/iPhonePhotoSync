# Apoyar este proyecto (componente reutilizable)

Menú **Ayuda → Apoyar este proyecto**: abre un diálogo con enlaces a **GitHub Sponsors** y **Ko-fi** (aportación puntual
y/o patrocinio mensual). Los pagos ocurren solo en el sitio del proveedor: el programa no tiene backend, no procesa pagos,
no recoge ni guarda datos de donantes y no usa rastreadores.

## Configurar los enlaces reales

Estado actual: solo **Ko-fi** (`https://ko-fi.com/joscarper`) está activo; GitHub Sponsors está desactivado hasta que
se configure su URL.

Edita `src/iphonephotosync/support_config.py`:

```python
"providers": {
    "githubSponsors": {"enabled": True, "url": "https://github.com/sponsors/TU_USUARIO"},
    "koFi":           {"enabled": True, "url": "https://ko-fi.com/TU_USUARIO"},
},
```

- Un proveedor con `enabled: False`, URL vacía, malformada, `http`, con parámetros o de otro dominio **no se muestra**.
- Si no queda ningún proveedor válido (o `enabled: False`) **no aparece el menú**, y la ventana queda como antes.
- Con un solo proveedor o una sola modalidad (`donationOptions`) no quedan secciones vacías.
- `appearance.displayMode`: `"modal"` (bloquea la ventana) o `"window"` (no bloqueante). `showProviderLogos` muestra un
  glifo discreto junto al nombre; `showFooterMessage` el mensaje final.
- Textos: `language` (`es`/`en`), `title`, `description` y `texts` (cualquier clave de `support/texts.py`) los sustituyen.
  Para otro idioma, añade un diccionario en `texts.py`.

Las URLs de GitHub Sponsors se abren con `?frequency=one-time` / `?frequency=recurring` según la sección. Ko-fi abre el
perfil (la modalidad recurrente depende de que esté habilitada en tu cuenta).

## Reutilizar en otra aplicación PySide6

1. Copia la carpeta `src/iphonephotosync/support/` (y ajusta el prefijo de importación si cambia el paquete).
2. Crea tu propio diccionario de configuración con la misma estructura.
3. En tu ventana principal: `install_support_menu(ventana, SupportConfig.from_dict(MI_CONFIG))`.

Estructura: `config.py` (configuración y enlaces válidos, sin Qt) · `providers.py` (definición y validación por
proveedor; añadir otro = una entrada) · `texts.py` (textos/traducción) · `styles.py` (QSS con la paleta del sistema, claro
y oscuro) · `dialog.py` (interfaz) · `menu.py` (integración con el menú).

## Pruebas

```bash
python -m pytest tests/test_support.py
```
