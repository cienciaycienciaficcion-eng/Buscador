# -*- coding: utf-8 -*-
"""Persistencia mínima y tolerante del catálogo maestro Tienda BNA."""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
CATALOGO_CACHE = BASE / "cache_bna" / "catalogo_bna.json"


def _leer():
    if not CATALOGO_CACHE.exists():
        return []
    try:
        data = json.loads(CATALOGO_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in ("productos", "catalogo", "items", "results"):
            if isinstance(data.get(k), list):
                return [x for x in data[k] if isinstance(x, dict)]
        # Formato indexado por SKU.
        if all(isinstance(v, dict) for v in data.values()):
            return list(data.values())
    return []


def cargar_catalogo_bna():
    return _leer()


def guardar_catalogo_bna(productos):
    CATALOGO_CACHE.parent.mkdir(parents=True, exist_ok=True)
    CATALOGO_CACHE.write_text(
        json.dumps(productos, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def actualizar_catalogo_bna(productos, consulta=""):
    actuales = _leer()
    por_sku = {}
    sin_sku = []
    for p in actuales:
        sku = str(p.get("sku") or "").strip()
        if sku:
            por_sku[sku] = p
        else:
            sin_sku.append(p)
    for original in productos or []:
        if not isinstance(original, dict):
            continue
        p = dict(original)
        sku = str(p.get("sku") or "").strip()
        if not sku:
            continue
        previo = por_sku.get(sku, {})
        # Los campos vistos recientemente se actualizan; se retienen los que
        # falten en la nueva tarjeta, sin borrar URL/identidad conocida.
        combinado = dict(previo)
        combinado.update({k: v for k, v in p.items() if v not in (None, "", [], {})})
        consultas = combinado.get("consultas_catalogo", [])
        if not isinstance(consultas, list):
            consultas = [str(consultas)] if consultas else []
        if consulta and consulta not in consultas:
            consultas.append(consulta)
        combinado["consultas_catalogo"] = consultas
        por_sku[sku] = combinado
    resultado = sin_sku + list(por_sku.values())
    guardar_catalogo_bna(resultado)
    return resultado
