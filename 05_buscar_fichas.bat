@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ======================================================
echo PASO 5 - BUSCAR ENTRE LAS FICHAS DESCARGADAS
echo ======================================================
echo Esta busqueda es local: no consulta Tienda BNA.
echo Busca texto, campos tecnicos y valores dentro de cache_bna\fichas.
echo.
echo Ejemplos desde CMD:
echo   py buscar_fichas.py 16 GB
echo   py buscar_fichas.py --categoria monitor --campo resolucion --valor 1920
echo   py buscar_fichas.py --campo memoria --valor 16
echo.
py buscar_fichas.py
pause
