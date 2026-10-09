# -*- coding: utf-8 -*-
"""Colector no interactivo para GitHub Actions: recorre el catálogo BNA completo."""
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from buscador_bna import descubrir_categorias_bna, ejecutar_scraper
from catalogo_bna import cargar_catalogo_bna


def main():
    (BASE / "salidas").mkdir(exist_ok=True)
    print("=" * 72)
    print("COLECTOR AUTOMÁTICO TIENDA BNA - GITHUB ACTIONS")
    print("Recorrido completo por categorías; no descarga fichas en esta etapa.")
    print("=" * 72)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(locale="es-AR", viewport={"width": 1440, "height": 1000})
        try:
            categorias = descubrir_categorias_bna(page)
        finally:
            page.close()
            browser.close()

    (BASE / "salidas" / "categorias_descubiertas.json").write_text(
        json.dumps(categorias, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not categorias:
        print("[AVISO] No se detectaron categorías. Se intentará el catálogo general.")
        objetivos = [(None, "Catálogo general")]
    else:
        objetivos = []
        for nombre, info in categorias.items():
            ruta = info.get("href") or info.get("slug")
            if ruta:
                objetivos.append((ruta, nombre))
        if not objetivos:
            objetivos = [(None, "Catálogo general")]

    errores = []
    for i, (ruta, nombre) in enumerate(objetivos, 1):
        print("\n" + "#" * 72)
        print(f"[COLECTOR] Categoría {i}/{len(objetivos)}: {nombre}")
        print("[COLECTOR] Todas las páginas; sin límite de productos.")
        try:
            productos = ejecutar_scraper(
                ["producto"],
                0,
                0,
                f"salidas/catalogo_categoria_{i:03d}.json",
                preguntar_paginas=False,
                obtener_fichas=False,
                max_fichas=0,
                categoria_slug=ruta,
                categoria_nombre=nombre,
                modo_completo=True,
            )
            print(f"[COLECTOR] Finalizada {nombre}: {len(productos or [])} registros leídos.")
        except Exception as exc:
            mensaje = f"{nombre}: {type(exc).__name__}: {exc}"
            print(f"[ERROR] {mensaje}")
            errores.append(mensaje)
        # Pequeña pausa entre categorías para no encadenar cargas agresivas.
        time.sleep(2)

    catalogo = cargar_catalogo_bna()
    reporte = {
        "categorias_detectadas": len(categorias),
        "categorias_intentadas": len(objetivos),
        "errores_categoria": errores,
        "productos_unicos_en_catalogo": len(catalogo),
        "archivo_catalogo": "cache_bna/catalogo_bna.json",
        "nota": "El catálogo es acumulativo por SKU. Revisar logs y auditoría antes de asumir cobertura total.",
    }
    (BASE / "salidas" / "reporte_colector_github.json").write_text(
        json.dumps(reporte, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n" + "=" * 72)
    print(f"SKU únicos acumulados: {len(catalogo)}")
    print(f"Categorías con errores: {len(errores)}")
    print("Reporte: salidas/reporte_colector_github.json")
    print("=" * 72)
    if errores:
        print("[AVISO] Hubo categorías con errores; se conserva el catálogo parcial para descargarlo.")


if __name__ == "__main__":
    main()
