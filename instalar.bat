@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ======================================================
echo INSTALACION - SCRAPER LIMPIO TIENDA BNA
echo ======================================================
py -m pip install --upgrade pip
if errorlevel 1 goto error
py -m pip install -r requirements.txt
if errorlevel 1 goto error
py -m playwright install chromium
if errorlevel 1 goto error
echo.
echo Instalacion terminada.
echo Ejecuta 01_crear_catalogo.bat para comenzar.
pause
exit /b 0
:error
echo.
echo Hubo un error. Verifica que Python este instalado y disponible como comando py.
pause
exit /b 1
