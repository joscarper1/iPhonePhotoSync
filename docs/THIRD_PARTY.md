# Componentes y datos de terceros

| Componente | Uso | Licencia | Notas |
|---|---|---|---|
| pymobiledevice3 11.63.1 | Acceso AFC al iPhone | GPL-3.0-or-later | Si se distribuye el ejecutable, el conjunto debe ser GPL |
| ExifTool 13.64 (Phil Harvey) | Lectura de metadatos | Artistic License / GPL | `tools/exiftool`, origen y SHA-256 en `tools/exiftool/SOURCE.txt` |
| GeoNames (cities15000, countryInfo, admin1CodesASCII, alternateNamesV2 [es]) | Geocodificación inversa **offline** (`src/iphone_backup/data/geonames.sqlite`) | CC-BY 4.0 | **Atribución obligatoria:** datos de GeoNames, https://www.geonames.org |
| Pillow | Solo pruebas (JPEG sintético) | HPND | Dependencia de desarrollo |
| PySide6 | Interfaz (Etapa 7) | LGPL-3.0 | Aún no incorporado |

La base `src/iphone_backup/data/geonames.sqlite` se genera con `tools/build_geonames_db.py` a partir de los volcados descargados
(ejecutar con `python -I`, los datos descargados no son de confianza). Nada se envía a Internet en tiempo de ejecución.

## Miniaturas (galería de lo pendiente)
- **Pillow** (HPND): decodifica JPEG/PNG y genera las miniaturas. https://python-pillow.org
- **pillow-heif** (BSD-3-Clause): lectura de HEIC/HEIF. Incluye **libheif** (LGPL-3.0) y **libde265** (LGPL-3.0); se enlazan como bibliotecas dinámicas de la rueda oficial, sin modificarlas.
- **PyAV** (BSD-3-Clause) con **FFmpeg** (LGPL-2.1+ en la rueda oficial): decodifica el primer fotograma de los vídeos (H.264/HEVC). Solo se usa para decodificar; nunca se re-codifica ni se modifica ningún vídeo del usuario.
- Las miniaturas se guardan únicamente en el disco local (`_Manifest/thumbs` del destino); no se envían a ningún servicio externo.

## Distribución del proyecto
- **Licencia del proyecto: GPL-3.0-or-later.** Obligada por **pymobiledevice3** (GPL-3.0-or-later), https://github.com/doronz88/pymobiledevice3. Quien reciba los ejecutables puede obtener el código fuente exacto de cada versión en la etiqueta correspondiente de este repositorio.
- **PySide6 / Qt for Python** (LGPL-3.0): se enlaza dinámicamente (bibliotecas incluidas sin modificar en el paquete). https://www.qt.io/qt-for-python
- **PyInstaller** (GPL-2.0 con excepción para el programa empaquetado): solo se usa para compilar. https://pyinstaller.org
- **ExifTool** © Phil Harvey (Artistic License / GPL): se incluye sin modificar dentro del paquete; se descarga en la compilación con `tools/fetch_exiftool.py`. https://exiftool.org
