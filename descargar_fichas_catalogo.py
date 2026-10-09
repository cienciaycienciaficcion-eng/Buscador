# -*- coding: utf-8 -*-
"""Descarga fichas técnicas de los SKU presentes en cache_bna/catalogo_bna.json.

Guarda solo la evidencia obtenida de la web, en cache_bna/fichas/SKU.json.
No limpia ni transforma las fichas existentes. Por defecto omite fichas válidas
ya presentes; --forzar vuelve a consultar todas las URLs del catálogo.
"""
import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from catalogo_bna import cargar_catalogo_bna
from buscador_bna import (
    CACHE_FICHAS_DIR, cargar_ficha_cache, descargar_fichas_paralelas,
    guardar_ficha_cache,
)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--forzar", action="store_true", help="Volver a descargar aunque haya una ficha válida en caché")
    ap.add_argument("--limite", type=int, default=0, help="Límite opcional de productos; 0 = todos")
    ap.add_argument("--paralelas", type=int, default=2, help="Páginas simultáneas; recomendado 2-4")
    args = ap.parse_args()

    productos = cargar_catalogo_bna()
    if not productos:
        print("No hay catálogo todavía. Primero ejecutá una búsqueda con buscador_bna.py y --completo.")
        raise SystemExit(2)
    candidatos = []
    vistos = set()
    sin_url = 0
    cache_ok = 0
    for p in productos:
        sku = str(p.get("sku") or "").strip()
        url = str(p.get("url") or "").strip()
        if not sku or not url:
            sin_url += 1
            continue
        if sku in vistos:
            continue
        vistos.add(sku)
        if not args.forzar and cargar_ficha_cache(sku):
            cache_ok += 1
            continue
        candidatos.append(p)
    if args.limite > 0:
        candidatos = candidatos[:args.limite]

    CACHE_FICHAS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Catálogo: {len(productos)} | con ficha válida ya guardada: {cache_ok} | sin URL/SKU: {sin_url}")
    print(f"A descargar: {len(candidatos)} | paralelas: {max(1, args.paralelas)}")
    if not candidatos:
        print("No hay fichas pendientes.")
        return

    resultados, segundos = descargar_fichas_paralelas(None, candidatos, max_paralelas=max(1, args.paralelas))
    ok = error = 0
    errores = []
    for producto, ficha in resultados:
        sku = str(producto.get("sku") or "").strip()
        if guardar_ficha_cache(sku, ficha, producto):
            ok += 1
        else:
            error += 1
            errores.append({"sku": sku, "producto": producto.get("producto"), "url": producto.get("url"), "error": ficha.get("error") or "Ficha sin filas válidas"})
    reporte = {
        "total_catalogo": len(productos), "candidatos": len(candidatos),
        "descargadas_y_guardadas": ok, "fallidas_o_vacias": error,
        "ya_en_cache": cache_ok, "sin_sku_o_url": sin_url,
        "segundos": round(segundos, 2), "errores": errores,
    }
    (BASE / "salidas").mkdir(exist_ok=True)
    (BASE / "salidas" / "reporte_descarga_fichas.json").write_text(json.dumps(reporte, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nRESUMEN DESCARGA")
    print(f"Guardadas: {ok} | fallidas/vacías: {error} | tiempo: {segundos:.1f} s")
    print(f"Fichas: {CACHE_FICHAS_DIR}")
    print("Reporte: salidas/reporte_descarga_fichas.json")

if __name__ == "__main__":
    main()
