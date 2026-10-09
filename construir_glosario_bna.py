# -*- coding: utf-8 -*-
"""Construye un inventario de nombres de campos observados en las fichas.
No aplica alias, no corrige valores y no altera los JSON originales.
"""
import json, re, csv, collections
from pathlib import Path
BASE = Path(__file__).resolve().parent
CARPETA = BASE / "cache_bna" / "fichas"
OUT = BASE / "salidas"
OUT.mkdir(exist_ok=True)
conteo = collections.Counter()
ejemplos = collections.defaultdict(list)
valores_por_campo = collections.defaultdict(collections.Counter)
productos = 0
for archivo in sorted(CARPETA.glob("*.json")):
    try:
        obj = json.loads(archivo.read_text(encoding="utf-8"))
    except Exception:
        continue
    ficha = obj.get("ficha", obj) if isinstance(obj, dict) else {}
    filas = ficha.get("filas", []) if isinstance(ficha, dict) else []
    if not isinstance(filas, list):
        continue
    productos += 1
    sku = str(obj.get("sku", archivo.stem)) if isinstance(obj, dict) else archivo.stem
    for fila in filas:
        if not isinstance(fila, dict): continue
        campo = str(fila.get("campo") or "").strip()
        valor = str(fila.get("valor") or "").strip()
        if not campo: continue
        clave = re.sub(r"\s+", " ", campo.casefold()).strip().rstrip(":")
        conteo[(campo, clave)] += 1
        if valor: valores_por_campo[(campo, clave)][valor] += 1
        if len(ejemplos[(campo, clave)]) < 5 and valor:
            ejemplos[(campo, clave)].append({"sku":sku,"valor":valor[:240]})
registros=[]
for (campo, clave), n in sorted(conteo.items(), key=lambda x:(-x[1],x[0][0].casefold())):
    registros.append({"campo_original":campo,"clave_textual":clave,"frecuencia_fichas":n,"valores_distintos":len(valores_por_campo[(campo,clave)]),"ejemplos":ejemplos[(campo,clave)]})
(OUT / "glosario_bna_observado.json").write_text(json.dumps({"version":1,"fichas_leidas":productos,"campos":registros},ensure_ascii=False,indent=2),encoding="utf-8")
with (OUT / "glosario_bna_observado.csv").open("w",newline="",encoding="utf-8-sig") as f:
    w=csv.DictWriter(f,fieldnames=["campo_original","clave_textual","frecuencia_fichas","valores_distintos"]); w.writeheader()
    for r in registros: w.writerow({k:r[k] for k in w.fieldnames})
print(f"Fichas inspeccionadas: {productos}")
print(f"Variantes de etiquetas observadas: {len(registros)}")
print("Salidas: salidas/glosario_bna_observado.json y .csv")
