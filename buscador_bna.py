#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Tienda BNA - buscador de ofertas v26 contextual

Basado en el scraper de catálogo:
- busca productos en Tienda BNA
- extrae precio, descuento, cuotas, envío, imagen y URL
- interpreta RAM / almacenamiento desde el nombre
- tolera formatos imperfectos del catálogo:
    16GB 512GB
    16GB 1TB
    32GB 2TB
    8GB 256SSD
    8GB 512GB SSD
    8GB 512GBB
    128+8GB
- filtra por especificaciones
- calcula métricas simples de valor
- guarda JSON

Requiere:
    pip install playwright
    playwright install chromium
"""

import argparse
import asyncio
import gc
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright


BASE = Path(__file__).resolve().parent
from catalogo_bna import CATALOGO_CACHE, cargar_catalogo_bna, guardar_catalogo_bna, actualizar_catalogo_bna
SCRAPER_BASE = BASE / "scraper_tienda_bna_catalogo_busqueda.py"

# Tienda BNA permite cambiar el orden del catálogo mediante el parámetro "o".
# Lo fijamos para que ejecuciones consecutivas sean comparables.
ORDEN_BNA = "available_on-desc"
NOMBRE_ORDEN_BNA = "Novedades"

# Las categorías se descubren dinámicamente desde filters.category.items.
# Solo se conserva un nivel: categoría principal -> hijos directos.
CATEGORIAS_CACHE = BASE / "categorias_bna.json"

# Caché persistente de Fichas Técnicas, indexado por SKU.
# Precio/stock/financiación NO se cachean: se consultan en cada ejecución.
CACHE_FICHAS_DIR = BASE / "cache_bna" / "fichas"
# Catálogo maestro persistente: un registro por SKU, alimentado por las búsquedas generales.
CATALOGO_CACHE = BASE / "cache_bna" / "catalogo_bna.json"


def _normalizar_categoria_item(item):
    if not isinstance(item, dict):
        return None
    nombre = str(item.get("name") or item.get("title") or "").strip()
    slug = str(item.get("slug") or "").strip()
    href = str(item.get("href") or "").strip()
    if not nombre or not slug:
        return None
    # Conservamos tanto slug como href porque BNA puede usar un slug
    # corto (ej. "computacion") pero una ruta de catálogo completa
    # (ej. "/ar/tecnologia-computacion").
    return {"nombre": nombre, "slug": slug, "href": href}


def _extraer_categorias_un_nivel(data):
    """Extrae categoría principal e hijos directos; no recorre niveles inferiores."""
    filtros = data.get("filters") if isinstance(data, dict) else None
    bloque = filtros.get("category") if isinstance(filtros, dict) else None
    items = bloque.get("items") if isinstance(bloque, dict) else None
    if not isinstance(items, list):
        return {}

    resultado = {}
    for item in items:
        raiz = _normalizar_categoria_item(item)
        if not raiz:
            continue
        childs = item.get("childs") or item.get("children") or []
        sub = {}
        if isinstance(childs, list):
            for child in childs:
                c = _normalizar_categoria_item(child)
                if c:
                    # Guardamos el objeto completo para no perder la ruta real.
                    sub[c["nombre"]] = {
                        "slug": c["slug"],
                        "href": c["href"],
                    }
        resultado[raiz["nombre"]] = {
            "slug": raiz["slug"],
            "href": raiz["href"],
            "subcategorias": sub,
        }

    # Fallback si BNA entrega el bloque aplanado sin ``childs``.
    if not resultado:
        for item in items:
            raiz = _normalizar_categoria_item(item)
            if raiz:
                resultado[raiz["nombre"]] = {
                    "slug": raiz["slug"],
                    "href": raiz["href"],
                    "subcategorias": {},
                }
    return resultado


def descubrir_categorias_bna(page):
    """Descubre las categorías reales que usa la SPA de Tienda BNA.

    Primero captura respuestas JSON de la propia página y busca el bloque:
        filters -> category -> items
    que contiene las categorías y sus hijos directos.

    Solo como respaldo se inspeccionan enlaces, pero se ignoran páginas legales
    y enlaces que no parezcan categorías de catálogo. No se recorre una
    jerarquía superior a un nivel.
    """
    from urllib.parse import urlparse

    respuestas_categorias = []

    def procesar_json(data):
        try:
            categorias = _extraer_categorias_un_nivel(data)
            if categorias:
                respuestas_categorias.append(categorias)
                return True
        except Exception:
            pass
        return False

    def normalizar_slug(href):
        try:
            path = urlparse(href).path.strip("/")
        except Exception:
            path = str(href).split("?")[0].strip("/")
        if path.startswith("ar/"):
            path = path[3:]
        return path

    try:
        url = "https://www.tiendabna.com.ar/catalog?query=producto&o=" + ORDEN_BNA
        print("[BNA] Descubriendo categorías desde la web...")

        def on_response(response):
            try:
                # La API/SPA de BNA no siempre declara el Content-Type de forma
                # consistente. Por eso no descartamos la respuesta solamente
                # porque el header no diga "application/json".
                url_resp = response.url or ""
                es_candidata = (
                    "api-bna.avenida.com" in url_resp
                    or "/api/" in url_resp
                    or "search" in url_resp
                    or "catalog" in url_resp
                )

                if not es_candidata:
                    return

                ct = (response.headers.get("content-type") or "").lower()

                try:
                    data = response.json()
                except Exception:
                    # Segundo intento: leer el cuerpo y decodificar JSON.
                    # Esto cubre respuestas con Content-Type incorrecto.
                    cuerpo = response.text()
                    if not cuerpo or not cuerpo.lstrip().startswith(("{", "[")):
                        return
                    data = json.loads(cuerpo)

                if isinstance(data, dict):
                    categorias = _extraer_categorias_un_nivel(data)
                    if categorias:
                        respuestas_categorias.append(categorias)
                        print(
                            f"[BNA] Respuesta de categorías encontrada: "
                            f"{len(categorias)} principales / "
                            f"{sum(len(v.get('subcategorias', {})) for v in categorias.values())} subcategorías"
                        )
            except Exception:
                pass

        page.on("response", on_response)
        page.goto(url, wait_until="domcontentloaded", timeout=90000)

        # La SPA puede realizar varias peticiones. Esperamos solo hasta que
        # aparezca el bloque de productos o hasta un máximo corto; no usamos
        # wait_for_timeout porque en este entorno ya vimos bloqueos del driver.
        try:
            page.wait_for_selector("article#modern-variant-card", timeout=20000)
        except Exception:
            pass

        # Si ya recibimos categorías, no necesitamos esperar nada más.
        if respuestas_categorias:
            # Preferimos la respuesta que tenga más categorías.
            categorias = max(respuestas_categorias, key=lambda x: (
                len(x),
                sum(len(v.get("subcategorias", {})) for v in x.values())
            ))
            CATEGORIAS_CACHE.write_text(
                json.dumps(categorias, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            total_sub = sum(len(x.get("subcategorias", {})) for x in categorias.values())
            print(
                f"[BNA] Categorías descubiertas: {len(categorias)} principales, "
                f"{total_sub} subcategorías."
            )
            return categorias

        # Respaldo DOM. No aceptamos cualquier enlace /ar/: excluimos rutas
        # conocidas como legales/informativas y exigimos que parezcan rutas
        # de catálogo por su posición/nombre.
        enlaces = page.locator("a[href]")
        encontrados = []
        excluidas = {
            "boton-de-arrepentimiento",
            "defensa-al-consumidor",
            "preguntas-frecuentes",
            "descuentos-y-beneficios",
            "terminos-y-condiciones",
            "politica-de-privacidad",
            "contacto",
            "ayuda",
            "como-comprar",
            "medios-de-pago",
            "preguntas-frecuentes",
            "beneficios",
            "bases-y-condiciones",
        }

        for i in range(enlaces.count()):
            a = enlaces.nth(i)
            href = a.get_attribute("href") or ""
            nombre = " ".join((a.inner_text() or "").split()).strip()
            slug = normalizar_slug(href)
            if not slug or not nombre:
                continue
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug, re.I):
                continue
            if slug in excluidas:
                continue
            if slug in ("products", "catalog", "login", "register"):
                continue
            encontrados.append((nombre, slug))

        por_slug = {}
        for nombre, slug in encontrados:
            por_slug.setdefault(slug, nombre)

        # El fallback solo usa raíces claramente presentes en los enlaces y
        # sus hijos. Esto evita inventar categorías desde páginas informativas.
        raices = {}
        for slug, nombre in por_slug.items():
            if "-" not in slug:
                raices.setdefault(
                    slug,
                    {"nombre": nombre, "slug": slug, "subcategorias": {}}
                )

        for slug, nombre in por_slug.items():
            if "-" not in slug:
                continue
            raiz_slug = slug.split("-", 1)[0]
            if raiz_slug not in raices:
                continue
            raices[raiz_slug]["subcategorias"].setdefault(nombre, slug)

        categorias = {
            v["nombre"]: {
                "slug": v["slug"],
                "subcategorias": v["subcategorias"],
            }
            for v in raices.values()
        }

        # El fallback DOM solo es aceptable si encontramos suficientes
        # subcategorías que permitan demostrar que realmente es el árbol
        # comercial. Una lista de páginas legales no se acepta.
        total_sub = sum(len(x.get("subcategorias", {})) for x in categorias.values())
        nombres_legales = {
            "botón de arrepentimiento",
            "defensa al consumidor",
            "preguntas frecuentes",
            "descuentos y beneficios",
        }
        categorias_validas = {
            k: v for k, v in categorias.items()
            if k.strip().lower() not in nombres_legales
        }

        if categorias_validas and total_sub >= 2:
            CATEGORIAS_CACHE.write_text(
                json.dumps(categorias_validas, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            total_sub = sum(
                len(x.get("subcategorias", {}))
                for x in categorias_validas.values()
            )
            print(
                f"[BNA] Categorías descubiertas por respaldo: "
                f"{len(categorias_validas)} principales, {total_sub} subcategorías."
            )
            return categorias_validas

        print("[BNA] El respaldo DOM no contiene un árbol de categorías válido.")
    except Exception as exc:
        print(f"[BNA] Error descubriendo categorías desde la web: {exc}")

    if CATEGORIAS_CACHE.exists():
        try:
            categorias = json.loads(CATEGORIAS_CACHE.read_text(encoding="utf-8"))
            # La caché v3 no conservaba siempre el href real de las
            # subcategorías. Si falta, preferimos redescubrirlas.
            tiene_href = any(
                isinstance(info, dict) and info.get("href")
                for info in categorias.values()
            )
            if not tiene_href:
                print("[BNA] Caché antigua sin rutas href; se redescubrirán categorías.")
            else:
                print(f"[BNA] Se usa caché de categorías: {len(categorias)} principales.")
                return categorias
        except Exception as exc:
            print(f"[BNA] Caché de categorías inválida: {exc}")

    print("[BNA] Sin categorías dinámicas; se usará el catálogo general.")
    return {}


def seleccionar_categoria_bna(categorias):
    """Menú interactivo: todas -> categoría -> subcategoría directa."""
    if not categorias:
        return None, None

    print("\n" + "=" * 60)
    print("              ¿DÓNDE BUSCAR?")
    print("=" * 60)
    print("  0) TODAS LAS CATEGORÍAS")
    lista = list(categorias.items())
    for i, (nombre, info) in enumerate(lista, 1):
        print(f"  {i}) {nombre}")

    while True:
        try:
            r = input("\nSeleccione [0 = todas]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None, None
        if r == "":
            return None, None
        if r.isdigit() and 0 <= int(r) <= len(lista):
            break
        print(f"Introduzca un número entre 0 y {len(lista)}.")

    if r == "0":
        return None, "Todas las categorías"

    nombre, info = lista[int(r) - 1]
    sub = info.get("subcategorias") or {}
    if not sub:
        print(f"[BNA] Categoría seleccionada: {nombre}")
        return info.get("href") or info.get("slug"), nombre

    print(f"\n{nombre.upper()}")
    print("  0) Toda la categoría")
    sublista = list(sub.items())
    for i, (subnombre, _) in enumerate(sublista, 1):
        print(f"  {i}) {subnombre}")

    while True:
        try:
            r2 = input("\nSeleccione: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return info.get("slug"), nombre
        if r2 == "":
            return info.get("slug"), nombre
        if r2.isdigit() and 0 <= int(r2) <= len(sublista):
            break
        print(f"Introduzca un número entre 0 y {len(sublista)}.")

    if r2 == "0":
        print(f"[BNA] Categoría seleccionada: {nombre}")
        return (
            info.get("href") or info.get("slug"),
            nombre,
        )

    subnombre, subinfo = sublista[int(r2) - 1]
    if isinstance(subinfo, dict):
        subruta = subinfo.get("href") or subinfo.get("slug")
    else:
        subruta = subinfo
    print(f"[BNA] Subcategoría seleccionada: {subnombre}")
    return subruta, subnombre


def parse_numero(texto):
    if texto is None:
        return None
    s = str(texto).replace(".", "").replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def parse_especificaciones(nombre):
    """Extrae RAM, almacenamiento y tipo, incluyendo SSD512GB/SSD 512GB."""
    original = nombre or ""
    s = re.sub(r"\s+", " ", original.upper().replace(",", ".")).strip()
    ram_gb = None
    storage_gb = None
    storage_tipo = None

    if re.search(r"NVME", s):
        storage_tipo = "nvme"
    elif re.search(r"SSD", s):
        storage_tipo = "ssd"
    elif re.search(r"HDD", s):
        storage_tipo = "hdd"
    elif re.search(r"E[- ]?MMC", s):
        storage_tipo = "emmc"

    m = re.search(r"\b(\d+)\s*\+\s*(\d+)\s*GB\b", s)
    if m and int(m.group(1)) >= 64 and int(m.group(2)) <= 64:
        storage_gb, ram_gb = int(m.group(1)), int(m.group(2))

    capacidades = []
    for m in re.finditer(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(GB|TB)", s, re.I):
        n = float(m.group(1).replace(",", "."))
        gb = n * 1024 if m.group(2).upper() == "TB" else n
        contexto = s[max(0,m.start()-20):min(len(s),m.end()+20)]
        tipo = None
        if "NVME" in contexto: tipo = "nvme"
        elif "SSD" in contexto: tipo = "ssd"
        elif "HDD" in contexto: tipo = "hdd"
        elif "EMMC" in contexto or "E-MMC" in contexto: tipo = "emmc"
        capacidades.append((gb, tipo))

    for m in re.finditer(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(SSD|NVME|HDD|EMMC)\b", s, re.I):
        capacidades.append((float(m.group(1).replace(",", ".")), m.group(2).lower().replace("-", "")))

    if storage_tipo:
        compatibles = [gb for gb,tipo in capacidades if tipo == storage_tipo or (storage_tipo == "ssd" and tipo == "nvme")]
        if compatibles: storage_gb = int(round(max(compatibles)))

    if ram_gb is None:
        # DDR3/DDR4/DDR5 es una señal fuerte de que la capacidad cercana
        # corresponde a RAM, incluso cuando "SSD" aparece inmediatamente
        # después (por ejemplo: "16GB DDR4 SSD 480GB").
        ram_ddr = []
        for m in re.finditer(
            r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(GB|TB)\s*DDR\s*[2345]\b",
            s,
            flags=re.I,
        ):
            gb = float(m.group(1).replace(",", "."))
            if m.group(2).upper() == "TB":
                gb *= 1024
            if 4 <= gb <= 128:
                ram_ddr.append(gb)
        if ram_ddr:
            ram_gb = int(round(max(ram_ddr)))

    if ram_gb is None:
        ram_candidatos = []
        for m in re.finditer(r"(?:RAM|MEMORIA)\D{0,12}(\d+(?:[.,]\d+)?)\s*(GB|TB)|(?<!\d)(\d+(?:[.,]\d+)?)\s*(GB|TB)\D{0,12}(?:RAM|MEMORIA)", s, re.I):
            n = next((x for x in m.groups()[::2] if x), None)
            unidad = next((x for x in m.groups()[1::2] if x), None)
            if n:
                gb = float(n.replace(",", ".")) * (1024 if unidad and unidad.upper()=="TB" else 1)
                if 4 <= gb <= 128: ram_candidatos.append(gb)
        if ram_candidatos: ram_gb = int(round(max(ram_candidatos)))

    if ram_gb is None:
        normales = [gb for gb,tipo in capacidades if 4 <= gb <= 128 and tipo is None]
        if normales: ram_gb = int(round(max(normales)))

    if storage_gb is None:
        normales = [gb for gb,tipo in capacidades if gb >= 128 and int(round(gb)) != ram_gb]
        if normales: storage_gb = int(round(max(normales)))

    return {"ram_gb": ram_gb, "storage_gb": storage_gb, "storage_tipo": storage_tipo, "texto_original": original}


def enriquecer_producto(p):
    nombre = p.get("producto", "")
    specs = parse_especificaciones(nombre)
    p["especificaciones"] = specs

    precio = p.get("precio")
    descuento = p.get("descuento") or 0

    if precio:
        if specs["storage_gb"]:
            p["precio_por_gb_storage"] = round(precio / specs["storage_gb"], 2)
        else:
            p["precio_por_gb_storage"] = None

        if specs["ram_gb"]:
            p["precio_por_gb_ram"] = round(precio / specs["ram_gb"], 2)
        else:
            p["precio_por_gb_ram"] = None

    # Puntaje deliberadamente simple y transparente.
    score = float(descuento)

    financiacion = p.get("financiacion") or []
    cuotas_sin_interes = [
        x.get("cuotas", 0)
        for x in financiacion
        if x.get("sin_interes")
    ]
    max_cuotas = max(cuotas_sin_interes, default=0)

    score += min(max_cuotas, 24) * 0.35

    if p.get("envio_gratis"):
        score += 5

    ram = specs["ram_gb"] or 0
    storage = specs["storage_gb"] or 0

    if ram >= 16:
        score += 5
    elif ram >= 8:
        score += 2

    if storage >= 1024:
        score += 5
    elif storage >= 512:
        score += 3
    elif storage >= 256:
        score += 1

    # Premio adicional cuando el tipo está explícito y es SSD/NVMe.
    if specs["storage_tipo"] in ("ssd", "nvme"):
        score += 2

    p["score_oferta"] = round(score, 2)
    p["cuotas_sin_interes_max"] = max_cuotas

    return p


def familia_producto(nombre):
    """
    Agrupa variantes de un mismo modelo eliminando capacidades.
    No intenta resolver modelos ambiguos al 100%; sirve para comparar
    configuraciones claramente relacionadas.
    """
    s = (nombre or "").lower()

    s = re.sub(r"\b\d+\s*(?:tb|gb)\b", " ", s, flags=re.I)
    s = re.sub(r"\b\d+\s*(?:ssd|nvme|hdd|emmc)\b", " ", s, flags=re.I)
    s = re.sub(r"\b\d+\s*\+\s*\d+\s*gb\b", " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" -_")

    return s


def comparar_variantes(productos):
    grupos = {}
    for p in productos:
        fam = familia_producto(p.get("producto", ""))
        if fam:
            grupos.setdefault(fam, []).append(p)

    for fam, items in grupos.items():
        if len(items) < 2:
            continue

        # La comparación se hace dentro de la misma familia.
        items.sort(key=lambda x: x.get("precio") or float("inf"))

        mejor = items[0]
        mejor_precio = mejor.get("precio") or 0

        for p in items:
            precio = p.get("precio") or 0
            ram = (p.get("especificaciones") or {}).get("ram_gb")
            storage = (p.get("especificaciones") or {}).get("storage_gb")

            p["familia_modelo"] = fam

            if mejor_precio and precio:
                p["prima_vs_variante_mas_barata_pct"] = round(
                    (precio / mejor_precio - 1) * 100, 2
                )

            # Índice orientativo de configuración.
            # No pretende sustituir una evaluación técnica.
            config = 0
            if ram:
                config += ram
            if storage:
                config += storage / 64

            p["indice_configuracion"] = round(config, 2)

        # Identificamos una variante "equilibrada":
        # buen score de oferta sin pagar desproporcionadamente más.
        candidatos = [
            p for p in items
            if p.get("precio") and p.get("score_oferta") is not None
        ]
        if candidatos:
            elegido = max(
                candidatos,
                key=lambda x: (
                    x.get("score_oferta", 0),
                    x.get("indice_configuracion", 0),
                    -(x.get("precio") or 0)
                )
            )
            for p in items:
                p["mejor_variante_familia"] = (
                    p.get("sku") == elegido.get("sku")
                )

    return productos



def parse_criterio_encadenado(texto):
    """Parsea el lenguaje general de búsqueda.

    Formato: base +atributo:valor +texto -texto +(A|B) -(A|B).
    ':' separa concepto y valor; '+'/'-' inicial incluyen/excluyen;
    '+' al final del valor expresa mínimo (>=).
    """
    tokens = re.findall(r'[^\s]+', texto.strip())
    base = []
    incluidos = []
    excluidos = []

    for token in tokens:
        if token.startswith('+') and len(token) > 1:
            incluidos.append(token[1:])
        elif token.startswith('-') and len(token) > 1:
            excluidos.append(token[1:])
        else:
            base.append(token)

    return {
        "base_query": " ".join(base).strip(),
        "incluidos": incluidos,
        "excluidos": excluidos,
    }


def parse_capacidad(valor):
    """Convierte 500, 500gb, 1tb, 1.5tb a GB."""
    s = str(valor).strip().lower().replace(',', '.')
    m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(tb|gb)?', s)
    if not m:
        return None
    numero = float(m.group(1))
    unidad = m.group(2) or 'gb'
    if unidad == 'tb':
        numero *= 1024
    return int(round(numero))


def parse_operador_numero(valor):
    """Devuelve (operador, numero). Sin operador => >=."""
    m = re.fullmatch(r'(<=|>=|=|<|>)?\s*(\d+(?:[.,]\d+)?)', valor.strip())
    if not m:
        return None, None
    operador = m.group(1) or '>='
    numero = float(m.group(2).replace(',', '.'))
    return operador, numero


def cumple_numero(actual, operador, objetivo):
    if actual is None or objetivo is None:
        return False
    if operador == '>=':
        return actual >= objetivo
    if operador == '<=':
        return actual <= objetivo
    if operador == '>':
        return actual > objetivo
    if operador == '<':
        return actual < objetivo
    return actual == objetivo


def criterio_capacidad_ok(actual, valor):
    operador, numero_raw = parse_operador_numero(valor)
    if numero_raw is None:
        # Para 1tb / 500gb no hay operador explícito.
        capacidad = parse_capacidad(valor)
        return actual is not None and capacidad is not None and actual >= capacidad

    # Si el usuario escribe 64 o 500, son GB.
    capacidad = int(round(numero_raw))
    return cumple_numero(actual, operador, capacidad)




def comparar_numero(actual, expresion):
    if actual is None:
        return False

    expresion = str(expresion).strip().replace(",", ".")
    m = re.fullmatch(
        r"(>=|<=|>|<|=)?\s*(-?\d+(?:\.\d+)?)",
        expresion
    )
    if not m:
        return False

    op = m.group(1) or "="
    objetivo = float(m.group(2))
    actual = float(actual)

    if op == ">=":
        return actual >= objetivo
    if op == "<=":
        return actual <= objetivo
    if op == ">":
        return actual > objetivo
    if op == "<":
        return actual < objetivo
    return actual == objetivo


def es_superior(valor, minimo):
    """True si valor es igual o superior al mínimo."""
    if valor is None:
        return False
    return float(valor) >= float(minimo)


def parse_capacidad_criterio(valor):
    """
    Convierte 32, 32gb, 1tb, 1.5tb, etc. a GB.
    """
    valor = valor.strip().lower().replace(",", ".")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(gb|tb)?\+?", valor)
    if not m:
        return None

    numero = float(m.group(1))
    unidad = m.group(2) or "gb"

    if unidad == "tb":
        numero *= 1024

    return numero


def criterio_capacidad(actual, expresion):
    """
    Soporta:
      32     -> exactamente 32
      32+    -> 32 o superior
      >=32   -> 32 o superior
      <=32
      >32
      <32
    """
    if actual is None:
        return False

    expresion = expresion.strip().lower()

    if expresion.endswith("+") and not expresion.startswith(("+", "-", ">")):
        minimo = parse_capacidad_criterio(expresion[:-1])
        return minimo is not None and es_superior(actual, minimo)

    if re.fullmatch(r"\d+(?:[.,]\d+)?\s*(?:gb|tb)?", expresion):
        objetivo = parse_capacidad_criterio(expresion)
        return objetivo is not None and float(actual) == objetivo

    return comparar_numero(actual, expresion)


def normalizar_cpu(texto):
    """
    Devuelve una familia/rango comparable para CPU Intel/AMD.
    Ejemplos:
      Ryzen 7 -> ('amd', 7)
      Ryzen 9 -> ('amd', 9)
      Core i7 -> ('intel', 7)
      Core i9 -> ('intel', 9)
    """
    texto = texto.lower()

    m = re.search(r'\bryzen\s*(?:threadripper\s*)?(?:ai\s*)?([3579])\b', texto)
    if m:
        return "amd", int(m.group(1))

    m = re.search(r'\b(?:intel\s*)?core\s*i([3579])\b', texto)
    if m:
        return "intel", int(m.group(1))

    # También soporta nombres como i7-12700H.
    m = re.search(r'\bi([3579])[-\s]?\d{3,5}[a-z]*\b', texto)
    if m:
        return "intel", int(m.group(1))

    return None, None


def comparar_cpu(texto_cpu, criterio):
    """
    Soporta:
      ryzen7   -> Ryzen 7
      ryzen7+  -> Ryzen 7 o superior
      corei7   -> Core i7
      corei7+  -> Core i7 o superior
    """
    texto_cpu = (texto_cpu or "").lower()
    criterio = criterio.lower().replace(" ", "")

    m = re.fullmatch(r"(ryzen|corei)([3579])(\+)?", criterio)
    if not m:
        return criterio in texto_cpu

    familia = m.group(1)
    nivel = int(m.group(2))
    superior = bool(m.group(3))

    if familia == "ryzen":
        fam_producto, nivel_producto = normalizar_cpu(texto_cpu)
        if fam_producto != "amd" or nivel_producto is None:
            return False
    else:
        fam_producto, nivel_producto = normalizar_cpu(texto_cpu)
        if fam_producto != "intel" or nivel_producto is None:
            return False

    if superior:
        return nivel_producto >= nivel

    return nivel_producto == nivel


def construir_texto_ficha(producto):
    ficha = producto.get("ficha_tecnica") or {}
    partes = []

    for fila in ficha.get("filas", []) or []:
        partes.append(
            f"{fila.get('campo', '')} {fila.get('valor', '')}"
        )

    return " ".join(partes).lower()


def obtener_fuentes_producto(producto):
    """Construye todas las fuentes textuales disponibles para evaluar filtros.

    Las exclusiones (-texto) deben ser definitivas y por eso también se
    consideran la URL y el SKU. Esto evita que un producto cuyo nombre no
    contiene el término, pero cuya URL sí lo contiene (por ejemplo
    /workstation-notebook-...), vuelva a entrar en el resultado.
    """
    nombre = str(producto.get("producto") or "").lower()
    descripcion = str(producto.get("descripcion") or "").lower()
    ficha_texto = construir_texto_ficha(producto)
    url = str(producto.get("url") or "").lower()
    sku = str(producto.get("sku") or "").lower()

    return {
        "nombre": nombre,
        "descripcion": descripcion,
        "ficha": ficha_texto,
        "url": url,
        "sku": sku,
        "todo": f"{nombre} {descripcion} {ficha_texto} {url} {sku}".strip(),
    }


def extraer_ram_de_ficha(producto):
    ficha = producto.get("ficha_tecnica") or {}

    for fila in ficha.get("filas", []) or []:
        campo = str(fila.get("campo", "")).lower()
        valor = str(fila.get("valor", ""))

        if any(x in campo for x in ("memoria", "ram", "memory")):
            nums = re.findall(
                r'(?<!\d)(\d+)\s*GB\b',
                valor,
                flags=re.I
            )
            if nums:
                return max(int(x) for x in nums)

    fuentes = [
        construir_texto_ficha(producto),
        str(producto.get("descripcion") or ""),
        str(producto.get("producto") or ""),
    ]
    for texto in fuentes:
        m = re.search(
            r'(?:memoria|ram)[^.;]{0,120}?'
            r'(?<!\d)(\d+)\s*gb\b',
            texto,
            flags=re.I
        )
        if m:
            return int(m.group(1))
    return None


def extraer_storage_de_ficha(producto):
    ficha = producto.get("ficha_tecnica") or {}

    for fila in ficha.get("filas", []) or []:
        campo = str(fila.get("campo", "")).lower()
        valor = str(fila.get("valor", "")).lower()

        if any(x in campo for x in (
            "disco", "almacenamiento", "storage", "unidad"
        )):
            tipo = None
            if "nvme" in valor:
                tipo = "nvme"
            elif "ssd" in valor:
                tipo = "ssd"
            elif "emmc" in valor:
                tipo = "emmc"
            elif "hdd" in valor or "disco rígido" in valor:
                tipo = "hdd"

            capacidades = []
            for n, unidad in re.findall(
                r'(?<!\d)(\d+(?:[.,]\d+)?)\s*(tb|gb)\b',
                valor,
                flags=re.I
            ):
                numero = float(n.replace(",", "."))
                if unidad.lower() == "tb":
                    numero *= 1024
                capacidades.append(numero)

            if capacidades:
                return max(capacidades), tipo

    return None, None


def extraer_procesador_de_ficha(producto):
    ficha = producto.get("ficha_tecnica") or {}

    for fila in ficha.get("filas", []) or []:
        campo = str(fila.get("campo", "")).lower()
        if any(x in campo for x in (
            "procesador", "cpu", "microprocesador"
        )):
            valor = str(fila.get("valor", ""))
            if valor.strip():
                return valor

    # Si la ficha no trae CPU, usamos la descripción antes del nombre comercial.
    descripcion = str(producto.get("descripcion") or "")
    if descripcion.strip():
        return descripcion
    return str(producto.get("producto") or "")


def normalizar_termino(texto):
    """Normaliza un término para búsquedas generales de atributos."""
    s = str(texto or "").lower().strip()
    s = s.replace("™", "")
    # Tolerancia a errores/variantes frecuentes de escritura.
    s = re.sub(r"\brizen\b", "ryzen", s)
    s = re.sub(r"\bryz[ée]n\b", "ryzen", s)
    s = re.sub(r"\bcore\s*i\s*([3579])\b", r"corei\1", s)
    s = re.sub(r"\bryzen\s*([3579])\b", r"ryzen\1", s)
    s = re.sub(r"[^a-z0-9áéíóúüñ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def buscar_atributo_en_ficha(producto, atributo):
    """Devuelve los valores de filas de Ficha Técnica relacionadas con atributo."""
    atributo_n = normalizar_termino(atributo)
    valores = []
    ficha = producto.get("ficha_tecnica") or {}
    for fila in ficha.get("filas", []) or []:
        campo = normalizar_termino(fila.get("campo", ""))
        if not campo:
            continue
        # Coincidencia por palabras: permite "memoria ram" para +ram:32.
        palabras = atributo_n.split()
        if atributo_n in campo or all(w in campo for w in palabras):
            valores.append(str(fila.get("valor", "")))
    return valores


def evaluar_atributo_generico(producto, atributo, expresion):
    """Motor general atributo:valor. Primero ficha, luego nombre."""
    atributo_n = normalizar_termino(atributo)
    expresion = str(expresion or "").strip()
    es_minimo_cpu = expresion.rstrip().endswith("+")
    expr_n = normalizar_termino(expresion.rstrip("+")).strip()

    # CPU: permite +ryzen:7+, +core:7+, etc.
    # El signo + es semántico y debe conservarse: Ryzen 7+ significa
    # Ryzen 7, 9, etc.; no significa Ryzen 7 exacto.
    if atributo_n in ("ryzen", "core", "procesador", "cpu"):
        cpu = extraer_procesador_de_ficha(producto)
        if not cpu:
            cpu = str(producto.get("producto") or "")
        if atributo_n == "ryzen":
            criterio_cpu = "ryzen" + expr_n + ("+" if es_minimo_cpu else "")
        elif atributo_n == "core":
            criterio_cpu = "corei" + expr_n + ("+" if es_minimo_cpu else "")
        else:
            criterio_cpu = expr_n + ("+" if es_minimo_cpu else "")
        return comparar_cpu(cpu, criterio_cpu), f"CPU={cpu}"

    # Valores numéricos: RAM, almacenamiento y cualquier campo que contenga GB/TB.
    valores_ficha = buscar_atributo_en_ficha(producto, atributo_n)
    texto_fuentes = " ".join(
        valores_ficha
        + [str(producto.get("descripcion") or "")]
    )

    # Si el valor buscado es capacidad, buscar capacidades dentro del atributo.
    if re.search(r"(?:tb|gb)\+?$|^[<>]=?\s*\d", expresion.lower()) or re.fullmatch(r"(?:\d+(?:[.,]\d+)?)(?:gb|tb)?\+?", expresion.lower()):
        caps = []
        for m in re.finditer(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(gb|tb)\b", texto_fuentes, re.I):
            v=float(m.group(1).replace(",", "."))
            if m.group(2).lower()=="tb": v*=1024
            caps.append(v)
        if not caps:
            for texto in (
                str(producto.get("descripcion") or ""),
                str(producto.get("producto") or ""),
            ):
                for m in re.finditer(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(gb|tb)\b", texto, re.I):
                    v=float(m.group(1).replace(",", "."))
                    if m.group(2).lower()=="tb": v*=1024
                    caps.append(v)
        if caps:
            return any(criterio_capacidad(v, expresion) for v in caps), f"{atributo}={caps}"

    # Texto: el valor puede aparecer en la fila de ficha o, si no hay ficha,
    # en nombre/descrición/especificaciones.
    fuentes = valores_ficha[:]
    fuentes.append(str(producto.get("descripcion") or ""))
    fuentes.append(str(producto.get("producto") or ""))
    fuentes.append(str((producto.get("especificaciones") or {}).get("texto_original") or ""))
    objetivo = normalizar_termino(expr_n)
    if not objetivo:
        return False, f"{atributo}=sin valor"
    ok = any(objetivo in normalizar_termino(v) for v in fuentes)
    return ok, f"{atributo}={'OK' if ok else 'NO'}"


def _evaluar_subcriterio_simple_original(producto, criterio):
    """
    Evalúa un único criterio atómico.
    Los grupos OR se resuelven fuera de esta función.
    """
    criterio = criterio.strip().lower()

    # Sintaxis general atributo:valor. Los casos comerciales/CPU específicos
    # siguen existiendo por compatibilidad, pero cualquier atributo nuevo
    # puede caer en este motor genérico.
    if ":" in criterio:
        atributo, expresion = criterio.split(":", 1)
        atributo = atributo.strip()
        if atributo and atributo not in (
            "precio", "descuento", "cuotas", "envio", "envío",
            "ram", "storage", "ssd", "nvme", "hdd", "emmc",
            "procesador", "cpu", "gpu", "placa", "video"
        ):
            return evaluar_atributo_generico(producto, atributo, expresion)
    fuentes = obtener_fuentes_producto(producto)
    ficha = fuentes["ficha"]

    ram = extraer_ram_de_ficha(producto)
    storage, storage_tipo = extraer_storage_de_ficha(producto)

    specs = producto.get("especificaciones") or {}
    if ram is None:
        ram = specs.get("ram_gb")
    if storage is None:
        storage = specs.get("storage_gb")
    if storage_tipo is None:
        storage_tipo = specs.get("storage_tipo")

    # Fallback final al nombre del producto. Esto es importante cuando la
    # Ficha Técnica existe pero no informa CPU/RAM/almacenamiento completos.
    nombre = fuentes["nombre"]
    if ram is None:
        ram_caps = []
        for m in re.finditer(
            r"(?:ram|memoria)\D{0,12}(\d+(?:[.,]\d+)?)\s*(gb|tb)\b",
            nombre, flags=re.I
        ):
            valor = float(m.group(1).replace(",", "."))
            if m.group(2).lower() == "tb":
                valor *= 1024
            ram_caps.append(valor)
        if ram_caps:
            ram = max(ram_caps)

    if storage is None or storage_tipo is None:
        patron_storage = r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(gb|tb)\b"
        candidatos_storage = []
        for m in re.finditer(patron_storage, nombre, flags=re.I):
            valor = float(m.group(1).replace(",", "."))
            if m.group(2).lower() == "tb":
                valor *= 1024
            contexto = nombre[max(0, m.start()-18):m.end()+18]
            tipo = None
            if "nvme" in contexto:
                tipo = "nvme"
            elif "ssd" in contexto:
                tipo = "ssd"
            elif "emmc" in contexto:
                tipo = "emmc"
            elif "hdd" in contexto:
                tipo = "hdd"
            if tipo or valor >= 128:
                candidatos_storage.append((valor, tipo))
        if candidatos_storage:
            if storage is None:
                storage = max(x[0] for x in candidatos_storage)
            if storage_tipo is None:
                tipos = [x[1] for x in candidatos_storage if x[1]]
                if tipos:
                    storage_tipo = tipos[0]

    # RAM:32+
    if criterio.startswith("ram:"):
        expresion = criterio.split(":", 1)[1]
        return criterio_capacidad(ram, expresion), f"RAM ficha={ram}GB"

    # STORAGE:1TB+
    if criterio.startswith("storage:"):
        expresion = criterio.split(":", 1)[1]
        objetivo = parse_capacidad_criterio(expresion)
        if objetivo is None:
            return False, f"storage ficha={storage}GB"
        if expresion.endswith("+"):
            ok = storage is not None and storage >= objetivo
        else:
            ok = storage is not None and storage == objetivo
        return ok, f"storage ficha={storage}GB"

    # SSD:512+, SSD:, NVME:, etc.
    for tipo in ("ssd", "nvme", "hdd", "emmc"):
        if criterio.startswith(tipo + ":"):
            expresion = criterio.split(":", 1)[1].strip()
            tipo_ok = (
                storage_tipo == tipo
                or (tipo == "ssd" and storage_tipo == "nvme")
                or tipo in ficha
                or tipo in nombre
                or (tipo == "ssd" and "nvme" in nombre)
            )

            # Sin capacidad: +ssd: significa simplemente que tiene ese tipo
            # de almacenamiento.
            if not expresion:
                return tipo_ok, (
                    f"storage={storage}GB tipo={storage_tipo}"
                )

            objetivo = parse_capacidad_criterio(expresion)
            capacidad_ok = (
                objetivo is not None
                and storage is not None
                and (
                    storage >= objetivo
                    if expresion.endswith("+")
                    else storage == objetivo
                )
            )
            return tipo_ok and capacidad_ok, (
                f"storage={storage}GB tipo={storage_tipo}"
            )

    # SSD sin capacidad.
    if criterio in ("ssd", "nvme", "hdd", "emmc"):
        if criterio == "ssd":
            ok = storage_tipo in ("ssd", "nvme") or "ssd" in ficha
        else:
            ok = storage_tipo == criterio or criterio in ficha
        return ok, f"storage tipo={storage_tipo}"

    # CPU: ryzen7+, corei7+, etc.
    if re.fullmatch(r"(?:ryzen|corei)[3579]\+?", criterio):
        cpu = extraer_procesador_de_ficha(producto)
        return comparar_cpu(cpu, criterio), f"CPU={cpu}"

    # CPU con prefijo explícito.
    if criterio.startswith("procesador:") or criterio.startswith("cpu:"):
        cpu_criterio = criterio.split(":", 1)[1]
        cpu = extraer_procesador_de_ficha(producto)
        return comparar_cpu(cpu, cpu_criterio), f"CPU={cpu}"

    # GPU textual en ficha.
    if criterio.startswith(("gpu:", "placa:", "video:")):
        valor = criterio.split(":", 1)[1]
        campos = []

        for fila in (producto.get("ficha_tecnica") or {}).get("filas", []) or []:
            campo = str(fila.get("campo", "")).lower()
            if any(x in campo for x in (
                "video", "gpu", "gráfica", "grafica", "placa"
            )):
                campos.append(str(fila.get("valor", "")))

        campos.append(str(producto.get("descripcion") or ""))
        campos.append(str(producto.get("producto") or ""))
        return valor in " ".join(campos).lower(), "GPU ficha/descripcion/nombre"

    # Criterios comerciales.
    if criterio.startswith("precio:"):
        return comparar_numero(
            producto.get("precio"),
            criterio.split(":", 1)[1]
        ), f"precio={producto.get('precio')}"

    if criterio.startswith("descuento:"):
        return comparar_numero(
            producto.get("descuento"),
            criterio.split(":", 1)[1]
        ), f"descuento={producto.get('descuento')}"

    if criterio.startswith("cuotas:"):
        return comparar_numero(
            producto.get("cuotas_sin_interes_max") or 0,
            criterio.split(":", 1)[1]
        ), "cuotas"

    # Texto genérico: ficha primero, nombre después.
    return (
        criterio in fuentes["todo"],
        "ficha/descripcion/nombre"
    )


def separar_grupo_or(criterio):
    """
    Reconoce:
      (a|b|c)
      a|b|c
    y devuelve alternativas.
    """
    criterio = criterio.strip()

    if criterio.startswith("(") and criterio.endswith(")"):
        interior = criterio[1:-1]
    else:
        interior = criterio

    if "|" not in interior:
        return None

    partes = [
        x.strip()
        for x in interior.split("|")
        if x.strip()
    ]

    return partes if len(partes) >= 2 else None


def evaluar_subcriterio(producto, criterio):
    """
    Evalúa un criterio, incluyendo grupos OR.
    """
    alternativas = separar_grupo_or(criterio)

    if alternativas:
        resultados = []
        for alternativa in alternativas:
            ok, detalle = evaluar_subcriterio_simple(
                producto,
                alternativa
            )
            resultados.append((alternativa, ok, detalle))

        ok = any(x[1] for x in resultados)

        detalle = " OR ".join(
            f"{x[0]}={'OK' if x[1] else 'NO'}"
            for x in resultados
        )

        return ok, detalle

    return evaluar_subcriterio_simple(producto, criterio)


def evaluar_subcriterio_por_nombre(producto, criterio):
    """
    Pre-filtro barato basado EXCLUSIVAMENTE en el nombre del producto.

    Se usa antes de abrir la Ficha Técnica. No pretende decidir si el
    producto cumple definitivamente: solamente determina si vale la pena
    gastar una consulta de Ficha Técnica.
    """
    alternativas = separar_grupo_or(criterio)
    if alternativas:
        return any(
            evaluar_subcriterio_por_nombre(producto, alternativa)
            for alternativa in alternativas
        )

    criterio = criterio.strip().lower()
    nombre = str(producto.get("producto") or "").lower()

    if not nombre:
        return False

    # Criterios comerciales no dependen del nombre. No bloqueamos el
    # candidato aquí; se resolverán posteriormente con los datos del card.
    if criterio.startswith((
        "precio:", "descuento:", "cuotas:", "envio:", "envío:"
    )):
        return True

    # CPU: Ryzen 7+, Core i7+, etc.
    if re.fullmatch(r"(?:ryzen|corei)[3579]\+?", criterio):
        return comparar_cpu(nombre, criterio)

    if criterio.startswith("procesador:") or criterio.startswith("cpu:"):
        return comparar_cpu(
            nombre,
            criterio.split(":", 1)[1]
        )

    # RAM:32+, RAM:64, etc. Buscamos capacidades expresadas en el nombre.
    if criterio.startswith("ram:"):
        expresion = criterio.split(":", 1)[1]
        capacidades = []
        for n, unidad in re.findall(
            r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(gb|tb)\b",
            nombre,
            flags=re.I,
        ):
            valor = float(n.replace(",", "."))
            if unidad.lower() == "tb":
                valor *= 1024
            capacidades.append(valor)

        # En nombres de productos, 32GB/64GB suele identificar RAM, pero
        # también puede aparecer almacenamiento. Priorizamos expresiones
        # cercanas a RAM/memoria y, si no existen, usamos las capacidades
        # disponibles como filtro de candidatos.
        ram_caps = []
        for m in re.finditer(
            r"(?:ram|memoria)\D{0,12}(\d+(?:[.,]\d+)?)\s*(gb|tb)",
            nombre,
            flags=re.I,
        ):
            valor = float(m.group(1).replace(",", "."))
            if m.group(2).lower() == "tb":
                valor *= 1024
            ram_caps.append(valor)

        valores = ram_caps or capacidades
        if not valores:
            return False
        return any(criterio_capacidad(v, expresion) for v in valores)

    # Storage / SSD / NVMe / HDD: buscamos capacidades del nombre.
    if criterio.startswith(("storage:", "ssd:", "nvme:", "hdd:", "emmc:")):
        tipo, expresion = criterio.split(":", 1)
        expresion = expresion.strip()

        # +ssd: / +nvme: / +hdd: significa presencia del tipo, sin exigir
        # una capacidad concreta. También detectamos formatos como SSD512GB
        # donde no existe un límite de palabra entre "SSD" y "512".
        if not expresion:
            if tipo == "storage":
                return any(x in nombre for x in ("ssd", "nvme", "hdd", "emmc"))
            if tipo == "ssd":
                return "ssd" in nombre or "nvme" in nombre
            return tipo in nombre

        capacidades = []
        patron = r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(gb|tb)\b"
        for m in re.finditer(patron, nombre, flags=re.I):
            valor = float(m.group(1).replace(",", "."))
            if m.group(2).lower() == "tb":
                valor *= 1024

            # Para SSD/NVMe/HDD intentamos asociar la capacidad al tipo.
            inicio = max(0, m.start() - 18)
            contexto = nombre[inicio:m.end() + 18]
            tipo_ok = (
                tipo == "storage"
                or tipo in contexto
                or (tipo == "ssd" and "nvme" in contexto)
            )
            if tipo_ok:
                capacidades.append(valor)

        if not capacidades:
            return False

        if not expresion:
            return True
        return any(criterio_capacidad(v, expresion) for v in capacidades)

    # Atributos desconocidos: si aparecen en el nombre, sirven como
    # candidatos para después resolverlos contra la Ficha Técnica.
    if ":" in criterio:
        atributo, expresion = criterio.split(":", 1)
        if atributo.strip() and atributo.strip() not in (
            "precio", "descuento", "cuotas", "envio", "envío",
            "ram", "storage", "ssd", "nvme", "hdd", "emmc",
            "procesador", "cpu"
        ):
            objetivo = normalizar_termino(expresion.rstrip("+<>= "))
            nombre_n = normalizar_termino(producto.get("producto") or "")
            atributo_n = normalizar_termino(atributo)
            return atributo_n in nombre_n and (not objetivo or objetivo in nombre_n)

    # Criterios técnicos textuales (GPU, marca, familia, etc.).
    # Normalizamos espacios y puntuación básica para que "RTX-4060" y
    # "RTX 4060" puedan funcionar como candidatos.
    nombre_normalizado = re.sub(r"[^a-z0-9áéíóúüñ]+", " ", nombre)
    criterio_normalizado = re.sub(r"[^a-z0-9áéíóúüñ]+", " ", criterio)
    return criterio_normalizado.strip() in nombre_normalizado.strip()


def seleccionar_candidatos_por_nombre(productos, incluidos, excluidos):
    """
    Selecciona candidatos para Ficha Técnica / página de detalle.

    Las exclusiones se pueden descartar inicialmente con una comprobación
    barata sobre el nombre, pero la exclusión definitiva se repite después
    sobre URL, descripción y Ficha Técnica.

    Cualquier criterio técnico (+ram, +storage, +ryzen, +core, +cpu, GPU,
    etc.) obliga a enviar TODOS los productos no excluidos a la etapa de
    Ficha Técnica. El nombre del card nunca decide que un candidato técnico
    desaparezca antes de consultar su ficha.
    """
    candidatos = []
    descartados_exclusion = 0

    # Estos criterios dependen de la Ficha Técnica o de la descripción
    # completa. Si aparece cualquiera de ellos, NO podemos descartar un
    # producto por lo que diga solamente el nombre del card.
    detalle_prefijos = (
        "ram:", "storage:", "ssd:", "nvme:", "hdd:", "emmc:",
        "gpu:", "placa:", "video:",
        "procesador:", "cpu:", "ryzen:", "core:"
    )
    comerciales = (
        "precio:", "descuento:", "cuotas:", "envio:", "envío:"
    )

    for producto in productos:
        if any(
            evaluar_subcriterio_por_nombre(producto, criterio)
            for criterio in excluidos
        ):
            descartados_exclusion += 1
            continue

        if not incluidos:
            candidatos.append(producto)
            continue

        requiere_detalle = False
        for criterio in incluidos:
            alternativas = separar_grupo_or(criterio) or [criterio]
            for alternativa in alternativas:
                c = alternativa.strip().lower()
                if c.startswith(detalle_prefijos):
                    requiere_detalle = True
                    break
                if ":" in c and not c.startswith(comerciales):
                    requiere_detalle = True
                    break
            if requiere_detalle:
                break

        if requiere_detalle:
            # Criterio técnico: la ficha es la fuente de verdad.
            candidatos.append(producto)
        elif any(
            evaluar_subcriterio_por_nombre(producto, criterio)
            for criterio in incluidos
        ):
            candidatos.append(producto)

    return candidatos, descartados_exclusion


def puntuar_producto(producto, incluidos):
    cumplidos = []
    no_cumplidos = []
    detalles = {}

    for criterio in incluidos:
        ok, detalle = evaluar_subcriterio(producto, criterio)
        detalles[criterio] = detalle

        if ok:
            cumplidos.append(criterio)
        else:
            no_cumplidos.append(criterio)

    total = len(incluidos)
    coincidencias = len(cumplidos)

    porcentaje = (
        round(coincidencias / total * 100, 2)
        if total else 0
    )

    return {
        "coincidencias": coincidencias,
        "total_subcriterios": total,
        "porcentaje_coincidencia": porcentaje,
        "criterios_cumplidos": cumplidos,
        "criterios_no_cumplidos": no_cumplidos,
        "detalles_criterios": detalles,
    }


def aplicar_ranking_criterios(productos, incluidos):
    resultado = []

    for producto in productos:
        ranking = puntuar_producto(producto, incluidos)

        if ranking["coincidencias"] == 0:
            continue

        producto = dict(producto)
        producto["ranking_criterios"] = ranking
        producto["score_coincidencia"] = ranking["coincidencias"]
        producto["porcentaje_coincidencia"] = ranking["porcentaje_coincidencia"]

        resultado.append(producto)

    # Prioridad absoluta: cantidad de criterios positivos cumplidos.
    # El score de oferta y el descuento solo desempatan dentro del mismo
    # nivel de coincidencia técnica.
    resultado.sort(
        key=lambda p: (
            p.get("score_coincidencia", 0),
            p.get("score_oferta", 0),
            p.get("descuento", 0) or 0,
        ),
        reverse=True,
    )

    return resultado


def evaluar_criterios_encadenados(producto, incluidos, excluidos):
    for criterio in excluidos:
        ok, _ = evaluar_subcriterio(producto, criterio)
        if ok:
            return False

    if not incluidos:
        return True

    return puntuar_producto(producto, incluidos)["coincidencias"] == len(incluidos)


def diagnosticar_criterios(producto, incluidos):
    return puntuar_producto(producto, incluidos)["criterios_no_cumplidos"]




def _nombre_archivo_cache_ficha(sku):
    """Convierte un SKU en un nombre de archivo seguro y estable."""
    texto = str(sku or "").strip()
    if not texto:
        return ""
    texto = re.sub(r"[^A-Za-z0-9._-]+", "_", texto)
    return texto[:180]


def cargar_ficha_cache(sku):
    """
    Carga una Ficha Técnica previamente guardada.

    El caché está indexado por SKU. No se considera válida una entrada
    incompleta o una ficha que no haya podido descargarse.
    """
    nombre = _nombre_archivo_cache_ficha(sku)
    if not nombre:
        return None

    archivo = CACHE_FICHAS_DIR / f"{nombre}.json"
    if not archivo.exists():
        return None

    try:
        data = json.loads(archivo.read_text(encoding="utf-8"))
    except Exception:
        return None

    ficha = data.get("ficha") if isinstance(data, dict) else None
    if not isinstance(ficha, dict):
        # Compatibilidad con un caché que haya guardado directamente
        # el objeto ficha.
        ficha = data if isinstance(data, dict) else None

    if not isinstance(ficha, dict):
        return None

    if not ficha.get("disponible"):
        return None

    if not isinstance(ficha.get("filas"), list) or not ficha.get("filas"):
        return None

    return ficha


def guardar_ficha_cache(sku, ficha, producto=None):
    """Guarda únicamente fichas válidas; nunca cachea errores de descarga."""
    if not sku or not isinstance(ficha, dict):
        return False
    if not ficha.get("disponible"):
        return False
    if not isinstance(ficha.get("filas"), list) or not ficha.get("filas"):
        return False

    nombre = _nombre_archivo_cache_ficha(sku)
    if not nombre:
        return False

    try:
        CACHE_FICHAS_DIR.mkdir(parents=True, exist_ok=True)
        archivo = CACHE_FICHAS_DIR / f"{nombre}.json"
        payload = {
            "sku": str(sku),
            "url": (producto or {}).get("url") if isinstance(producto, dict) else None,
            "producto": (producto or {}).get("producto") if isinstance(producto, dict) else None,
            "ficha": ficha,
        }
        archivo.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except Exception:
        return False



def obtener_ficha_con_cache(page, producto):
    """Compatibilidad con el flujo secuencial anterior."""
    sku = str(producto.get("sku") or "").strip()
    if sku:
        ficha_cache = cargar_ficha_cache(sku)
        if ficha_cache is not None:
            return ficha_cache, True

    ficha = extraer_ficha_tecnica(page, producto.get("url"))
    if sku and ficha.get("disponible"):
        guardar_ficha_cache(sku, ficha, producto)
    return ficha, False


async def extraer_ficha_tecnica_async(page, url):
    """Descarga una ficha usando una Page async exclusiva de este worker."""
    if not url:
        return {
            "disponible": False, "error": "URL de producto vacía",
            "filas": [], "datos": {}, "descripcion": ""
        }

    descripcion = ""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)

        try:
            desc = page.locator(".main-description").first
            if await desc.count():
                descripcion = " ".join((await desc.inner_text()).split()).strip()
        except Exception:
            pass

        try:
            await page.wait_for_selector(
                "div[role='tab'], .mat-tab-label", timeout=20000
            )
        except Exception:
            pass

        tab = page.locator("div[role='tab']").filter(
            has_text=re.compile(r"^\s*Ficha Técnica\s*$", re.I)
        )
        if not await tab.count():
            tab = page.locator(".mat-tab-label").filter(
                has_text=re.compile(r"^\s*Ficha Técnica\s*$", re.I)
            )
        if not await tab.count():
            tab = page.get_by_text("Ficha Técnica", exact=True)

        if not await tab.count():
            return {
                "disponible": False,
                "error": "No se encontró la pestaña Ficha Técnica",
                "filas": [], "datos": {}, "descripcion": descripcion
            }

        await tab.first.click()
        await page.wait_for_timeout(500)

        try:
            await page.wait_for_selector(
                ".mat-tab-body-active table", timeout=10000
            )
        except Exception:
            pass

        tablas = page.locator(".mat-tab-body-active table")
        if not await tablas.count():
            tablas = page.locator("table.table")
        if not await tablas.count():
            tablas = page.locator("table")

        tabla = None
        for i in range(await tablas.count()):
            candidata = tablas.nth(i)
            if (
                await candidata.locator("tr th").count()
                and await candidata.locator("tr td").count()
            ):
                tabla = candidata
                break

        if tabla is None:
            return {
                "disponible": False,
                "error": "No se encontró tabla con campos de Ficha Técnica",
                "filas": [], "datos": {}, "descripcion": descripcion
            }

        filas = []
        datos = {}
        rows = tabla.locator("tr")

        for i in range(await rows.count()):
            row = rows.nth(i)
            th = row.locator("th").first
            td = row.locator("td").first
            if not await th.count() or not await td.count():
                continue

            campo = " ".join((await th.inner_text()).split()).strip()
            valor = " ".join((await td.inner_text()).split()).strip()
            if not campo:
                continue

            filas.append({"campo": campo, "valor": valor})
            clave = campo.rstrip(":").strip().lower()
            clave = re.sub(r"\s+", " ", clave)

            if clave in datos:
                if not isinstance(datos[clave], list):
                    datos[clave] = [datos[clave]]
                datos[clave].append(valor)
            else:
                datos[clave] = valor

        if not filas:
            return {
                "disponible": False, "error": "Ficha Técnica vacía",
                "filas": [], "datos": {}, "descripcion": descripcion
            }

        return {
            "disponible": True,
            "filas": filas,
            "datos": datos,
            "descripcion": descripcion,
        }

    except Exception as exc:
        return {
            "disponible": False,
            "error": str(exc),
            "filas": [],
            "datos": {},
            "descripcion": descripcion,
        }


async def _descargar_ficha_worker(browser, producto):
    page = await browser.new_page()
    try:
        ficha = await extraer_ficha_tecnica_async(
            page, producto.get("url")
        )
        return producto, ficha
    finally:
        await page.close()


async def descargar_fichas_lote_async(productos):
    """Descarga hasta 4 fichas simultáneamente."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            tareas = [
                _descargar_ficha_worker(browser, producto)
                for producto in productos
            ]
            return await asyncio.gather(*tareas)
        finally:
            await browser.close()


def _descargar_ficha_worker_sync(producto, indice, total):
    """
    Descarga una ficha en un hilo aislado con su propio Playwright.

    Playwright sync no es thread-safe: cada worker crea su propio contexto
    Playwright/browser/page y lo cierra al terminar. Así evitamos tanto
    asyncio.run() anidado como compartir una Page entre hilos.
    """
    from playwright.sync_api import sync_playwright

    nombre = producto.get("producto") or producto.get("sku") or "producto"
    t0 = time.perf_counter()

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page_worker = browser.new_page(
                viewport={"width": 1440, "height": 1000},
                locale="es-AR",
            )
            try:
                ficha = extraer_ficha_tecnica(page_worker, producto.get("url"))
            finally:
                page_worker.close()
                browser.close()

        return indice, producto, ficha, time.perf_counter() - t0
    except Exception as exc:
        ficha = {
            "disponible": False,
            "error": str(exc),
            "filas": [],
            "datos": {},
            "descripcion": "",
        }
        return indice, producto, ficha, time.perf_counter() - t0


def descargar_fichas_paralelas(page, productos, max_paralelas=4):
    """
    Descarga fichas en paralelo, por defecto 4 simultáneas.

    ``page`` se conserva en la firma para compatibilidad con el resto del
    scraper, pero NO se comparte entre hilos. Cada worker usa su propio
    Playwright sync, evitando el error de asyncio.run() dentro del scraper.
    """
    inicio = time.perf_counter()
    resultados = []
    total = len(productos)

    if not total:
        return resultados, 0.0

    max_paralelas = max(1, int(max_paralelas or 1))
    print(
        f"[BNA] Descarga de fichas: {total} | "
        f"concurrencia={max_paralelas}"
    )

    # Mantener el orden original facilita asociar cada resultado con el SKU.
    ordenados = {}
    with ThreadPoolExecutor(max_workers=max_paralelas) as executor:
        futuros = {
            executor.submit(
                _descargar_ficha_worker_sync,
                producto,
                indice,
                total,
            ): indice
            for indice, producto in enumerate(productos, 1)
        }

        for futuro in as_completed(futuros):
            indice, producto, ficha, dt = futuro.result()
            ordenados[indice] = (producto, ficha)
            nombre = producto.get("producto") or producto.get("sku") or "producto"

            if ficha.get("disponible"):
                print(
                    f"[BNA]    OK {indice}/{total} | "
                    f"{nombre[:75]} | "
                    f"{len(ficha.get('filas', []))} campos | {dt:.2f} s"
                )
            else:
                print(
                    f"[BNA]    ERROR {indice}/{total} | "
                    f"{nombre[:75]} | "
                    f"{ficha.get('error', 'sin detalle')} | {dt:.2f} s"
                )

    resultados = [ordenados[i] for i in range(1, total + 1)]
    dt_total = time.perf_counter() - inicio
    ok = sum(1 for _, ficha in resultados if ficha.get("disponible"))
    print(
        f"[BNA]    Lote terminado: {dt_total:.2f} s | "
        f"OK={ok} | errores={total - ok}"
    )
    gc.collect()
    return resultados, dt_total


def extraer_ficha_tecnica(page, url):
    """
    Abre la página del producto, activa 'Ficha Técnica' y extrae
    la tabla completa como filas y diccionario.
    """
    if not url:
        return {
            "disponible": False,
            "error": "URL de producto vacía",
            "filas": [],
            "datos": {},
            "descripcion": "",
        }

    try:
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=90000
        )

        # La descripción puede contener especificaciones que no aparecen en
        # la Ficha Técnica (por ejemplo, "Memoria RAM: 16 GB").
        descripcion = ""
        try:
            desc = page.locator(".main-description").first
            if desc.count():
                descripcion = " ".join(desc.inner_text().split()).strip()
        except Exception:
            descripcion = ""

        try:
            page.wait_for_selector(
                "div[role='tab'], .mat-tab-label",
                timeout=20000
            )
        except Exception:
            pass

        tab = page.locator("div[role='tab']").filter(
            has_text=re.compile(r"^\s*Ficha Técnica\s*$", re.I)
        )

        if not tab.count():
            tab = page.locator(".mat-tab-label").filter(
                has_text=re.compile(r"^\s*Ficha Técnica\s*$", re.I)
            )

        if not tab.count():
            tab = page.get_by_text(
                "Ficha Técnica",
                exact=True
            )

        if not tab.count():
            return {
                "disponible": False,
                "error": "No se encontró la pestaña Ficha Técnica",
                "filas": [],
                "datos": {},
            }

        tab.first.click()
        page.wait_for_timeout(500)

        try:
            page.wait_for_selector(
                ".mat-tab-body-active table",
                timeout=10000
            )
        except Exception:
            pass

        tablas = page.locator(
            ".mat-tab-body-active table"
        )

        if not tablas.count():
            tablas = page.locator("table.table")

        if not tablas.count():
            tablas = page.locator("table")

        tabla = None

        for i in range(tablas.count()):
            candidata = tablas.nth(i)
            if (
                candidata.locator("tr th").count()
                and candidata.locator("tr td").count()
            ):
                tabla = candidata
                break

        if tabla is None:
            return {
                "disponible": False,
                "error": "No se encontró tabla con campos de Ficha Técnica",
                "filas": [],
                "datos": {},
            }

        filas = []
        datos = {}

        rows = tabla.locator("tr")

        for i in range(rows.count()):
            row = rows.nth(i)

            th = row.locator("th").first
            td = row.locator("td").first

            if not th.count() or not td.count():
                continue

            campo = " ".join(
                th.inner_text().split()
            ).strip()

            valor = " ".join(
                td.inner_text().split()
            ).strip()

            if not campo:
                continue

            filas.append({
                "campo": campo,
                "valor": valor,
            })

            clave = campo.rstrip(":").strip().lower()
            clave = re.sub(r"\s+", " ", clave)

            if clave in datos:
                if not isinstance(datos[clave], list):
                    datos[clave] = [datos[clave]]
                datos[clave].append(valor)
            else:
                datos[clave] = valor

        return {
            "disponible": bool(filas),
            "filas": filas,
            "datos": datos,
            "descripcion": descripcion,
        }

    except Exception as exc:
        return {
            "disponible": False,
            "error": str(exc),
            "filas": [],
            "datos": {},
            "descripcion": descripcion,
        }


def enriquecer_con_ficha_tecnica(page, productos, max_fichas=0,
                                 criterios_incluidos=None,
                                 criterios_excluidos=None):
    """
    Caché primero; faltantes descargados en paralelo de a 4.
    El procesamiento contextual posterior sigue siendo secuencial.
    """
    candidatos = [p for p in productos if p.get("url")]
    if max_fichas and max_fichas > 0:
        candidatos = candidatos[:max_fichas]

    total = len(candidatos)
    if not total:
        print("[BNA] No hay productos con URL para consultar Ficha Técnica.")
        return productos

    t_total = time.perf_counter()
    resultados = {}
    pendientes = []
    cache_hits = 0

    t0 = time.perf_counter()
    for producto in candidatos:
        sku = str(producto.get("sku") or "").strip()
        ficha = cargar_ficha_cache(sku) if sku else None
        if ficha is not None:
            resultados[id(producto)] = (ficha, True)
            cache_hits += 1
        else:
            pendientes.append(producto)

    print(
        f"\n[BNA] Fichas: {total} | CACHE={cache_hits} | "
        f"WEB pendientes={len(pendientes)} | "
        f"revisión caché={time.perf_counter() - t0:.2f} s"
    )

    web_ok = 0
    web_error = 0
    if pendientes:
        descargadas, tiempo_web = descargar_fichas_paralelas(
            page, pendientes, max_paralelas=4
        )
        for producto, ficha in descargadas:
            if ficha.get("disponible"):
                web_ok += 1
                sku = str(producto.get("sku") or "").strip()
                if sku:
                    guardar_ficha_cache(sku, ficha, producto)
            else:
                web_error += 1
            resultados[id(producto)] = (ficha, False)

        print(
            f"[BNA] Descarga WEB total: {tiempo_web:.2f} s | "
            f"OK={web_ok} | errores={web_error}"
        )

    t_proceso = time.perf_counter()

    for producto in candidatos:
        ficha, desde_cache = resultados.get(
            id(producto),
            ({
                "disponible": False, "error": "No se obtuvo ficha",
                "filas": [], "datos": {}, "descripcion": ""
            }, False)
        )

        producto["ficha_tecnica"] = ficha
        producto["ficha_desde_cache"] = desde_cache
        producto["descripcion"] = ficha.get("descripcion", "")

        # IMPORTANTE: se conserva el parser contextual existente.
        try:
            producto["especificaciones"] = parse_especificaciones_contextual(
                producto
            )
        except Exception:
            pass

        if ficha.get("disponible") and criterios_incluidos:
            ranking = puntuar_producto(producto, criterios_incluidos)
            producto["ranking_criterios"] = ranking
            producto["score_coincidencia"] = ranking["coincidencias"]
            producto["porcentaje_coincidencia"] = ranking["porcentaje_coincidencia"]

            excluido = any(
                evaluar_subcriterio(producto, ex)[0]
                for ex in (criterios_excluidos or [])
            )
            producto["pasa_filtro_positivo"] = ranking["coincidencias"] >= 1
            producto["descartado_por_exclusion"] = excluido

    print(
        f"[BNA] Procesamiento contextual: "
        f"{time.perf_counter() - t_proceso:.2f} s"
    )
    print(
        f"[BNA] RESUMEN FICHAS: total={total} | "
        f"CACHE={cache_hits} | WEB={web_ok} | errores={web_error}"
    )
    print(
        f"[BNA] TIEMPO TOTAL ETAPA FICHAS: "
        f"{time.perf_counter() - t_total:.2f} s"
    )

    return productos



def ejecutar_scraper(
    queries,
    max_pages,
    max_productos,
    salida_base,
    preguntar_paginas=True,
    obtener_fichas=False,
    max_fichas=0,
    categoria_slug=None,
    categoria_nombre=None,
    modo_completo=False,
):
    """
    Busca productos directamente en el catálogo.

    max_pages:
      - None / 0: recorrer todas las páginas detectadas.
      - >0: límite de seguridad/manual.

    La paginación de Tienda BNA expone enlaces como:
        /catalog?query=gamer&...&p=26

    Por eso primero detectamos el mayor ?p= visible y usamos ese
    valor como total de páginas.
    """
    from urllib.parse import quote, urljoin
    from playwright.sync_api import sync_playwright

    productos = []
    vistos = set()
    fichas_procesadas_global = 0

    def parse_precio(texto):
        if not texto:
            return None
        m = re.search(r"[\d.]+(?:,\d+)?", texto)
        if not m:
            return None
        return int(
            m.group(0)
            .replace(".", "")
            .replace(",", ".")
            .split(".")[0]
        )

    def parse_financiacion(texto):
        if not texto:
            return []
        resultados = []

        patron = re.compile(
            r"(\d+)\s+cuotas?\s+"
            r"(sin\s+inter[eé]s\s+)?"
            r"de\s+\$\s*([\d.,]+)"
            r"(?:\s+con\s+(.+))?",
            re.I
        )

        for m in patron.finditer(texto):
            cuotas = int(m.group(1))
            sin_interes = bool(m.group(2))
            valor = float(
                m.group(3).replace(".", "").replace(",", ".")
            )
            medio = (m.group(4) or "").strip() or None

            resultados.append({
                "cuotas": cuotas,
                "valor_cuota": valor,
                "total": round(valor * cuotas, 2),
                "sin_interes": sin_interes,
                "medio": medio,
                "texto_original": m.group(0).strip(),
            })

        return resultados

    def detectar_total_paginas(page):
        """Detecta el total real de páginas del paginador de Tienda BNA.

        La SPA puede tardar en montar el paginador, por eso esperamos explícitamente
        a que aparezca y además buscamos cualquier enlace cuyo href contenga ?p=N/&p=N.
        No dependemos de que el enlace esté dentro de .pagination-complete.
        """
        # Dar tiempo a Angular para montar el paginador.
        try:
            page.wait_for_selector(".pagination-complete", timeout=10000)
        except Exception:
            pass

        # No bloqueamos con wait_for_timeout; si no hay paginador, una sola
        # página es un resultado válido.
        max_page = 1

        # Primero: enlaces del paginador, si existen.
        links = page.locator("a[href*='p=']")
        try:
            cantidad = links.count()
        except Exception:
            cantidad = 0

        for i in range(cantidad):
            link = links.nth(i)
            href = link.get_attribute("href") or ""
            texto = (link.inner_text() or "").strip()

            # Solo aceptar p=N, evitando cualquier otro número del href.
            matches = re.findall(r"(?:[?&]p=)(\d+)(?:&|$)", href)
            for m in matches:
                pagina_href = int(m)
                # Si el texto es numérico, exigimos coherencia.
                if re.fullmatch(r"\d+", texto):
                    if int(texto) == pagina_href:
                        max_page = max(max_page, pagina_href)
                else:
                    # El enlace puede estar renderizado sin texto útil.
                    max_page = max(max_page, pagina_href)

        # Segundo: revisar específicamente el paginador completo.
        paginador = page.locator(".pagination-complete")
        if paginador.count():
            hrefs = paginador.locator("a")
            for i in range(hrefs.count()):
                href = hrefs.nth(i).get_attribute("href") or ""
                m = re.search(r"(?:[?&]p=)(\d+)(?:&|$)", href)
                if m:
                    max_page = max(max_page, int(m.group(1)))

        current = page.locator(".pagination-complete .current")
        if current.count():
            texto_actual = (current.first.inner_text() or "").strip()
            if re.fullmatch(r"\d+", texto_actual):
                max_page = max(max_page, int(texto_actual))

        return max_page

    def extraer_pagina(page, url, query, pagina_num):
        page.goto(url, wait_until="domcontentloaded", timeout=90000)

        try:
            page.wait_for_selector(
                "article#modern-variant-card",
                timeout=30000
            )
        except Exception:
            pass

        # Activar lazy loading.
        for _ in range(8):
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(350)

        cards = page.locator("article#modern-variant-card")
        encontrados = 0
        productos_pagina = []

        for i in range(cards.count()):
            card = cards.nth(i)

            sku = card.get_attribute("data-sku")
            nombre = card.get_attribute("data-name")

            if not sku:
                continue

            clave = str(sku)

            link = card.locator("a[href*='/products/']").first
            href = link.get_attribute("href") if link.count() else None
            producto_url = urljoin("https://www.tiendabna.com.ar", href) if href else None

            imagen = None
            if link.count():
                style = link.get_attribute("style") or ""
                m = re.search(r"url\(['\"]?([^'\")]+)", style)
                if m:
                    imagen = m.group(1)

            precio_anterior = parse_precio(
                card.locator(".price").first.inner_text()
                if card.locator(".price").count() else ""
            )

            precio = parse_precio(
                card.locator(".sale-price").first.inner_text()
                if card.locator(".sale-price").count() else
                card.locator(".price").first.inner_text()
                if card.locator(".price").count() else ""
            )

            descuento = None
            badge = card.locator(".badge-sale-price").first
            if badge.count():
                txt = badge.inner_text()
                m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", txt)
                if m:
                    descuento = float(m.group(1).replace(",", "."))

            financiacion = []
            amount = card.locator(".amount.cost")
            for j in range(amount.count()):
                financiacion.extend(
                    parse_financiacion(amount.nth(j).inner_text())
                )

            precio_sin_impuestos = None
            sin_imp = card.locator(".without-tax").first
            if sin_imp.count():
                precio_sin_impuestos = parse_precio(sin_imp.inner_text())

            footer = ""
            if card.locator(".card-footer-badge").count():
                footer = card.locator(".card-footer-badge").inner_text()

            envio_gratis = "envío gratis" in footer.lower() or "envio gratis" in footer.lower()

            producto = {
                "sku": str(sku),
                "producto": nombre,
                "url": producto_url,
                "imagen": imagen,
                "precio_anterior": precio_anterior,
                "precio": precio,
                "descuento": descuento,
                "financiacion": financiacion,
                "precio_sin_impuestos": precio_sin_impuestos,
                "envio_gratis": envio_gratis,
                "_query": query,
            }

            productos_pagina.append(producto)

            # SKU identifica el producto, pero NO descarta una segunda oferta.
            # Guardamos las distintas apariciones para poder compararlas.
            snapshot = {
                "pagina": pagina_num,
                "url": producto_url,
                "precio_anterior": precio_anterior,
                "precio": precio,
                "descuento": descuento,
                "financiacion": financiacion,
                "precio_sin_impuestos": precio_sin_impuestos,
                "envio_gratis": envio_gratis,
            }

            existente = next((x for x in productos if str(x.get("sku")) == clave and x.get("_query") == query), None)
            if existente is not None:
                ofertas = existente.setdefault("ofertas_sku", [])
                # No duplicar exactamente la misma aparición.
                if snapshot not in ofertas:
                    ofertas.append(snapshot)
                    existente["cantidad_apariciones_sku"] = len(ofertas)
                    precios = [o.get("precio") for o in ofertas + [{"precio": existente.get("precio")} ] if o.get("precio") is not None]
                    if precios:
                        existente["mejor_precio_sku"] = min(precios)
                        existente["peor_precio_sku"] = max(precios)
                    existente["sku_comparado"] = True
                continue

            producto["ofertas_sku"] = [snapshot]
            producto["cantidad_apariciones_sku"] = 1
            producto["mejor_precio_sku"] = precio
            producto["peor_precio_sku"] = precio
            producto["sku_comparado"] = True
            producto["primera_aparicion_sku"] = snapshot
            vistos.add(clave)
            productos.append(producto)
            encontrados += 1

            if max_productos and len([x for x in productos if x.get("_query") == query]) >= max_productos:
                break

        return encontrados, productos_pagina


    modo_fichas_por_pagina = bool(obtener_fichas)

    def procesar_fichas_de_pagina(productos_pagina, query, incluidos, excluidos):
        """
        Procesa únicamente los SKUs nuevos de ESTA página.

        Los SKUs ya presentes en cache_bna/fichas se reutilizan sin descarga.
        En modo normal se pregunta cuántos SKUs nuevos quiere procesar.
        En --completo se procesan automáticamente todos los nuevos.
        """
        nonlocal fichas_procesadas_global

        if not productos_pagina:
            print("[BNA]   Página sin productos para Ficha Técnica.")
            return

        # Una observación por SKU para decidir qué fichas nuevas consultar.
        por_sku = {}
        for prod in productos_pagina:
            sku = str(prod.get("sku") or "").strip()
            if sku and sku not in por_sku:
                por_sku[sku] = prod

        # Primero enriquecemos solo con datos baratos del card.
        for prod in por_sku.values():
            enriquecer_producto(prod)

        candidatos, descartados_nombre = seleccionar_candidatos_por_nombre(
            list(por_sku.values()), incluidos, excluidos
        )

        # El usuario quiere saber los SKUs nuevos de la página, no solamente
        # los que quedaron después de un filtro barato. Para no gastar fichas
        # innecesariamente, la selección final sí respeta los candidatos.
        nuevos_todos = [
            prod for prod in por_sku.values()
            if cargar_ficha_cache(str(prod.get("sku") or "").strip()) is None
        ]
        nuevos_candidatos = [
            prod for prod in candidatos
            if cargar_ficha_cache(str(prod.get("sku") or "").strip()) is None
        ]

        print(
            f"[BNA]   Página: {len(productos_pagina)} apariciones | "
            f"{len(por_sku)} SKUs únicos | "
            f"SKUs nuevos en caché: {len(nuevos_todos)}"
        )
        if incluidos:
            print(
                f"[BNA]   Candidatos a Ficha Técnica: {len(candidatos)} | "
                f"nuevos procesables: {len(nuevos_candidatos)} | "
                f"descartados por '-': {descartados_nombre}"
            )

        # Adjuntar fichas ya existentes sin consumir cuota.
        existentes_con_ficha = []
        for prod in por_sku.values():
            sku = str(prod.get("sku") or "").strip()
            ficha = cargar_ficha_cache(sku) if sku else None
            if ficha is not None:
                existentes_con_ficha.append(prod)

        disponibles = list(nuevos_candidatos)
        if max_fichas and max_fichas > 0:
            restantes = max(0, max_fichas - fichas_procesadas_global)
            if restantes <= 0:
                print(
                    f"[BNA]   Límite acumulado --max-fichas={max_fichas} ya alcanzado."
                )
                disponibles = []
            elif len(disponibles) > restantes:
                disponibles = disponibles[:restantes]
                print(
                    f"[BNA]   --max-fichas deja {restantes} fichas nuevas "
                    f"disponibles en esta página."
                )

        seleccionados = disponibles
        if disponibles and not modo_completo:
            while True:
                try:
                    respuesta = input(
                        f"[BNA]   ¿Cuántos de los {len(disponibles)} SKUs nuevos desea "
                        f"procesar? [0-{len(disponibles)}, Enter = todos]: "
                    ).strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    respuesta = "0"

                if respuesta == "":
                    seleccionados = disponibles
                    break
                if respuesta.isdigit() and 0 <= int(respuesta) <= len(disponibles):
                    seleccionados = disponibles[:int(respuesta)]
                    break
                print(
                    f"[BNA]   Introduzca un número entre 0 y {len(disponibles)}, "
                    "o Enter para todos."
                )

        if disponibles and modo_completo:
            print(
                f"[BNA]   --completo: procesando automáticamente "
                f"{len(disponibles)} SKUs nuevos."
            )

        if seleccionados:
            # Solo se descargan SKUs nuevos seleccionados. Las fichas cacheadas
            # se agregan después para que la evaluación final tenga todos los datos.
            enriquecer_con_ficha_tecnica(
                page,
                seleccionados,
                max_fichas=0,
                criterios_incluidos=incluidos,
                criterios_excluidos=excluidos,
            )
            fichas_procesadas_global += len(seleccionados)
            print(
                f"[BNA]   Fichas nuevas procesadas en esta página: "
                f"{len(seleccionados)} | acumuladas: {fichas_procesadas_global}"
            )
        elif not disponibles:
            print("[BNA]   No hay SKUs nuevos para procesar en esta página.")
        else:
            print("[BNA]   Se seleccionaron 0 fichas nuevas.")

        # Refrescar/adjuntar la ficha cacheada a TODAS las apariciones de esta página.
        # Así un SKU viejo y uno recién procesado siguen usando la misma ficha.
        mapa_fichas = {}
        for prod in por_sku.values():
            sku = str(prod.get("sku") or "").strip()
            ficha = cargar_ficha_cache(sku) if sku else None
            if ficha is not None:
                mapa_fichas[sku] = ficha

        for prod in productos_pagina:
            sku = str(prod.get("sku") or "").strip()
            ficha = mapa_fichas.get(sku)
            if ficha is not None:
                prod["ficha_tecnica"] = ficha
                prod["ficha_desde_cache"] = True
                prod["descripcion"] = ficha.get("descripcion", "")
                try:
                    prod["especificaciones"] = parse_especificaciones_contextual(prod)
                except Exception:
                    pass
                if ficha.get("disponible") and incluidos:
                    ranking = puntuar_producto(prod, incluidos)
                    prod["ranking_criterios"] = ranking
                    prod["score_coincidencia"] = ranking["coincidencias"]
                    prod["porcentaje_coincidencia"] = ranking["porcentaje_coincidencia"]
                    prod["pasa_filtro_positivo"] = ranking["coincidencias"] >= 1
                    prod["descartado_por_exclusion"] = any(
                        evaluar_subcriterio(prod, ex)[0] for ex in excluidos
                    )

        # Enriquecer también el objeto consolidado global por SKU.
        for prod in por_sku.values():
            sku = str(prod.get("sku") or "").strip()
            ficha = mapa_fichas.get(sku)
            if ficha is not None:
                prod["ficha_tecnica"] = ficha
                prod["ficha_desde_cache"] = True
                prod["descripcion"] = ficha.get("descripcion", "")
                try:
                    prod["especificaciones"] = parse_especificaciones_contextual(prod)
                except Exception:
                    pass

        gc.collect()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1440, "height": 1000},
            locale="es-AR",
        )

        for query in queries:
            criterios = parse_criterio_encadenado(query)
            base_query = criterios["base_query"] or query
            incluidos = criterios["incluidos"]
            excluidos = criterios["excluidos"]

            categoria_bna = categoria_slug

            if categoria_bna:
                # categoria_bna puede ser un href completo de BNA:
                # /ar/tecnologia-computacion
                # o un slug: tecnologia-computacion.
                ruta_categoria = str(categoria_bna).strip()
                ruta_categoria = re.sub(r"^https?://www\.tiendabna\.com\.ar", "", ruta_categoria)
                ruta_categoria = ruta_categoria.split("?", 1)[0].strip("/")
                if ruta_categoria.startswith("ar/"):
                    ruta_categoria = ruta_categoria[3:]
                ruta_categoria = ruta_categoria.strip("/")

                base_url = (
                    "https://www.tiendabna.com.ar/catalog/"
                    + ruta_categoria
                    + "?query=" + quote(str(base_query).replace(" ", "+"), safe="+")
                    + "&o=" + ORDEN_BNA
                )
                print(
                    f"[BNA] Categoría seleccionada: {categoria_nombre or categoria_bna} "
                    f"[{categoria_bna}] (búsqueda base: {base_query})"
                )
            else:
                base_url = (
                    "https://www.tiendabna.com.ar/catalog?query="
                    + quote(str(base_query).replace(" ", "+"), safe="+")
                    + "&o=" + ORDEN_BNA
                )
                print("[BNA] Alcance: TODAS LAS CATEGORÍAS; catálogo general")

            print(f"[BNA] Orden forzado: {NOMBRE_ORDEN_BNA} ({ORDEN_BNA})")
            print(f"[BNA] URL inicial: {base_url}")

            page.goto(
                base_url,
                wait_until="domcontentloaded",
                timeout=90000
            )

            try:
                page.wait_for_selector(
                    "article#modern-variant-card",
                    timeout=30000
                )
            except Exception:
                pass

            # El paginador puede aparecer después de las tarjetas. Esperamos
            # explícitamente a que Angular termine de renderizarlo antes de
            # detectar cuántas páginas existen.
            try:
                page.wait_for_selector(
                    ".pagination-complete",
                    timeout=15000
                )
            except Exception:
                pass
            page.wait_for_timeout(1000)

            total = detectar_total_paginas(page)

            if max_pages and max_pages > 0:
                total_a_recorrer = min(total, max_pages)
                limite_txt = f" (límite configurado: {max_pages})"
            elif preguntar_paginas and sys.stdin.isatty():
                while True:
                    try:
                        respuesta = input(
                            f"[BNA] ¿Cuántas páginas desea recorrer? [1-{total}, Enter = todas]: "
                        ).strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        respuesta = ""

                    if respuesta == "":
                        total_a_recorrer = total
                        break
                    if respuesta.isdigit() and 1 <= int(respuesta) <= total:
                        total_a_recorrer = int(respuesta)
                        break
                    print(f"[BNA] Introduzca un número entre 1 y {total}, o Enter para todas.")
                limite_txt = " (selección interactiva)"
            else:
                total_a_recorrer = total
                limite_txt = " (todas; ejecución no interactiva)"

            print(
                f"[BNA] Páginas detectadas: {total} | "
                f"se recorrerán: {total_a_recorrer}{limite_txt}"
            )

            for pagina in range(1, total_a_recorrer + 1):
                if pagina == 1:
                    url = base_url
                else:
                    url = f"{base_url}&p={pagina}"

                print(
                    f"[BNA] Página {pagina}/{total_a_recorrer}: {url}"
                )

                try:
                    n, productos_pagina = extraer_pagina(page, url, query, pagina)
                    productos_actuales = [x for x in productos if x.get("_query") == query]
                    print(f"[BNA]   productos nuevos: {n} | acumulados: {len(productos_actuales)}")

                    if modo_fichas_por_pagina:
                        procesar_fichas_de_pagina(
                            productos_pagina,
                            query,
                            incluidos,
                            excluidos,
                        )

                    # Guardado incremental: si la ejecución se interrumpe después
                    # de esta página, las páginas ya procesadas quedan persistidas.
                    nombre_base_tmp = re.sub(
                        r"[^a-zA-Z0-9áéíóúüñÁÉÍÓÚÜÑ_-]+",
                        "_",
                        base_query.strip(),
                    )
                    nombre_base_tmp = re.sub(r"_+", "_", nombre_base_tmp).strip("_")
                    archivo_incremental = BASE / f"resultados_{nombre_base_tmp}.json"
                    archivo_incremental.write_text(
                        json.dumps(
                            [x for x in productos if x.get("_query") == query],
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )

                    # Liberamos DOM/objetos asociados a esta página antes de
                    # continuar. La siguiente página se abre sobre una Page nueva.
                    try:
                        page.close()
                    except Exception:
                        pass
                    page = browser.new_page(
                        viewport={"width": 1440, "height": 1000},
                        locale="es-AR",
                    )
                    gc.collect()

                    if max_productos:
                        total_query = len([x for x in productos if x.get("_query") == query])
                        if total_query >= max_productos:
                            print(f"[BNA]   alcanzado max-productos={max_productos}")
                            break
                except Exception as exc:
                    print(
                        f"[BNA]   ERROR página {pagina}: {exc}"
                    )

            # Guardar un JSON independiente para cada búsqueda.
            productos_query = [
                p for p in productos
                if p.get("_query") == query
            ]

            # Para una consulta encadenada, los archivos se nombran usando
            # solamente la búsqueda base (ej. gamer), no los +filtros.
            nombre_base = re.sub(r"[^a-zA-Z0-9áéíóúüñÁÉÍÓÚÜÑ_-]+", "_", base_query.strip())
            nombre_base = re.sub(r"_+", "_", nombre_base).strip("_")
            archivo_query = BASE / f"resultados_{nombre_base}.json"

            archivo_query.write_text(
                json.dumps(
                    productos_query,
                    ensure_ascii=False,
                    indent=2
                ),
                encoding="utf-8"
            )

            # Primero enriquecemos datos del card.
            productos_query = [enriquecer_producto(p) for p in productos_query]
            # El catálogo maestro se actualiza con cada búsqueda general.
            # No se eliminan SKU ausentes: es un cache acumulativo por producto.
            actualizar_catalogo_bna(productos_query, base_query)

            # Pre-filtro: NO abrimos la Ficha Técnica de todo el catálogo.
            # Primero usamos solamente el nombre para descartar exclusiones
            # y conservar productos que cumplan al menos UN criterio '+'.
            # Luego la Ficha Técnica permite hacer el filtro AND definitivo.
            candidatos_ficha, descartados_nombre = seleccionar_candidatos_por_nombre(
                productos_query,
                incluidos,
                excluidos,
            )

            criterios_tecnicos = any(
                any(
                    alternativa.strip().lower().startswith((
                        "ram:", "storage:", "ssd:", "nvme:", "hdd:", "emmc:",
                        "gpu:", "placa:", "video:", "procesador:", "cpu:",
                        "ryzen:", "core:"
                    ))
                    for alternativa in (separar_grupo_or(c) or [c])
                )
                for c in incluidos
            )
            if criterios_tecnicos:
                print("[BNA] Criterios técnicos detectados: la Ficha Técnica se usará como fuente de verdad.")

            print(
                f"[BNA] Pre-filtro por nombre: {len(candidatos_ficha)} candidatos "
                f"de {len(productos_query)} "
                f"(descartados por '-': {descartados_nombre})"
            )

            if obtener_fichas and candidatos_ficha and not modo_fichas_por_pagina:
                antes_fichas = len(candidatos_ficha)
                limite_fichas = max_fichas
                if preguntar_paginas and sys.stdin.isatty() and max_fichas == 0:
                    while True:
                        try:
                            respuesta_f = input(
                                f"[BNA] ¿Cuántas Fichas Técnicas desea analizar? [0-{antes_fichas}, Enter = todas]: "
                            ).strip()
                        except (EOFError, KeyboardInterrupt):
                            print()
                            respuesta_f = ""
                        if respuesta_f == "":
                            limite_fichas = antes_fichas
                            break
                        if respuesta_f.isdigit() and 0 <= int(respuesta_f) <= antes_fichas:
                            limite_fichas = int(respuesta_f)
                            break
                        print(f"[BNA] Introduzca un número entre 0 y {antes_fichas}, o Enter para todas.")
                if limite_fichas == 0:
                    candidatos_ficha = []
                    print("[BNA] Fichas consultadas: 0")
                else:
                    candidatos_ficha = enriquecer_con_ficha_tecnica(
                        page,
                        candidatos_ficha,
                        max_fichas=limite_fichas,
                        criterios_incluidos=incluidos,
                        criterios_excluidos=excluidos,
                    )

                # Los objetos son mutables, por lo que las fichas quedan
                # también incorporadas en productos_query.
                fichas_realmente_consultadas = min(antes_fichas, limite_fichas)
                print(
                    f"[BNA] Fichas consultadas: "
                    f"{fichas_realmente_consultadas}"
                )

            # La lista que llega a la evaluación definitiva debe conservar
            # los descartes realizados por el pre-filtro de nombre. En la
            # versión anterior se volvía a recorrer productos_query completo
            # y eso permitía que un producto descartado por una exclusión
            # reapareciera.
            pool_definitivo = candidatos_ficha if incluidos else productos_query

            # Las exclusiones se aplican ANTES de los criterios positivos y
            # son una barrera definitiva. Se evalúan sobre nombre,
            # descripción, ficha, URL y SKU.
            if excluidos:
                pool_definitivo = [
                    p for p in pool_definitivo
                    if not any(
                        evaluar_subcriterio(p, ex)[0]
                        for ex in excluidos
                    )
                ]

            if incluidos:
                filtrados = []
                for producto in pool_definitivo:
                    ranking = puntuar_producto(producto, incluidos)
                    producto = dict(producto)
                    producto["ranking_criterios"] = ranking
                    producto["score_coincidencia"] = ranking["coincidencias"]
                    producto["porcentaje_coincidencia"] = ranking["porcentaje_coincidencia"]

                    # Filtro definitivo: TODOS los '+' deben cumplirse.
                    if ranking["coincidencias"] == len(incluidos):
                        filtrados.append(producto)
            else:
                filtrados = list(pool_definitivo)

            filtrados.sort(
                key=lambda p: (
                    p.get("score_coincidencia", 0),
                    p.get("porcentaje_coincidencia", 0),
                    p.get("score_oferta", 0),
                    p.get("descuento", 0) or 0,
                ),
                reverse=True,
            )

            archivo_query.write_text(
                json.dumps(productos_query, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

            archivo_filtrado = BASE / f"resultados_{nombre_base}_filtrado.json"
            archivo_filtrado.write_text(
                json.dumps(filtrados, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

            print(
                f"[BNA] JSON de búsqueda: {archivo_query.name} "
                f"({len(productos_query)} productos)"
            )
            print(
                f"[BNA] JSON filtrado:  {archivo_filtrado.name} "
                f"({len(filtrados)} productos cumplen criterios)"
            )

            if not filtrados and productos_query:
                print("[BNA] Diagnóstico: ningún producto cumplió TODOS los criterios.")
                muestras = []
                for prod in productos:
                    fallos = diagnosticar_criterios(prod, incluidos)
                    if fallos:
                        muestras.append((prod.get("producto", ""), fallos))
                    if len(muestras) >= 8:
                        break
                for nombre_prod, fallos in muestras:
                    print(f"[BNA]   NO: {nombre_prod}")
                    print(f"[BNA]       falla: {', '.join(fallos)}")

        browser.close()

    # Guardamos un temporal para mantener la estructura de la versión anterior.
    tmp = BASE / "_tmp_bna_todas_busquedas.json"
    tmp.write_text(
        json.dumps(productos, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    return [
        enriquecer_producto(p)
        for p in comparar_variantes(productos)
    ]


# ============================================================================
# MOTOR CONTEXTUAL V26
# ============================================================================
# La extracción ya NO toma cualquier número + GB del texto.
# Primero localiza el concepto (RAM, almacenamiento, Ryzen, etc.) y después
# busca valores cercanos, ponderando etiquetas, unidades y contexto.

def _texto_limpio_contextual(texto):
    s = str(texto or "")
    s = s.replace("\xa0", " ")
    s = s.replace("™", "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalizar_contexto(texto):
    s = _texto_limpio_contextual(texto).lower()
    s = re.sub(r"\brizen\b", "ryzen", s)
    s = re.sub(r"\bryz[ée]n\b", "ryzen", s)
    return s


def _numero_gb(numero, unidad):
    v = float(str(numero).replace(",", "."))
    if unidad.lower() == "tb":
        v *= 1024
    return v


def _capacidad_cercana(texto, inicio, fin):
    """Devuelve capacidades dentro de [inicio, fin], con posición."""
    patron = re.compile(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(GB|TB)", re.I)
    resultado = []
    for m in patron.finditer(texto, max(0, inicio), min(len(texto), fin)):
        resultado.append({
            "gb": int(round(_numero_gb(m.group(1), m.group(2)))),
            "numero_original": m.group(1),
            "unidad": m.group(2).upper(),
            "inicio": m.start(),
            "fin": m.end(),
            "texto": m.group(0),
        })
    return resultado


def _contexto(texto, inicio, fin, radio=55):
    a = max(0, inicio-radio)
    b = min(len(texto), fin+radio)
    return texto[a:b].strip()


def _bloques_texto_producto(producto):
    """Orden de confianza: descripción, ficha técnica, nombre."""
    bloques = []
    descripcion = _texto_limpio_contextual(producto.get("descripcion"))
    if descripcion:
        bloques.append(("descripcion", descripcion, 1.00))

    ficha = producto.get("ficha_tecnica") or {}
    for fila in ficha.get("filas", []) or []:
        campo = _texto_limpio_contextual(fila.get("campo"))
        valor = _texto_limpio_contextual(fila.get("valor"))
        if campo or valor:
            bloques.append(("ficha:" + campo.lower(), f"{campo}: {valor}", 1.00))

    nombre = _texto_limpio_contextual(producto.get("producto"))
    if nombre:
        bloques.append(("nombre", nombre, 0.70))

    return bloques


def _buscar_ram_contextual(texto, peso_base=1.0):
    """Busca RAM partiendo de las etiquetas y no de cualquier GB."""
    s = _normalizar_contexto(texto)
    candidatos = []

    # Caso ideal: "Memoria RAM: 8GB", "RAM 16 GB", "Memoria: 16 GB DDR4".
    anclas = list(re.finditer(r"(?:memoria\s+ram|memoria|ram\s*:?)", s, re.I))
    for ancla in anclas:
        ventana_fin = min(len(s), ancla.end() + 80)
        caps = _capacidad_cercana(s, ancla.end(), ventana_fin)
        for cap in caps:
            distancia = cap["inicio"] - ancla.end()
            if distancia < 0 or distancia > 80:
                continue
            sub = s[ancla.end():cap["fin"]]
            score = 50 + max(0, 30 - distancia)
            if re.search(r"\bddr\s*[2345]\b", sub, re.I):
                score += 20
            if "ram" in s[max(0, ancla.start()-15):ancla.end()+15]:
                score += 10
            if 4 <= cap["gb"] <= 128:
                score += 10
            # Si antes de la capacidad aparece "almacenamiento", ya no es RAM.
            if re.search(r"almacenamiento|storage|disco\s+(?:ssd|hdd)", sub, re.I):
                score -= 50
            candidatos.append({
                "valor": cap["gb"],
                "score": score * peso_base,
                "fuente": "contexto_ram",
                "texto": cap["texto"],
                "contexto": _contexto(s, ancla.start(), cap["fin"]),
                "posicion": [ancla.start(), cap["fin"]],
            })

    # Señal fuerte aunque falte la palabra RAM: "16GB DDR4".
    for m in re.finditer(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(GB|TB)\s*DDR\s*[2345]\b", s, re.I):
        gb = _numero_gb(m.group(1), m.group(2))
        if 4 <= gb <= 128:
            candidatos.append({
                "valor": int(round(gb)),
                "score": 82 * peso_base,
                "fuente": "ddr_contexto",
                "texto": m.group(0),
                "contexto": _contexto(s, m.start(), m.end()),
                "posicion": [m.start(), m.end()],
            })

    if not candidatos:
        return None
    return max(candidatos, key=lambda x: x["score"])


def _buscar_storage_contextual(texto, peso_base=1.0):
    """Busca almacenamiento a partir de su etiqueta/tipo y luego capacidad."""
    s = _normalizar_contexto(texto)
    candidatos = []

    # Caso ideal: "Almacenamiento: SSD 240GB".
    anclas = list(re.finditer(r"(?:almacenamiento|storage|capacidad|disco|unidad)\s*:?", s, re.I))
    for ancla in anclas:
        fin = min(len(s), ancla.end()+100)
        segmento = s[ancla.end():fin]
        caps = _capacidad_cercana(s, ancla.end(), fin)
        for cap in caps:
            antes = s[ancla.end():cap["inicio"]]
            tipo = None
            if re.search(r"\bnvme\b", antes, re.I):
                tipo = "nvme"
            elif re.search(r"\bssd\b", antes, re.I):
                tipo = "ssd"
            elif re.search(r"\bhdd\b|disco\s+r[ií]gido", antes, re.I):
                tipo = "hdd"
            elif re.search(r"\bemmc\b|e-mmc", antes, re.I):
                tipo = "emmc"

            distancia = cap["inicio"] - ancla.end()
            score = 50 + max(0, 30-distancia)
            if tipo:
                score += 30
            if "ssd" in segmento or "nvme" in segmento or "hdd" in segmento or "emmc" in segmento:
                score += 10
            candidatos.append({
                "valor": cap["gb"],
                "tipo": tipo,
                "score": score*peso_base,
                "fuente": "contexto_storage",
                "texto": cap["texto"],
                "contexto": _contexto(s, ancla.start(), cap["fin"]),
                "posicion": [ancla.start(), cap["fin"]],
            })

    # Segundo nivel: tipo de disco explícito con capacidad cercana.
    for tm in re.finditer(r"\b(NVMe|SSD|HDD|eMMC)\b", s, re.I):
        fin = min(len(s), tm.end()+45)
        caps = _capacidad_cercana(s, tm.end(), fin)
        for cap in caps:
            distancia = cap["inicio"]-tm.end()
            candidatos.append({
                "valor": cap["gb"],
                "tipo": tm.group(1).lower().replace("-", ""),
                "score": (72-max(0, distancia))*peso_base,
                "fuente": "tipo_storage",
                "texto": cap["texto"],
                "contexto": _contexto(s, tm.start(), cap["fin"]),
                "posicion": [tm.start(), cap["fin"]],
            })

    if not candidatos:
        return None
    return max(candidatos, key=lambda x: x["score"])


def _buscar_cpu_contextual(texto, peso_base=1.0):
    """Localiza AMD/Intel + familia CPU por proximidad textual."""
    s = _normalizar_contexto(texto)
    candidatos = []

    patrones = [
        re.compile(r"\b(amd\s+)?ryzen\s*(?:ai\s+)?([3579])\b(?:\s*([a-z]?\d{3,6}[a-z]?))?", re.I),
        re.compile(r"\b(intel\s+)?core\s*i([3579])\b(?:[-\s]*([a-z]?\d{3,6}[a-z]?))?", re.I),
    ]
    for patron in patrones:
        for m in patron.finditer(s):
            familia = "ryzen" if "ryzen" in m.group(0).lower() else "corei"
            fabricante = "amd" if familia == "ryzen" else "intel"
            # Miramos 25 caracteres hacia atrás para detectar AMD/Intel separado.
            previo = s[max(0,m.start()-25):m.start()]
            if familia == "ryzen" and "amd" in previo:
                fabricante = "amd"
            if familia == "corei" and "intel" in previo:
                fabricante = "intel"
            nivel = int(m.group(2))
            modelo = m.group(3) or ""
            texto_cpu = m.group(0).strip()
            score = 75
            if re.search(r"\bamd\b", previo) or re.search(r"\bintel\b", previo):
                score += 15
            if modelo:
                score += 10
            candidatos.append({
                "fabricante": fabricante,
                "familia": familia,
                "nivel": nivel,
                "modelo": modelo,
                "valor": texto_cpu,
                "score": score*peso_base,
                "fuente": "contexto_cpu",
                "contexto": _contexto(s, m.start(), m.end(), 45),
                "posicion": [m.start(), m.end()],
            })

    if not candidatos:
        return None
    return max(candidatos, key=lambda x: x["score"])


def parse_especificaciones_contextual(producto):
    """Extrae specs usando proximidad semántica y conserva evidencia."""
    bloques = _bloques_texto_producto(producto)
    ram_cands = []
    storage_cands = []
    cpu_cands = []

    for origen, texto, peso in bloques:
        r = _buscar_ram_contextual(texto, peso)
        if r:
            r["origen"] = origen
            ram_cands.append(r)
        st = _buscar_storage_contextual(texto, peso)
        if st:
            st["origen"] = origen
            storage_cands.append(st)
        cpu = _buscar_cpu_contextual(texto, peso)
        if cpu:
            cpu["origen"] = origen
            cpu_cands.append(cpu)

    ram = max(ram_cands, key=lambda x:x["score"]) if ram_cands else None
    storage = max(storage_cands, key=lambda x:x["score"]) if storage_cands else None
    cpu = max(cpu_cands, key=lambda x:x["score"]) if cpu_cands else None

    # Último recurso para nombres comerciales compactos: si hay dos
    # capacidades sin etiquetas, la menor suele ser RAM y la mayor storage.
    # Nunca se usa si ya existe evidencia contextual.
    if not ram or not storage:
        nombre = _normalizar_contexto(producto.get("producto"))
        caps = _capacidad_cercana(nombre, 0, len(nombre))
        if caps:
            ordenadas = sorted(caps, key=lambda x:x["gb"])
            if not ram:
                ram_posibles = [x for x in ordenadas if 4 <= x["gb"] <= 128]
                if ram_posibles:
                    x = ram_posibles[0]
                    ram = {"valor":x["gb"],"score":25,"fuente":"nombre_fallback","texto":x["texto"],"contexto":_contexto(nombre,x["inicio"],x["fin"]),"origen":"nombre"}
            if not storage:
                st_posibles = [x for x in ordenadas if x["gb"] >= 128 and (not ram or x["gb"] != ram["valor"])]
                if st_posibles:
                    x = st_posibles[-1]
                    storage = {"valor":x["gb"],"tipo":None,"score":20,"fuente":"nombre_fallback","texto":x["texto"],"contexto":_contexto(nombre,x["inicio"],x["fin"]),"origen":"nombre"}

    specs = {
        "ram_gb": int(ram["valor"]) if ram else None,
        "storage_gb": int(storage["valor"]) if storage else None,
        "storage_tipo": storage.get("tipo") if storage else None,
        "procesador": cpu["valor"] if cpu else None,
        "texto_original": _texto_limpio_contextual(producto.get("producto")),
        "evidencia": {
            "ram": ram,
            "storage": storage,
            "procesador": cpu,
        },
    }
    return specs


def parse_especificaciones(nombre):
    # Compatibilidad con llamadas antiguas: crea un producto mínimo.
    return parse_especificaciones_contextual({"producto": nombre})


def enriquecer_producto(p):
    specs = parse_especificaciones_contextual(p)
    p["especificaciones"] = specs
    precio = p.get("precio")
    descuento = p.get("descuento") or 0
    if precio:
        p["precio_por_gb_storage"] = round(precio/specs["storage_gb"],2) if specs.get("storage_gb") else None
        p["precio_por_gb_ram"] = round(precio/specs["ram_gb"],2) if specs.get("ram_gb") else None
    score = float(descuento)
    financiacion = p.get("financiacion") or []
    cuotas = [x.get("cuotas",0) for x in financiacion if x.get("sin_interes")]
    max_cuotas = max(cuotas, default=0)
    score += min(max_cuotas,24)*0.35
    if p.get("envio_gratis"): score += 5
    ram=specs.get("ram_gb") or 0
    storage=specs.get("storage_gb") or 0
    if ram>=16: score+=5
    elif ram>=8: score+=2
    if storage>=1024: score+=5
    elif storage>=512: score+=3
    elif storage>=256: score+=1
    if specs.get("storage_tipo") in ("ssd","nvme"): score+=2
    p["score_oferta"]=round(score,2)
    p["cuotas_sin_interes_max"]=max_cuotas
    return p


def extraer_ram_de_ficha(producto):
    specs = parse_especificaciones_contextual(producto)
    return specs.get("ram_gb")


def extraer_storage_de_ficha(producto):
    specs = parse_especificaciones_contextual(producto)
    return specs.get("storage_gb"), specs.get("storage_tipo")


def extraer_procesador_de_ficha(producto):
    specs = parse_especificaciones_contextual(producto)
    return specs.get("procesador") or _texto_limpio_contextual(producto.get("descripcion")) or _texto_limpio_contextual(producto.get("producto"))


def comparar_cpu(texto_cpu, criterio):
    """Comparación CPU basada en la familia/nivel localizado contextualmente."""
    c = _normalizar_contexto(criterio).replace(" ", "")
    m = re.fullmatch(r"(ryzen|corei)([3579])(\+)?", c)
    if not m:
        return c in _normalizar_contexto(texto_cpu)
    fam_req, nivel_req, minimo = m.group(1), int(m.group(2)), bool(m.group(3))
    encontrado = _buscar_cpu_contextual(texto_cpu)
    if not encontrado:
        return False
    if fam_req == "ryzen" and encontrado["familia"] != "ryzen": return False
    if fam_req == "corei" and encontrado["familia"] != "corei": return False
    return encontrado["nivel"] >= nivel_req if minimo else encontrado["nivel"] == nivel_req


def evaluar_subcriterio_simple(producto, criterio):
    """Motor de filtros V26: primero evidencia contextual, luego texto general."""
    criterio = criterio.strip().lower()
    if ":" in criterio:
        atributo, expresion = [x.strip() for x in criterio.split(":",1)]
        if atributo == "ryzen":
            cpu = extraer_procesador_de_ficha(producto)
            ok = comparar_cpu(cpu, "ryzen" + expresion)
            return ok, f"CPU contextual={cpu}"
        if atributo == "core":
            cpu = extraer_procesador_de_ficha(producto)
            ok = comparar_cpu(cpu, "corei" + expresion)
            return ok, f"CPU contextual={cpu}"
    if criterio.startswith("ram:"):
        actual = extraer_ram_de_ficha(producto)
        return criterio_capacidad(actual, criterio.split(":",1)[1]), f"RAM contextual={actual}GB evidencia={(producto.get('especificaciones') or {}).get('evidencia',{}).get('ram')}"
    if criterio.startswith("storage:"):
        actual, tipo = extraer_storage_de_ficha(producto)
        expr=criterio.split(":",1)[1]
        obj=parse_capacidad_criterio(expr)
        ok=actual is not None and obj is not None and (actual>=obj if expr.endswith("+") else actual==obj)
        return ok, f"storage contextual={actual}GB tipo={tipo}"
    if criterio.startswith(("ssd:","nvme:","hdd:","emmc:")):
        tipo_req, expr=criterio.split(":",1)
        actual,tipo=extraer_storage_de_ficha(producto)
        tipo_ok=(tipo==tipo_req) or (tipo_req=="ssd" and tipo=="nvme")
        if not expr: return tipo_ok, f"storage contextual={actual}GB tipo={tipo}"
        obj=parse_capacidad_criterio(expr)
        cap_ok=actual is not None and obj is not None and (actual>=obj if expr.endswith("+") else actual==obj)
        return tipo_ok and cap_ok, f"storage contextual={actual}GB tipo={tipo}"
    if re.fullmatch(r"(?:ryzen|corei)[3579]\+?", criterio):
        cpu=extraer_procesador_de_ficha(producto)
        return comparar_cpu(cpu,criterio),f"CPU contextual={cpu}"
    # Mantener el resto del motor original para precio, descuentos, GPU, etc.
    return _evaluar_subcriterio_simple_original(producto, criterio)


# Guardamos una referencia al motor original para conservar compatibilidad.
# Se asigna inmediatamente antes de sustituirlo.

def normalizar_consulta_interactiva(texto):
    """Hace tolerante el modo interactivo cuando el usuario omite '+' en
    criterios de atributo, por ejemplo: 'ryzen:7+ +ram:16+'.

    Si no existe una búsqueda base y todos los tokens no prefijados tienen la
    forma atributo:valor, esos tokens pasan a ser criterios positivos y el
    nombre del atributo se usa como búsqueda textual de BNA.
    """
    parsed = parse_criterio_encadenado(texto)
    if not parsed["base_query"]:
        return parsed

    tokens = parsed["base_query"].split()
    if tokens and all(":" in t and not t.startswith(("+", "-")) for t in tokens):
        parsed["incluidos"] = tokens + parsed["incluidos"]
        bases = []
        for t in tokens:
            atributo = t.split(":", 1)[0].strip()
            if atributo:
                bases.append(atributo)
        parsed["base_query"] = " ".join(bases).strip()

    # Texto libre queda intacto:
    #   "ryzen 7" -> búsqueda BNA "ryzen 7"
    #   "monitor 27 144hz" -> búsqueda BNA completa
    # Solo los tokens con sintaxis atributo:valor se convierten
    # automáticamente en criterios.
    return parsed



def seleccionar_categorias_catalogo(categorias):
    """Selecciona todas, varias categorías principales o una categoría/subcategoría."""
    if not categorias:
        return [(None, "Todas las categorías")]
    lista = list(categorias.items())
    print("\n" + "=" * 60)
    print("       SELECCIÓN DE CATEGORÍAS")
    print("=" * 60)
    print("  0) Todas las categorías")
    for i, (nombre, _) in enumerate(lista, 1):
        print(f"  {i}) {nombre}")
    print("\nPodés elegir varias categorías principales separadas por coma, por ejemplo: 1,3,5")
    while True:
        try:
            r = input("Categoría(s) [0=todas]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return [(None, "Todas las categorías")]
        if not r or r == "0":
            return [(None, "Todas las categorías")]
        partes = [x.strip() for x in r.split(",") if x.strip()]
        if partes and all(x.isdigit() and 1 <= int(x) <= len(lista) for x in partes):
            indices = list(dict.fromkeys(int(x) for x in partes))
            break
        print(f"Ingresá 0 o números entre 1 y {len(lista)}, separados por coma.")
    if len(indices) > 1:
        return [(info.get("href") or info.get("slug"), nombre) for nombre, info in (lista[i-1] for i in indices)]
    nombre, info = lista[indices[0]-1]
    sub = info.get("subcategorias") or {}
    if not sub:
        return [(info.get("href") or info.get("slug"), nombre)]
    print(f"\n{nombre.upper()} — 0) Toda la categoría")
    sublista = list(sub.items())
    for i, (subnombre, _) in enumerate(sublista, 1):
        print(f"  {i}) {subnombre}")
    while True:
        try:
            r2 = input("Subcategoría [0=toda la categoría]: ").strip() or "0"
        except (EOFError, KeyboardInterrupt):
            r2 = "0"
        if r2.isdigit() and 0 <= int(r2) <= len(sublista):
            break
        print(f"Ingresá un número entre 0 y {len(sublista)}.")
    if r2 == "0":
        return [(info.get("href") or info.get("slug"), nombre)]
    subnombre, subinfo = sublista[int(r2)-1]
    return [(subinfo.get("href") or subinfo.get("slug"), f"{nombre} / {subnombre}")]


def modo_catalogo_interactivo():
    """Construye el catálogo maestro mediante búsquedas generales interactivas.

    Cada búsqueda recorre todas las páginas y `ejecutar_scraper()` incorpora
    automáticamente los SKU encontrados a cache_bna/catalogo_bna.json.
    El catálogo es acumulativo: una búsqueda posterior no elimina productos
    encontrados anteriormente.
    """
    print("\n" + "=" * 60)
    print("       CONSTRUCCIÓN INTERACTIVA DEL CATÁLOGO BNA")
    print("=" * 60)
    print("Cada búsqueda recorrerá TODAS las páginas disponibles.")
    print("Los SKU encontrados se acumularán en:")
    print(f"  {CATALOGO_CACHE}")
    print("\nPara terminar, escriba SALIR cuando se pida una búsqueda.")

    while True:
        categoria_slug = None
        categoria_nombre = None

        # Permite seleccionar varias categorías principales en una sola consulta.
        # Para una única categoría se conserva también el selector de subcategoría.
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw_menu:
                browser_menu = pw_menu.chromium.launch(headless=True)
                page_menu = browser_menu.new_page(locale="es-AR")
                categorias = descubrir_categorias_bna(page_menu)
                seleccion = seleccionar_categorias_catalogo(categorias)
                browser_menu.close()
        except Exception as exc:
            print(f"[CATALOGO] No se pudo cargar el menú de categorías: {exc}")
            print("[CATALOGO] Continuando con búsqueda general.")
            seleccion = [(None, "Todas las categorías")]

        print("\n¿Qué búsqueda general quieres agregar al catálogo?")
        print("Ejemplos: computadora | notebook | monitor | celular | tv")
        try:
            consulta = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[CATALOGO] Finalizado por el usuario.")
            break

        if not consulta or consulta.lower() in {"salir", "exit", "q", "0"}:
            break

        parsed = normalizar_consulta_interactiva(consulta)
        if not parsed.get("base_query"):
            print("[CATALOGO] Búsqueda vacía. Intente nuevamente.")
            continue

        print("\n[CATALOGO] Iniciando búsqueda:")
        print(f"  categorías: {', '.join(nombre or 'Todas' for _, nombre in seleccion)}")
        print(f"  consulta  : {parsed['base_query']}")
        print("  páginas   : TODAS")
        print("  límite    : SIN LÍMITE")

        try:
            total_obtenidos = 0
            for categoria_slug, categoria_nombre in seleccion:
                print(f"\n[CATALOGO] Procesando categoría: {categoria_nombre or 'Todas las categorías'}")
                productos = ejecutar_scraper(
                    [consulta], 0, 0, "ofertas_bna_v20.json",
                    preguntar_paginas=False, obtener_fichas=False, max_fichas=0,
                    categoria_slug=categoria_slug, categoria_nombre=categoria_nombre,
                    modo_completo=True,
                )
                total_obtenidos += len(productos)
                print(f"[CATALOGO] Categoría terminada: {len(productos)} productos obtenidos.")
            print(f"[CATALOGO] Búsquedas terminadas. Productos obtenidos sumando categorías: {total_obtenidos}.")
            print(f"[CATALOGO] Catálogo persistente: {CATALOGO_CACHE}")
        except KeyboardInterrupt:
            print("\n[CATALOGO] Búsqueda interrumpida. El catálogo conserva lo ya guardado.")
        except Exception as exc:
            print(f"[CATALOGO] Error durante la búsqueda: {exc}")

        print("\n¿Quieres agregar otra búsqueda? [S/n]")
        try:
            seguir = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if seguir in {"n", "no", "salir", "q", "0"}:
            break

    catalogo = cargar_catalogo_bna()
    print("\n" + "=" * 60)
    print("             CATÁLOGO BNA FINAL")
    print("=" * 60)
    print(f"SKU acumulados: {len(catalogo)}")
    print(f"Archivo: {CATALOGO_CACHE}")
    print("=" * 60)


def main():
    ap = argparse.ArgumentParser(
        description=(
            "Busca productos en Tienda BNA con consultas encadenadas. "
            "Ejemplo: 'gamer +(ryzen7+|corei7+) +ram:32+ +ssd:512+ -notebook -tablet'."
        )
    )

    ap.add_argument(
        "queries",
        nargs="*",
        help="Consulta(s). Si se omite, se inicia el modo interactivo."
    )
    ap.add_argument("--max-precio", type=float)
    ap.add_argument("--min-descuento", type=float, default=0)
    ap.add_argument("--min-cuotas", type=int, default=0)
    ap.add_argument("--sin-interes", action="store_true")
    ap.add_argument("--envio-gratis", action="store_true")
    ap.add_argument("--min-ram", type=int)
    ap.add_argument("--min-storage", type=int)
    ap.add_argument(
        "--tipo-storage",
        choices=["ssd", "nvme", "hdd", "emmc", "desconocido"]
    )
    ap.add_argument("--max-productos", type=int, default=0,
                    help="Límite opcional de productos. 0 = sin límite (recomendado con filtros encadenados).")
    ap.add_argument(
        "--max-pages", type=int, default=0,
        help="Límite opcional de páginas. 0 = preguntar al ejecutar manualmente; todas en ejecución no interactiva."
    )
    ap.add_argument(
        "--no-preguntar-paginas", action="store_true",
        help="No preguntar cuántas páginas recorrer. Usa --max-pages o todas."
    )
    ap.add_argument(
        "--actualizar-catalogo", action="store_true",
        help="Inicia la construcción interactiva y acumulativa del catálogo maestro."
    )
    ap.add_argument(
        "--catalogo-interactivo", action="store_true",
        help="Alias de --actualizar-catalogo: construye el catálogo mediante búsquedas interactivas."
    )
    ap.add_argument(
        "--ficha-tecnica",
        action="store_true",
        help="Abrir productos y extraer la Ficha Técnica ANTES de evaluar los subcriterios."
    )
    ap.add_argument(
        "--max-fichas",
        type=int,
        default=0,
        help="Máximo acumulado de fichas técnicas nuevas a consultar. 0 = sin límite."
    )
    ap.add_argument(
        "--completo",
        action="store_true",
        help="Recorre todas las páginas y procesa automáticamente todos los SKUs nuevos sin preguntar por página."
    )
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--salida", default="ofertas_bna_v20.json")

    args = ap.parse_args()

    # El modo de catálogo es explícitamente interactivo y no debe entrar
    # primero al menú de una búsqueda normal.
    if args.actualizar_catalogo or args.catalogo_interactivo:
        modo_catalogo_interactivo()
        return

    modo_interactivo = not args.queries and sys.stdin.isatty()

    categoria_slug = None
    categoria_nombre = None

    if modo_interactivo:
        print("\n" + "=" * 60)
        print("       SCRAPER DE PRODUCTOS - TIENDA BNA")
        print("=" * 60)

        # Descubrimos las categorías al iniciar. Solo mostramos un nivel.
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw_menu:
            browser_menu = pw_menu.chromium.launch(headless=True)
            page_menu = browser_menu.new_page(locale="es-AR")
            categorias = descubrir_categorias_bna(page_menu)
            categoria_slug, categoria_nombre = seleccionar_categoria_bna(categorias)

        print("\nLENGUAJE DE BÚSQUEDA")
        print("  +texto              Criterio positivo; basta cumplir 1 y se ordena por coincidencias")
        print("  -texto              Debe excluir")
        print("  +atributo:valor     Buscar atributo y valor")
        print("  +atributo:valor+    Valor mínimo")
        print("  +atributo:=valor    Valor exacto")
        print("  +atributo:<valor    Menor que")
        print("  +atributo:>valor    Mayor que")
        print("  +(A|B)              A O B")
        print("  -(A|B)              Excluir A O B")
        print("\nEjemplos:")
        print("  computadora +ryzen:7+ +ram:32+")
        print("  También acepta: ryzen:7+ +ram:16+ -notebook")
        print("  notebook +core:7+ +ssd:512+ -refurbished")
        print("  monitor +27 +144hz")
        print("\n¿Qué quieres buscar?")
        consulta_interactiva = input("> ").strip()
        if not consulta_interactiva:
            ap.error("Debe introducir una búsqueda.")
        args.queries = [consulta_interactiva]

    # Una consulta puede llegar separada por argumentos si el usuario olvidó
    # las comillas. La recomponemos para hacer el CLI más tolerante.
    consulta_completa = " ".join(args.queries).strip()
    parsed = normalizar_consulta_interactiva(consulta_completa)

    if not parsed["base_query"]:
        ap.error("Debe existir una búsqueda base antes de los criterios +.../-...")

    # Conservamos la consulta completa para que ejecutar_scraper pueda
    # aplicar los criterios +.../-... después de descargar la búsqueda base.
    base_queries = [consulta_completa]

    print("\n[BNA] Consulta encadenada:")
    print(f"      búsqueda BNA : {parsed['base_query']}")
    if parsed["incluidos"]:
        print(f"      incluir      : {', '.join('+' + x for x in parsed['incluidos'])}")
    if parsed["excluidos"]:
        print(f"      excluir      : {', '.join('-' + x for x in parsed['excluidos'])}")

    # Si la consulta contiene criterios técnicos (+ryzen, +ram, +storage,
    # +ssd, +gpu, +cpu, etc.), la Ficha Técnica debe activarse
    # automáticamente. No obligamos al usuario a recordar --ficha-tecnica.
    criterios_tecnicos_cli = any(
        any(
            alternativa.strip().lower().startswith((
                "ram:", "storage:", "ssd:", "nvme:", "hdd:", "emmc:",
                "gpu:", "placa:", "video:", "procesador:", "cpu:",
                "ryzen:", "core:"
            ))
            for alternativa in (separar_grupo_or(c) or [c])
        )
        for c in parsed["incluidos"]
    )

    fichas_auto = criterios_tecnicos_cli
    if fichas_auto and not (args.ficha_tecnica or modo_interactivo):
        print("[BNA] Filtro técnico detectado → Ficha Técnica activada automáticamente.")

    productos = ejecutar_scraper(
        base_queries,
        args.max_pages,
        args.max_productos,
        args.salida,
        preguntar_paginas=not args.no_preguntar_paginas,
        obtener_fichas=(args.ficha_tecnica or modo_interactivo or fichas_auto),
        max_fichas=args.max_fichas,
        categoria_slug=categoria_slug,
        categoria_nombre=categoria_nombre,
        modo_completo=args.completo,
    )

    # Como ejecutar_scraper trabaja con la búsqueda base, todas las salidas
    # aquí corresponden a esa consulta.
    filtrados = []

    for p in productos:
        precio = p.get("precio")
        descuento = p.get("descuento") or 0
        specs = p.get("especificaciones") or {}

        # Filtros CLI tradicionales.
        if args.max_precio is not None and (
            precio is None or precio > args.max_precio
        ):
            continue

        if descuento < args.min_descuento:
            continue

        cuotas = p.get("cuotas_sin_interes_max", 0)
        if cuotas < args.min_cuotas:
            continue
        if args.sin_interes and cuotas <= 0:
            continue
        if args.envio_gratis and not p.get("envio_gratis"):
            continue

        if args.min_ram is not None and (
            specs.get("ram_gb") is None
            or specs["ram_gb"] < args.min_ram
        ):
            continue

        if args.min_storage is not None and (
            specs.get("storage_gb") is None
            or specs["storage_gb"] < args.min_storage
        ):
            continue

        if args.tipo_storage:
            tipo = specs.get("storage_tipo") or "desconocido"
            if tipo != args.tipo_storage:
                continue

        # Los + son criterios POSITIVOS de puntuación:
        # basta cumplir al menos UNO. Luego se ordena por cantidad de
        # criterios cumplidos y por porcentaje de coincidencia.
        if parsed["incluidos"]:
            ranking = puntuar_producto(p, parsed["incluidos"])
            if ranking["coincidencias"] < 1:
                continue
            p["ranking_criterios"] = ranking
            p["score_coincidencia"] = ranking["coincidencias"]
            p["porcentaje_coincidencia"] = ranking["porcentaje_coincidencia"]

        # Los - son exclusiones duras y tienen prioridad sobre los +.
        if parsed["excluidos"]:
            if any(
                evaluar_subcriterio(p, ex)[0]
                for ex in parsed["excluidos"]
            ):
                continue

        filtrados.append(p)

    if parsed["incluidos"]:
        filtrados.sort(
            key=lambda p: (
                -(p.get("score_coincidencia") or 0),
                -(p.get("porcentaje_coincidencia") or 0),
                -(p.get("score_oferta") or 0),
                p.get("precio") or float("inf")
            )
        )
    else:
        filtrados.sort(
            key=lambda p: (
                -(p.get("score_oferta") or 0),
                p.get("precio") or float("inf")
            )
        )

    resultado = {
        "criterios": {
            "consulta_original": consulta_completa,
            "busqueda_base": parsed["base_query"],
            "incluidos": parsed["incluidos"],
            "excluidos": parsed["excluidos"],
            "max_precio": args.max_precio,
            "min_descuento": args.min_descuento,
            "min_cuotas": args.min_cuotas,
            "sin_interes": args.sin_interes,
            "envio_gratis": args.envio_gratis,
            "min_ram": args.min_ram,
            "min_storage": args.min_storage,
            "tipo_storage": args.tipo_storage,
            "ficha_tecnica": args.ficha_tecnica,
            "max_fichas": args.max_fichas,
            "categoria_slug": categoria_slug,
            "categoria_nombre": categoria_nombre,
        },
        "total_encontrados": len(productos),
        "total_filtrados": len(filtrados),
        "productos": filtrados,
    }

    salida = Path(args.salida)
    if not salida.is_absolute():
        salida = BASE / salida

    salida.write_text(
        json.dumps(resultado, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    # También generamos un archivo específico para consultas encadenadas.
    if parsed["incluidos"] or parsed["excluidos"]:
        nombre_base = re.sub(
            r"[^a-zA-Z0-9áéíóúüñÁÉÍÓÚÜÑ_-]+",
            "_",
            parsed["base_query"].strip()
        )
        nombre_base = re.sub(r"_+", "_", nombre_base).strip("_")
        salida_filtrada = BASE / f"resultados_{nombre_base}_filtrado.json"
        salida_filtrada.write_text(
            json.dumps(filtrados, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    else:
        salida_filtrada = None

    print("\n" + "=" * 80)
    print(f"Encontrados en búsqueda base: {len(productos)}")
    print(f"Cumplen criterios:            {len(filtrados)}")
    sku_repetidos = sum(1 for p in filtrados if (p.get("cantidad_apariciones_sku") or 1) > 1)
    print(f"SKU con múltiples apariciones: {sku_repetidos}")
    print(f"Archivo completo:              {salida}")
    if salida_filtrada:
        print(f"Archivo filtrado:              {salida_filtrada}")
    print("=" * 80)

    # OJO: el TOP se toma DESPUÉS de ordenar todos los resultados.
    # Así nunca mostramos simplemente los primeros productos encontrados.
    resultados_ordenados = sorted(
        filtrados,
        key=lambda p: (
            p.get("score_coincidencia", 0),
            p.get("score_oferta", 0),
            p.get("descuento", 0) or 0,
        ),
        reverse=True,
    )

    for i, p in enumerate(resultados_ordenados[:args.top], 1):
        s = p.get("especificaciones") or {}
        precio = p.get("precio")
        precio_txt = f"${precio:,.0f}" if precio is not None else "?"
        ranking = p.get("ranking_criterios") or {}
        cumplidos = ranking.get("criterios_cumplidos") or []
        no_cumplidos = ranking.get("criterios_no_cumplidos") or []
        total = ranking.get("total_subcriterios", len(parsed.get("incluidos") or []))
        coincidencias = ranking.get("coincidencias", p.get("score_coincidencia", 0))

        print(
            f"{i:2}. [{coincidencias}/{total}] {p.get('producto')}\n"
            f"    SKU: {p.get('sku')} | "
            f"{precio_txt} | "
            f"{p.get('descuento') or 0:g}% OFF | "
            f"RAM {s.get('ram_gb') or '?'} GB | "
            f"Storage {s.get('storage_gb') or '?'} GB "
            f"{s.get('storage_tipo') or ''} | "
            f"Score oferta {p.get('score_oferta')}\n"
            f"    ✓ {', '.join(cumplidos) if cumplidos else 'ninguno'}\n"
            f"    ✗ {', '.join(no_cumplidos) if no_cumplidos else 'ninguno'}\n"
        )


if __name__ == "__main__":
    main()
