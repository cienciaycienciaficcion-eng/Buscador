# Tienda BNA: colector de catálogo para GitHub Actions

Esta variante ejecuta el scraper en un runner de GitHub, sin dejar una PC encendida. No necesita API keys ni secretos.

## Cómo instalarlo en tu repositorio

1. Descargá este paquete y descomprimilo.
2. Copiá el contenido de la carpeta `proyecto_bna_github` a la raíz de un repositorio de GitHub (o subí estos archivos conservando las rutas).
3. Verificá que `buscador_bna.py`, `catalogo_bna.py`, `descargar_fichas_catalogo.py`, `auditar_fichas.py`, `construir_glosario_bna.py`, `github_colector_bna.py` y `requirements.txt` queden en la raíz.
4. Conservá la carpeta `.github/workflows/colectar-bna.yml`.
5. En GitHub, abrí **Actions → Colectar catálogo y fichas Tienda BNA → Run workflow**.
6. Para la primera prueba, dejá `descargar_fichas = false`. Así se recorre el catálogo y se sube el JSON del catálogo como artefacto. Cuando quieras traer también las fichas en esa misma ejecución, activá la opción.

## Dónde descargar los datos

Al terminar, abrí la ejecución en **Actions**, bajá el artefacto `bna-catalogo-fichas-N` y descomprimilo. Incluye `cache_bna/catalogo_bna.json`, reportes y, si se eligió descargar fichas, `cache_bna/fichas/` más auditoría y glosario.

## Importante sobre ejecución larga

- Cada ejecución tiene un límite de 350 minutos configurado para dejar margen antes del límite usual de GitHub-hosted runners. Si el catálogo tarda más, el trabajo puede terminar por tiempo; el artefacto se intenta subir igualmente y puede contener un catálogo parcial.
- El colector recorre las categorías que la web logra exponer en el momento de la ejecución y todas las páginas detectadas por el scraper. No se puede garantizar que BNA no cambie su web o bloquee temporalmente solicitudes.
- El catálogo se acumula por SKU durante esa ejecución. Las fichas técnicas están desactivadas por defecto porque pueden multiplicar mucho el tiempo total. Para un primer fin de semana, es más prudente recolectar el catálogo primero y analizar el reporte antes de descargar miles de fichas.
- Los artefactos de GitHub se conservan 14 días según la configuración del workflow; descargalos antes de que venzan.
- El workflow no hace commits de datos al repositorio: entrega los datos como artefacto, para no llenar el historial de Git con archivos JSON grandes.
- No se modifica ningún proyecto local ni otro repositorio automáticamente.

## Qué revisar al bajar el artefacto

1. `salidas/reporte_colector_github.json`: cantidad de categorías intentadas, SKU únicos y errores.
2. `cache_bna/catalogo_bna.json`: catálogo acumulado.
3. Si se descargaron fichas: `salidas/reporte_descarga_fichas.json`, `salidas/resumen_auditoria.json`, `salidas/auditoria_fichas.csv` y los archivos de glosario.

La ejecución es no interactiva: no pregunta categorías ni requiere dejar abierta una terminal local.
