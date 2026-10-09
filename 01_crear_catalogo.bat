@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ======================================================
echo PASO 1 - CREAR / AMPLIAR CATALOGO BNA
echo ======================================================
echo Se abrira el modo interactivo. Podes elegir una o varias categorias a la vez.
echo Para varias categorias, escribi sus numeros separados por coma: 1,3,5.
echo Tambien podes elegir 0 para todas las categorias.
echo Ejemplos de busqueda: notebook, monitor, tablet, computadora, pc, impresora.
echo Las busquedas se acumulan por SKU en cache_bna\catalogo_bna.json.
echo Escribi SALIR para terminar.
echo.
py buscador_bna.py --actualizar-catalogo
pause
