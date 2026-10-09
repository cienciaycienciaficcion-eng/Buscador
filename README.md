# Scraper limpio de Tienda BNA

Proyecto independiente para empezar desde cero. No usa ni modifica el directorio `D:\compras` salvo que lo descomprimas allí deliberadamente. La carpeta de trabajo recomendada es `D:\compras_bna_limpio`.

## Qué hace y en qué orden

1. **Descubrir productos y construir el catálogo** (`01_crear_catalogo.bat`). Usa Playwright/Chromium para leer el catálogo renderizado por la web, recorre todas las páginas de cada búsqueda y acumula los SKU en `cache_bna/catalogo_bna.json`.
2. **Descargar fichas técnicas** (`02_descargar_fichas.bat`). Lee el catálogo y abre cada URL de producto para activar la pestaña “Ficha Técnica”. Guarda cada ficha válida como `cache_bna/fichas/SKU.json`. Las fichas válidas ya existentes se omiten para no descargarlas otra vez. La descripción se conserva por separado cuando el extractor la encuentra.
3. **Auditar la carga** (`03_auditar_fichas.bat`). Genera un resumen y una tabla CSV de cobertura. No corrige ni reescribe fichas.
4. **Inventariar etiquetas reales** (`04_construir_glosario.bat`). Genera un inventario de nombres de campos tal como aparecen en las fichas, con frecuencia y ejemplos. No asigna alias ni normaliza valores semánticamente; ese trabajo viene después de comprobar la calidad de origen.
5. **Buscar dentro de las fichas descargadas** (`05_buscar_fichas.bat`). Permite localizar productos por texto, nombre de campo o valor técnico, sin volver a consultar la web.

## Instalación

- Descomprimí el ZIP en `D:\compras_bna_limpio`.
- Ejecutá `instalar.bat` una sola vez. Requiere Python instalado y el comando `py` disponible.
- Requiere conexión a Internet para consultar Tienda BNA y descargar Chromium.

## Ejecución automática desde cero

Ejecutá `00_ejecutar_flujo_completo.bat`. Elegí la opción 1 para actualizar el catálogo, descargar fichas pendientes, auditar y construir el glosario en una sola ejecución. La selección de categorías/búsquedas del catálogo sigue siendo interactiva; al terminarla, los pasos restantes continúan solos. Elegí la opción 2 si ya existe un catálogo y querés seguir con las fichas; opción 3 si solo querés auditar y reconstruir el glosario.

También podés ejecutar cada paso por separado usando los archivos `.bat` numerados.

## Ejecución manual desde cero

1. Ejecutá `01_crear_catalogo.bat`.
2. En cada búsqueda podés seleccionar **una o varias categorías principales**. Para varias, escribí los números separados por coma (por ejemplo `1,3,5`); `0` significa todas las categorías. Si elegís una sola categoría que tenga subcategorías, podés elegir una subcategoría concreta o toda la categoría. Después agregá una búsqueda general, por ejemplo `notebook`, `monitor`, `tablet`, `computadora` o `pc`. El catálogo es acumulativo por SKU: repetir búsquedas no borra productos anteriores.
3. Cuando termines, escribí `SALIR`.
4. Ejecutá `02_descargar_fichas.bat`. Para un catálogo grande puede tardar; se usa concurrencia 2 para evitar sobrecargar el sitio. Podés interrumpir con Ctrl+C y volver a ejecutarlo: las fichas válidas guardadas se conservan y se omiten en la siguiente ejecución.
5. Ejecutá `03_auditar_fichas.bat` y revisá `salidas/resumen_auditoria.json` y `salidas/auditoria_fichas.csv`.
6. Ejecutá `04_construir_glosario.bat` para conocer las etiquetas observadas en el corpus descargado.

## Copiar las fichas que ya tenés

Copiá los JSON de las fichas anteriores dentro de:

`cache_bna\fichas\`

Conservá sus nombres basados en SKU (por ejemplo, `1531064.json`). El lector acepta el formato de caché `{ "sku": "...", "url": "...", "producto": "...", "ficha": { ... } }` y también fichas cuyo objeto principal contenga directamente `disponible`, `filas`, `datos` y `descripcion`.

**No hace falta copiar resultados enriquecidos, CSV ni glosarios anteriores** para iniciar esta etapa. Primero conviene tener el catálogo y las fichas de origen. Los scripts de auditoría y glosario son de solo lectura respecto de los JSON originales.

## Buscar dentro de las fichas ya descargadas

Ejecutá `05_buscar_fichas.bat` para abrir el modo interactivo. Esta búsqueda es local: **no consulta la web ni descarga productos**. Busca en los JSON guardados en `cache_bna/fichas`, incluyendo nombres, descripciones y campos/valores de ficha. Los resultados se muestran en pantalla y se exportan a `salidas/resultados_busqueda_fichas.csv`.

Ejemplos desde CMD o PowerShell:

```powershell
py buscar_fichas.py 16 GB
py buscar_fichas.py --categoria monitor --campo resolucion --valor 1920
py buscar_fichas.py --campo memoria --valor 16
py buscar_fichas.py "USB-C" --csv salidas/usb_c.csv
```

Las palabras de una consulta de texto se interpretan como AND: todas deben aparecer en el texto del producto/ficha. `--campo` y `--valor` permiten localizar coincidencias en atributos técnicos sin depender de que el nombre del campo sea exactamente igual. La búsqueda solo puede encontrar información presente en las fichas que se descargaron o que copiaste a la carpeta.

## Búsqueda puntual

También podés ejecutar una búsqueda puntual desde CMD/PowerShell:

```powershell
py buscador_bna.py "notebook" --completo --no-preguntar-paginas --salida salidas/notebooks.json
```

Ejemplo con criterios técnicos (las fichas se consultan para los candidatos relevantes):

```powershell
py buscador_bna.py "notebook +ram:16+ +ssd:512+" --completo --no-preguntar-paginas --ficha-tecnica --salida salidas/notebooks_16gb.json
```

## Carpetas

- `cache_bna/catalogo_bna.json`: catálogo acumulativo de productos/SKU.
- `cache_bna/fichas/`: una ficha por SKU; se preserva entre ejecuciones.
- `salidas/`: auditorías, glosario observado y reportes de descarga.
- `entradas/`: espacio reservado para listas/archivos de entrada del usuario.

## Importante sobre la calidad de los datos

Una ficha vacía no se convierte en una ficha completa por tener un glosario. Si la pestaña no carga, el sitio cambió de estructura o la tabla no está disponible, la descarga se reporta como fallida y se debe revisar la URL/HTML. El glosario solo aprende etiquetas presentes en las fichas efectivamente guardadas; no inventa datos que no se descargaron.
