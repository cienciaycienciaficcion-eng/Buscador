# -*- coding: utf-8 -*-
"""
Orquestador del flujo BNA:
1) opcionalmente actualiza el catálogo en modo interactivo
2) descarga las fichas pendientes
3) audita las fichas guardadas
4) construye el inventario de etiquetas
No modifica ni elimina fichas originales.
"""
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
PYTHON = sys.executable

def ejecutar(titulo, args):
    print("\n" + "=" * 72)
    print(titulo)
    print("=" * 72, flush=True)
    resultado = subprocess.run([PYTHON, *args], cwd=BASE)
    if resultado.returncode != 0:
        print(f"\n[DETENIDO] El paso terminó con código {resultado.returncode}.")
        print("Corregí el problema y volvé a ejecutar este flujo; los datos guardados se conservan.")
        raise SystemExit(resultado.returncode)
    print(f"\n[OK] {titulo}", flush=True)

def preguntar(texto, defecto="s"):
    respuesta = input(texto).strip().lower()
    if not respuesta:
        respuesta = defecto
    return respuesta in ("s", "si", "sí", "y", "yes", "1")

def main():
    print("=" * 72)
    print("       SCRAPER BNA - FLUJO AUTOMÁTICO")
    print("=" * 72)
    print("El flujo no borra fichas existentes ni sobrescribe las válidas.")
    print("\nElegí cómo empezar:")
    print("  1. Flujo completo: actualizar catálogo y después procesar las fichas")
    print("  2. Continuar con el catálogo existente (descarga + auditoría + glosario)")
    print("  3. Solo auditar y reconstruir el glosario de las fichas actuales")
    opcion = input("\nOpción [1]: ").strip() or "1"

    if opcion == "1":
        ejecutar("PASO 1/4 - CATÁLOGO", ["buscador_bna.py", "--actualizar-catalogo"])
        print("\nCuando termine el catálogo, el proceso continuará automáticamente.", flush=True)
        ejecutar("PASO 2/4 - DESCARGA DE FICHAS", ["descargar_fichas_catalogo.py", "--paralelas", "2"])
        ejecutar("PASO 3/4 - AUDITORÍA", ["auditar_fichas.py"])
        ejecutar("PASO 4/4 - GLOSARIO OBSERVADO", ["construir_glosario_bna.py"])
    elif opcion == "2":
        ejecutar("PASO 1/3 - DESCARGA DE FICHAS", ["descargar_fichas_catalogo.py", "--paralelas", "2"])
        ejecutar("PASO 2/3 - AUDITORÍA", ["auditar_fichas.py"])
        ejecutar("PASO 3/3 - GLOSARIO OBSERVADO", ["construir_glosario_bna.py"])
    elif opcion == "3":
        ejecutar("AUDITORÍA", ["auditar_fichas.py"])
        ejecutar("GLOSARIO OBSERVADO", ["construir_glosario_bna.py"])
    else:
        print("Opción no válida.")
        raise SystemExit(2)

    print("\n" + "=" * 72)
    print("FLUJO TERMINADO")
    print("=" * 72)
    print("Revisá estos archivos:")
    print("  salidas/reporte_descarga_fichas.json")
    print("  salidas/resumen_auditoria.json")
    print("  salidas/auditoria_fichas.csv")
    print("  salidas/glosario_bna_observado.csv")
    print("  salidas/glosario_bna_observado.json")
    print("\nPara buscar productos, ejecutá 05_buscar_fichas.bat.")
    input("\nPresioná Enter para cerrar...")

if __name__ == "__main__":
    main()
