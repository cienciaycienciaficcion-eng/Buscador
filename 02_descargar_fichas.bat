@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ======================================================
echo PASO 2 - DESCARGAR FICHAS TECNICAS DEL CATALOGO
echo ======================================================
py descargar_fichas_catalogo.py --paralelas 2
pause
