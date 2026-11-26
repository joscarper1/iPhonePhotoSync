# Política de firma de código

Los ejecutables de Windows de iPhonePhotoSync se firman con un certificado emitido a la **SignPath Foundation**.

> Free code signing provided by [SignPath.io](https://about.signpath.io), certificate by [SignPath Foundation](https://signpath.org).

## Qué se firma

Solo los binarios de Windows (`iphonephotosync.exe` y el instalador `iphonephotosync-setup.exe`) compilados por el flujo
automático de GitHub Actions (`.github/workflows/release.yml`) a partir del código de este repositorio público, en la
etiqueta de versión correspondiente. Nada se firma desde equipos personales.

## Roles

- **Autor y responsable:** Jose Carlos Perez ([@joscarper1](https://github.com/joscarper1)).
- **Revisor y aprobador de firmas:** Jose Carlos Perez. Cada firma de una versión se aprueba manualmente.

## Privacidad

El programa no envía datos a ningún servicio: no hay telemetría ni conexión a Internet (ver el README, sección
Privacidad). Fotos, vídeos y metadatos permanecen en el equipo del usuario.

## Verificación

Cada Release incluye `SHA256SUMS.txt`. El código fuente exacto de cada versión es la etiqueta correspondiente.
