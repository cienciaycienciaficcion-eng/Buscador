# -*- coding: utf-8 -*-
"""Audita fichas guardadas sin modificarlas."""
import json, csv
from pathlib import Path
BASE = Path(__file__).resolve().parent
CARPETA = BASE / "cache_bna" / "fichas"
OUT = BASE / "salidas"
OUT.mkdir(exist_ok=True)
filas = []
for archivo in sorted(CARPETA.glob("*.json")):
    try:
        obj = json.loads(archivo.read_text(encoding="utf-8"))
        ficha = obj.get("ficha", obj) if isinstance(obj, dict) else {}
        raw_rows = ficha.get("filas", []) if isinstance(ficha, dict) else []
        datos = ficha.get("datos", {}) if isinstance(ficha, dict) else {}
        descripcion = ficha.get("descripcion", "") if isinstance(ficha, dict) else ""
        filas.append({
            "archivo": archivo.name, "sku": str(obj.get("sku", archivo.stem)) if isinstance(obj, dict) else archivo.stem,
            "producto": obj.get("producto", "") if isinstance(obj, dict) else "",
            "disponible": bool(ficha.get("disponible")) if isinstance(ficha, dict) else False,
            "cantidad_filas": len(raw_rows) if isinstance(raw_rows, list) else 0,
            "cantidad_campos_datos": len(datos) if isinstance(datos, dict) else 0,
            "descripcion_caracteres": len(descripcion or ""),
            "error": ficha.get("error", "") if isinstance(ficha, dict) else "estructura_invalida",
            "json_valido": True,
        })
    except Exception as e:
        filas.append({"archivo": archivo.name, "sku": archivo.stem, "producto":"", "disponible":False,"cantidad_filas":0,"cantidad_campos_datos":0,"descripcion_caracteres":0,"error":str(e),"json_valido":False})
campos = ["archivo","sku","producto","disponible","cantidad_filas","cantidad_campos_datos","descripcion_caracteres","error","json_valido"]
with (OUT / "auditoria_fichas.csv").open("w", newline="", encoding="utf-8-sig") as f:
    w=csv.DictWriter(f, fieldnames=campos); w.writeheader(); w.writerows(filas)
resumen = {
 "carpeta": str(CARPETA), "archivos_json": len(filas),
 "json_ilegibles": sum(not x["json_valido"] for x in filas),
 "fichas_con_filas": sum(x["cantidad_filas"] > 0 for x in filas),
 "fichas_sin_filas": sum(x["cantidad_filas"] == 0 for x in filas),
 "fichas_disponibles": sum(x["disponible"] for x in filas),
 "fichas_con_descripcion": sum(x["descripcion_caracteres"] > 0 for x in filas),
}
(OUT / "resumen_auditoria.json").write_text(json.dumps(resumen,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps(resumen,ensure_ascii=False,indent=2))
print("Detalle: salidas/auditoria_fichas.csv")
