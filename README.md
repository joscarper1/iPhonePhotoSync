# iPhonePhotoSync

Copia **las fotos y los vídeos originales** de tu iPhone a tu propio disco por USB, **sin modificar nada en el
teléfono**, con verificación SHA-256 y de forma **incremental**. Todo es local: no se sube nada a ningún servicio.

> Estado: versión 0.1 (beta). **Windows** está verificado con un iPhone real. **Linux y macOS** son
> **experimentales**: el código es portable y se prueba en integración continua, pero aún no se ha verificado el acceso
> a un iPhone real en esos sistemas.

## Qué hace

- Lee `/DCIM` del iPhone por USB (HEIC, JPEG, PNG, MOV, MP4, Live Photos y `.AAE`), **solo en lectura**. Nunca
  escribe, mueve ni borra nada en el teléfono. No recomprime ni convierte nada.
- Escanea y te dice **qué tienes en el iPhone que aún no está respaldado** (y por qué), comparando con tu respaldo
  anterior y con tu biblioteca existente. Muestra una galería de miniaturas para que elijas qué respaldar.
- Organiza por **Álbum > Año > Mes > Lugar** usando la fecha de captura, el GPS (geocodificación **sin Internet**) y
  los álbumes del iPhone. Lo que no está en un álbum va en `Año/Mes/Lugar`. Fecha desconocida → `Fecha desconocida`.
- Copia de forma **atómica** (archivo temporal → renombrado), verifica con **SHA-256** y **nunca sobrescribe** un
  archivo existente. Si se corta, se puede reanudar sin repetir lo ya copiado.
- Informa claramente **qué se copió, qué no y por qué**, y puede generar la lista de lo que **no** debes borrar del
  iPhone (ver `docs/GUIA_LIBERAR_ESPACIO.md`).

## Instalación

Descarga el paquete de tu sistema y descomprímelo. Enlaces directos a **la última versión**:

| Sistema | Descarga |
|---|---|
| Windows 10/11 (x64) | [iphonephotosync-setup.exe](https://github.com/joscarper1/iPhonePhotoSync/releases/latest/download/iphonephotosync-setup.exe) (instalador) · [iphonephotosync-windows-x64.zip](https://github.com/joscarper1/iPhonePhotoSync/releases/latest/download/iphonephotosync-windows-x64.zip) (portátil) |
| macOS (Apple Silicon) | [iphonephotosync-macos-arm64.zip](https://github.com/joscarper1/iPhonePhotoSync/releases/latest/download/iphonephotosync-macos-arm64.zip) |
| Linux (x64) | [iphonephotosync-linux-x64.tar.gz](https://github.com/joscarper1/iPhonePhotoSync/releases/latest/download/iphonephotosync-linux-x64.tar.gz) |

**Todas las versiones** (la última y las anteriores, con sus notas): <https://github.com/joscarper1/iPhonePhotoSync/releases>.
Cada versión incluye `SHA256SUMS.txt` para comprobar que el archivo no se alteró.

| Sistema | Requisitos |
|---|---|
| **Windows 10/11** | Tener instalado **Apple Devices** (Microsoft Store) o iTunes, que proporciona el controlador USB del iPhone. |
| **macOS** (Apple Silicon) | Nada más. Primera vez: clic derecho → *Abrir* (la app no está firmada ni notarizada). |
| **Linux** | Paquete `usbmuxd` instalado y en marcha, y `perl` (para ExifTool, incluido en la app). |

En el iPhone: desbloquéalo y pulsa **Confiar** cuando conectes el cable.

Los ejecutables **no están firmados** (no hay certificado de pago): Windows SmartScreen o macOS Gatekeeper pueden
avisarte. Puedes comprobar la integridad con `SHA256SUMS.txt` de la Release o compilar desde el código fuente.

## Uso

Abre la aplicación, elige el **disco de destino** (la carpeta que *contiene* `iPhone_Photos`, no la propia biblioteca) y
pulsa **Escanear iPhone**. Revisa lo pendiente, marca lo que quieras y pulsa **Respaldar**.

Desde el código fuente (Python 3.12+):

```bash
python -m pip install -e ".[dev]"
python tools/fetch_exiftool.py        # descarga ExifTool a tools/exiftool
iphonephotosync-ui                      # interfaz
iphonephotosync --help                  # línea de comandos (scan, preview, backup, verify, status, deletion-report)
```

Miniaturas de vídeos sin miniatura propia en el iPhone: el paquete ligero no incluye ffmpeg; desde el código fuente
se activan con `python -m pip install -e ".[video]"`.

Modo de pruebas sin iPhone: `iphonephotosync-ui --simulate CARPETA` (una carpeta con subcarpeta `DCIM/`).

## Compilar los paquetes

```bash
python -m pip install -e ".[dev]"
python tools/fetch_exiftool.py
python -m PyInstaller --noconfirm --clean packaging/iphonephotosync.spec
```

La publicación de versiones es automática: al subir una etiqueta `vX.Y.Z`, GitHub Actions compila para Windows, Linux
y macOS y crea la Release con los paquetes y sus sumas SHA-256 (`.github/workflows/release.yml`).

## Limitaciones conocidas

- Los **originales sin editar** de las fotos editadas viven en `/PhotoData/Mutations` y **no** se respaldan: se copia la
  versión visible en `/DCIM` más su `.AAE`. El informe para liberar espacio lo marca como "no a salvo".
- El servicio de archivos del iPhone (AFC) puede responder `NO_RESOURCES` tras miles de aperturas seguidas y quedar
  bloqueado hasta reconectar o reiniciar el teléfono. La herramienta lo detecta, detiene la sesión de forma limpia y se
  puede reanudar; la causa exacta no está identificada. Usa el escaneo rápido y la caché de metadatos para reducirlo.
- Velocidad: depende del iPhone (≈ 30 MB/s medidos por USB), no del disco.
- Las bibliotecas existentes **no se reorganizan**: solo lo nuevo usa la estructura con álbumes.
- La lectura de álbumes usa el esquema interno de Fotos de Apple, que puede cambiar entre versiones de iOS; si no se
  reconoce, se continúa sin álbumes y se avisa.

## Privacidad

Nada sale de tu equipo: ni fotos, ni vídeos, ni metadatos, ni coordenadas. La geocodificación usa una base local
(GeoNames). Las miniaturas se guardan solo en `_Manifest/thumbs` de tu destino. De la base de Fotos del iPhone solo se
leen los álbumes y la marca de favorito, y la copia temporal se borra.

## Licencia

**GPL-3.0-or-later** (ver [LICENSE](LICENSE)). Es la licencia que exige la dependencia
[pymobiledevice3](https://github.com/doronz88/pymobiledevice3) (GPL-3.0). Avisos de otras bibliotecas y datos
(GeoNames CC-BY 4.0, ExifTool, Qt/PySide6, FFmpeg, libheif…) en [docs/THIRD_PARTY.md](docs/THIRD_PARTY.md).

Datos geográficos: © GeoNames, <https://www.geonames.org> (CC-BY 4.0).
