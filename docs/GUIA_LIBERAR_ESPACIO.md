# Cómo liberar espacio en el iPhone de forma segura

**La herramienta no borra nada del iPhone** (regla de solo lectura del proyecto). Te dice qué NO está a salvo; tú borras desde la app Fotos.

## 1. Genera el informe (con el iPhone conectado y desbloqueado)

```powershell
python -m iphone_backup.cli deletion-report --dest "D:\Respaldo"
```

Crea en `D:\Respaldo\iPhone_Photos\_Logs\`:
- `resumen_liberar_espacio_<fecha>.txt`: cifras, motivos y fecha de corte.
- `no_borrar_<fecha>.csv`: **la lista de excepciones** (se abre en Excel).

El informe relee las copias del disco (SHA-256) y comprueba que **a salvo + excepciones = total visto**. Si no cuadra, no lo uses.

## 2. Qué significa estar en la lista "no borrar"

| Motivo | Qué hacer |
|---|---|
| Editado | El original sin editar NO está respaldado (solo la versión editada). No lo borres, o respáldalo aparte. |
| Es nuevo o cambió después del respaldo | Vuelve a ejecutar `backup` y genera otro informe. |
| No se ha copiado / La copia falló / En cuarentena | Revisa el `usuario.log`, vuelve a ejecutar `backup`. |
| Su pareja Live Photo no está a salvo | Se trata como un par: no borres ninguna de las dos mitades. |
| La copia en el disco falta o no es íntegra | Vuelve a ejecutar `backup`; revisa el disco. |
| El archivo equivalente de tu biblioteca ya no está | Ya no cuenta como respaldo: vuelve a ejecutar `backup`. |

## 3. Regla de oro

El informe **solo cubre lo que existía en el iPhone cuando se generó**. Lo que tomes o recibas después no está cubierto: no lo borres. Después de borrar, ejecuta:

```powershell
python -m iphone_backup.cli verify --dest "D:\Respaldo" --device
```

## 4. Borrar en el iPhone

1. Las fotos de la lista "no borrar" se ubican en la app Fotos por **fecha y hora** (columna `fecha_captura`) y por el nombre `IMG_xxxx` (en la foto, desliza hacia arriba para ver "Info"). *Pendiente de verificar en tu iOS: si la búsqueda por nombre de archivo funciona.*
2. Las **editadas** se ven con el filtro "Editadas" de la biblioteca.
3. Un método práctico es marcar las excepciones como **Favoritas** o añadirlas a un álbum "No borrar" y luego seleccionar y borrar el resto de lo anterior a la fecha de corte.
4. Lo borrado va a **Eliminados recientemente** durante 30 días (sin iCloud Fotos). Vacíalo cuando estés seguro.

## 5. Lo que NO puede hacer la herramienta (y por qué)

- **No crea el álbum "Saved in PC" ni mueve fotos a él:** Apple no ofrece una vía documentada para que un PC modifique la biblioteca de Fotos por USB; hacerlo editando su base de datos por AFC es un riesgo de corrupción. Se puede crear ese álbum en el iPhone con la app Atajos (*Buscar fotos* + *Añadir al álbum*); no está verificado en iOS 27.
- **No borra:** ni por AFC (puede dejar Fotos inconsistente) ni de otra forma.

## 6. Antes de borrar

Ten una **segunda copia** del HDD. Con un único disco, una falla pierde también lo que borraste del iPhone.
