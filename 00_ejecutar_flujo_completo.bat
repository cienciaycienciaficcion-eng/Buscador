@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ======================================================
echo SCRAPER BNA - FLUJO AUTOMATICO
echo ======================================================
py ejecutar_flujo.py
if errorlevel 1 (
  echo.
  echo El flujo se detuvo por un error. Lee el mensaje anterior.
  pause
)
