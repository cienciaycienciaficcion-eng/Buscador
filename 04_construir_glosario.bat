@echo off
chcp 65001 >nul
cd /d "%~dp0"
py construir_glosario_bna.py
pause
