# -*- coding: utf-8 -*-
"""Colector no interactivo para GitHub Actions: catálogo general/categorías BNA."""
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from buscador_bna import ejecutar_scraper
from catalogo_bna import cargar_catalogo_bna


def guardar_diagnostico_web():
    """Guarda evidencia útil si el scraper no logra obtener productos."""
    destino = BASE / "salidas"
    destino.mkdir(exist_ok=True)
    info = {"url_solicitada": "https://www.tiendabna.com.ar/catalog", "ok": False}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(locale="es-AR", viewport={"width": 1440, "height": 1000})
            try:
                response = page.goto(info["url_solicitada"], wait_until="domcontentloaded", timeout=90000)
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass
                info.update({
                    "status_http": response.status if response else None,
                    "url_final": page.url,
                    "titulo": page.title(),
                    "cantidad_tarjetas": page.locator("article#modern-variant-card").count(),
                    "texto_inicio": page.locator("body").inner_text(timeout=10000)[:6000],
                })
                page.screenshot(path=str(destino / "diagnostico_catalogo.png"), full_page=True)
                (destino / "diagnostico_catalogo.html").write_text(page.content(), encoding="utf-8")
                info["ok"] = True
            finally:
                browser.close()
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    (destino / "diagnostico_catalogo.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main():
    (BASE / "salidas").mkdir(exist_ok=True)
    print("=" * 72)
    print("COLECTOR AUTOMÁTICO TIENDA BNA - GITHUB ACTIONS")
    print("Recorre el catálogo general página por página, sin cargarlo completo de una vez.")
    print("=" * 72)

    # La recolección se hace sobre el catálogo general de Tienda BNA.
    # No intentamos descubrir categorías: el catálogo general ya contiene
    # los productos de todas ellas y la tienda debe recorrerse página a página.
    categorias = {}
    (BASE / "salidas" / "categorias_descubiertas.json").write_text(
        json.dumps(categorias, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    objetivos = [(None, "Catálogo general")]

    errores = []
    productos_leidos_total = 0
    for i, (ruta, nombre) in enumerate(objetivos, 1):
        print("\n" + "#" * 72)
        print(f"[COLECTOR] Objetivo {i}/{len(objetivos)}: {nombre}")
        print("[COLECTOR] Catálogo general oficial: query=%5C; recorrido página por página.")
        try:
            productos = ejecutar_scraper(
                ["\\"],
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
            leidos = len(productos or [])
            productos_leidos_total += leidos
            print(f"[COLECTOR] Finalizada {nombre}: {leidos} registros leídos.")
        except Exception as exc:
            mensaje = f"{nombre}: {type(exc).__name__}: {exc}"
            print(f"[ERROR] {mensaje}")
            errores.append(mensaje)
        time.sleep(2)

    catalogo = cargar_catalogo_bna()
    reporte = {
        "categorias_detectadas": len(categorias),
        "categorias_intentadas": len(objetivos),
        "registros_leidos_en_ejecucion": productos_leidos_total,
        "errores_categoria": errores,
        "productos_unicos_en_catalogo": len(catalogo),
        "archivo_catalogo": "cache_bna/catalogo_bna.json",
        "estado": "ok" if len(catalogo) > 0 else "sin_productos",
        "nota": "No asumir cobertura total sin revisar este reporte y los registros de la ejecución.",
    }
    (BASE / "salidas" / "reporte_colector_github.json").write_text(
        json.dumps(reporte, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n" + "=" * 72)
    print(f"Registros leídos en esta ejecución: {productos_leidos_total}")
    print(f"SKU únicos acumulados: {len(catalogo)}")
    print(f"Categorías con errores: {len(errores)}")
    print("Reporte: salidas/reporte_colector_github.json")
    print("=" * 72)

    if not catalogo:
        print("[ERROR] No se recolectó ningún producto. Se genera diagnóstico y se detiene el flujo antes de descargar fichas.")
        guardar_diagnostico_web()
        raise SystemExit(2)
    if errores:
        print("[AVISO] Hubo errores en algunas categorías; se conserva el catálogo parcial.")


if __name__ == "__main__":
    main()
