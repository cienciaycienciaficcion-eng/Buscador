# -*- coding: utf-8 -*-
"""Busca productos dentro de las fichas técnicas ya descargadas, sin consultar la web."""
import argparse
import csv
import json
import re
import unicodedata
from pathlib import Path

BASE = Path(__file__).resolve().parent
FICHAS_DIR = BASE / "cache_bna" / "fichas"
SALIDAS = BASE / "salidas"


def norm(value):
    s = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s.lower()).strip()


def load_record(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    ficha = data.get("ficha") if isinstance(data.get("ficha"), dict) else data
    filas = ficha.get("filas") if isinstance(ficha.get("filas"), list) else []
    datos = ficha.get("datos") if isinstance(ficha.get("datos"), dict) else {}
    descripcion = ficha.get("descripcion") or data.get("descripcion") or ""
    # Algunos formatos anteriores guardan el texto bruto en otras claves.
    bruto = " ".join(str(ficha.get(k) or data.get(k) or "") for k in (
        "texto_visible_bruto", "bloque_ficha_raw", "texto_ficha", "descripcion_completa"
    ))
    name = str(data.get("producto") or data.get("nombre") or data.get("title") or "")
    sku = str(data.get("sku") or data.get("id") or path.stem)
    url = str(data.get("url") or data.get("href") or "")
    fields = []
    for row in filas:
        if isinstance(row, dict):
            field = str(row.get("campo") or row.get("campo_original") or row.get("nombre") or "")
            value = str(row.get("valor") or row.get("value") or "")
            if field or value:
                fields.append((field, value))
    if not fields:
        for k, v in datos.items():
            if isinstance(v, list):
                for item in v:
                    fields.append((str(k), str(item)))
            else:
                fields.append((str(k), str(v)))
    text_parts = [name, sku, descripcion, bruto]
    text_parts.extend(f"{k}: {v}" for k, v in fields)
    return {"sku": sku, "producto": name, "url": url, "path": str(path),
            "fields": fields, "descripcion": str(descripcion), "texto": " ".join(text_parts)}


def search(query="", field="", value="", category="", limit=0):
    if not FICHAS_DIR.exists():
        return [], 0
    # Conserva frases entre comillas y separa el resto por espacios.
    terms = [norm(x) for pair in re.findall(r'"([^"]+)"|(\S+)', query) for x in pair if x]
    terms = [x for x in terms if x]
    field_n, value_n, category_n = norm(field), norm(value), norm(category)
    results, unreadable = [], 0
    for path in sorted(FICHAS_DIR.glob("*.json")):
        record = load_record(path)
        if not record:
            unreadable += 1
            continue
        full = norm(record["texto"])
        if terms and not all(t in full for t in terms):
            continue
        if category_n and category_n not in norm(record["producto"]):
            continue
        matching = []
        for f, v in record["fields"]:
            fn, vn = norm(f), norm(v)
            if field_n and field_n not in fn:
                continue
            if value_n and value_n not in vn:
                continue
            if not field_n and not value_n:
                # Si la consulta aparece en un campo, mostrar ese contexto.
                if terms and any(t in fn or t in vn for t in terms):
                    matching.append((f, v))
            else:
                matching.append((f, v))
        # Si la consulta solo aparece en descripción/nombre, también incluir el producto.
        if terms and not matching:
            matching = [("Descripción / nombre", record["descripcion"] or record["producto"])]
        if field_n or value_n:
            if not matching:
                continue
        record["coincidencias"] = matching[:30]
        results.append(record)
    if limit > 0:
        results = results[:limit]
    return results, unreadable


def export_csv(results, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["sku", "producto", "url", "campo_coincidente", "valor_coincidente", "archivo_ficha"])
        writer.writeheader()
        for r in results:
            matches = r["coincidencias"] or [("", "")]
            for field, value in matches:
                writer.writerow({"sku": r["sku"], "producto": r["producto"], "url": r["url"],
                                 "campo_coincidente": field, "valor_coincidente": value, "archivo_ficha": r["path"]})


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("consulta", nargs="*", help="Palabras a buscar; todas deben aparecer. Usá comillas para frases exactas.")
    ap.add_argument("--campo", default="", help="Filtrar por nombre de campo, por ejemplo 'memoria'.")
    ap.add_argument("--valor", default="", help="Filtrar por texto del valor, por ejemplo '16 GB'.")
    ap.add_argument("--categoria", default="", help="Filtrar por palabras del nombre de producto, por ejemplo 'monitor'.")
    ap.add_argument("--limite", type=int, default=0, help="Máximo de productos mostrados/exportados; 0=todos.")
    ap.add_argument("--csv", default=str(SALIDAS / "resultados_busqueda_fichas.csv"), help="Ruta del CSV de resultados.")
    args = ap.parse_args()
    query = " ".join(args.consulta).strip()
    if not query and not (args.campo or args.valor or args.categoria):
        print("BUSCAR EN FICHAS DESCARGADAS (no consulta la web)")
        print(f"Carpeta: {FICHAS_DIR}")
        print("Podés buscar por palabras, campo y/o valor. Ejemplos: 16 GB | --campo pantalla --valor IPS")
        try:
            query = input("Texto a buscar (Enter para buscar por campo/valor): ").strip()
            field = input("Campo (opcional): ").strip()
            value = input("Valor (opcional): ").strip()
            category = input("Tipo/nombre de producto (opcional): ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); return
    else:
        field, value, category = args.campo, args.valor, args.categoria
    results, unreadable = search(query, field, value, category, args.limite)
    print(f"Fichas leídas en {FICHAS_DIR}: {len(list(FICHAS_DIR.glob('*.json'))) if FICHAS_DIR.exists() else 0}")
    print(f"Resultados: {len(results)} | JSON ilegibles: {unreadable}")
    for i, r in enumerate(results, 1):
        print(f"\n{i}. {r['producto'] or '(sin nombre)'} | SKU {r['sku']}")
        if r['url']:
            print(f"   URL: {r['url']}")
        for f, v in r['coincidencias'][:8]:
            print(f"   - {f}: {v[:350]}")
        print(f"   Ficha: {r['path']}")
    out = Path(args.csv)
    if results:
        export_csv(results, out)
        print(f"\nCSV guardado: {out}")
    elif not results:
        print("No hubo coincidencias. Probá menos palabras o solo un campo/valor.")

if __name__ == "__main__":
    main()
