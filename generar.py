"""
Genera la lista de artistas musicales de App Música a partir de Wikidata.

Reglas (DECISIONES.md, D14):
  - Solo músicos reales (personas con profesión musical cuya descripción indica que se
    dedican a la música) y grupos o bandas musicales.
  - Solo artistas con al menos un artículo en Wikipedia.
  - Para cada artista se guardan sus nombres (nombre y nombres conocidos / alias en
    español, inglés y multilingüe), que son los únicos textos en los que se busca.

Salida (carpeta datos/):
  - artistas.tsv.gz  una línea por artista (ver CABECERA)
  - meta.json        fecha, número de artistas y tamaño
  - informe.md       informe de calidad, con los casos de prueba de D14

Uso:  python generar.py            (lista completa)
      python generar.py --prueba   (solo unas pocas profesiones, para probar)
"""
import gzip
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

UA = "AppMusica-datos/0.1 (https://github.com/Torpaz95/appmusica-datos)"
ENDPOINT = "https://query.wikidata.org/sparql"
SALIDA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "datos")
LOTE = 400  # artistas por consulta de detalles
CABECERA = ["id", "tipo", "fama", "nombre", "alias", "anio", "pais"]

MUSICO = "Q639669"   # músico
GRUPO = "Q215380"    # grupo musical
HUMANO = "Q5"

# Palabras que, en la descripción (español o inglés), indican que la persona se dedica a la
# música. Se comparan sin tildes y en minúsculas, al principio de una palabra.
PALABRAS_MUSICA = """
music musico musica cantant cantaor cantaora cantautor singer songwriter vocalist
compos rapper rapero rapera trapero trapera mc dj disc jockey
guitar bass bajista drummer bateria percusion pianist piano organist organista
violin viol cellist chelista violonchel saxofon saxophon trumpet trompet trombon
clarinet flaut flute oboe fagot bassoon harp arpista accordion acordeon banjo mandolin
ukulele sitar tabla keyboard teclista conductor director de orquesta orchestra orquesta
producer productor beatmaker opera tenor soprano baritone baritono mezzo contralto
countertenor contratenor bass-baritone jazz rock pop punk metal hip hop reggae reggaeton
folk blues soul funk gospel country flamenco salsa bachata tango fado k-pop j-pop
idol band banda grupo duo trio cuarteto quartet choir coro chorister corista
lyricist letrista arranger arreglista instrumentalist instrumentista multi-instrumentalist
multiinstrumentista bandleader frontman frontwoman troubadour trovador bard chansonnier
yodel beatbox turntablist electronic electronica edm techno house trance
""".split()


def sin_tildes(s):
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


_RE_MUSICA = re.compile(r"(?<![a-z0-9])(" + "|".join(
    re.escape(p) for p in sorted(set(PALABRAS_MUSICA), key=len, reverse=True)) + ")")


def descripcion_musical(desc):
    return bool(desc) and bool(_RE_MUSICA.search(sin_tildes(desc)))


# ---------------------------------------------------------------- consultas a Wikidata

def sparql(q, intentos=6):
    datos = urllib.parse.urlencode({"query": q}).encode()
    espera = 5
    for i in range(intentos):
        req = urllib.request.Request(ENDPOINT, data=datos, headers={
            "User-Agent": UA, "Accept": "application/sparql-results+json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)["results"]["bindings"]
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < intentos - 1:
                pausa = int(e.headers.get("Retry-After") or espera)
                time.sleep(min(pausa, 120))
                espera *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if i < intentos - 1:
                time.sleep(espera)
                espera *= 2
                continue
            raise


def qid(v):
    return v.rsplit("/", 1)[1]


def subclases(raiz):
    b = sparql(f"SELECT DISTINCT ?x WHERE {{ ?x wdt:P279* wd:{raiz} }}")
    return sorted({qid(x["x"]["value"]) for x in b})


def candidatos(propiedad, valor, extra=""):
    """Entidades con propiedad=valor y al menos un enlace a algún proyecto Wikimedia.
    Si la consulta es demasiado grande, se parte por número de enlaces."""
    base = f"?p wdt:{propiedad} wd:{valor}; wikibase:sitelinks ?s. {extra}"
    try:
        b = sparql(f"SELECT ?p WHERE {{ {base} FILTER(?s > 0) }}", intentos=2)
        return {qid(x["p"]["value"]) for x in b}
    except Exception:
        out = set()
        for lo, hi in [(1, 1), (2, 2), (3, 4), (5, 9), (10, 10**6)]:
            b = sparql(f"SELECT ?p WHERE {{ {base} FILTER(?s >= {lo} && ?s <= {hi}) }}")
            out |= {qid(x["p"]["value"]) for x in b}
        return out


def detalles(ids):
    vals = " ".join("wd:" + i for i in ids)
    d = {i: dict(nombres={}, alias=set(), desc={}, wp=0, tipos=set(), anio=None, pais=None)
         for i in ids}

    # Número de Wikipedias con artículo (las webs *.wikipedia.org/ entre sus enlaces).
    for x in sparql(f"""SELECT ?i ?w WHERE {{ VALUES ?i {{ {vals} }}
        ?a schema:about ?i; schema:isPartOf ?w }}"""):
        if x["w"]["value"].endswith(".wikipedia.org/"):
            d[qid(x["i"]["value"])]["wp"] += 1

    vivos = [i for i in ids if d[i]["wp"] > 0]
    if not vivos:
        return d
    vals = " ".join("wd:" + i for i in vivos)

    for x in sparql(f"""SELECT ?i ?l ?k WHERE {{ VALUES ?i {{ {vals} }}
        {{ ?i rdfs:label ?l BIND("n" AS ?k) }} UNION {{ ?i skos:altLabel ?l BIND("a" AS ?k) }}
        UNION {{ ?i schema:description ?l BIND("d" AS ?k) }}
        FILTER(LANG(?l) IN ("es", "en", "mul")) }}"""):
        e = d[qid(x["i"]["value"])]
        texto, lengua, k = x["l"]["value"], x["l"].get("xml:lang", ""), x["k"]["value"]
        if k == "n":
            e["nombres"][lengua] = texto
        elif k == "a":
            e["alias"].add(texto)
        elif lengua in ("es", "en"):
            e["desc"][lengua] = texto

    for x in sparql(f"""SELECT ?i ?t WHERE {{ VALUES ?i {{ {vals} }} ?i wdt:P31 ?t }}"""):
        d[qid(x["i"]["value"])]["tipos"].add(qid(x["t"]["value"]))

    for x in sparql(f"""SELECT ?i (MIN(YEAR(?f)) AS ?a) WHERE {{ VALUES ?i {{ {vals} }}
        ?i wdt:P569|wdt:P571|wdt:P2031 ?f }} GROUP BY ?i"""):
        if "a" in x:
            d[qid(x["i"]["value"])]["anio"] = int(x["a"]["value"])

    for x in sparql(f"""SELECT ?i (SAMPLE(?c) AS ?p) WHERE {{ VALUES ?i {{ {vals} }}
        ?i wdt:P495|wdt:P27 ?pa. ?pa wdt:P297 ?c }} GROUP BY ?i"""):
        if "p" in x:
            d[qid(x["i"]["value"])]["pais"] = x["p"]["value"]
    return d


# ---------------------------------------------------------------- búsqueda (réplica de D14)

ARTICULOS = {"the", "a", "an", "el", "la", "los", "las", "le", "les", "l", "il", "i", "lo",
             "gli", "o", "os", "as", "die", "der", "das", "de", "het", "en"}


def palabras(texto):
    return [w for w in re.split(r"[^a-z0-9]+", sin_tildes(texto)) if w]


def coincidencia(consulta, nombres):
    """0 = algún nombre empieza por lo escrito; 1 = lo escrito es el principio de otra
    palabra; None = no coincide. El artículo inicial del nombre se puede omitir."""
    q = palabras(consulta)
    if not q or len("".join(q)) < 2:
        return None
    mejor = None
    for n in nombres:
        w = palabras(n)
        for inicio in range(len(w)):
            trozo = w[inicio:inicio + len(q)]
            if len(trozo) < len(q):
                break
            if all(t == p for t, p in zip(trozo[:-1], q[:-1])) and trozo[-1].startswith(q[-1]):
                nivel = 0 if inicio == 0 or (inicio == 1 and w[0] in ARTICULOS) else 1
                mejor = nivel if mejor is None else min(mejor, nivel)
                if mejor == 0:
                    return 0
    return mejor


def buscar(consulta, artistas, n=10):
    res = []
    for a in artistas:
        c = coincidencia(consulta, [a["nombre"]] + a["alias"])
        if c is not None:
            res.append((c, -a["fama"], a["nombre"], a))
    res.sort(key=lambda r: r[:3])
    return [r[3] for r in res[:n]]


# (texto, deben salir entre los N primeros, no deben salir, N)
CASOS = [
    ("cyrus", ["Miley Cyrus", "Billy Ray Cyrus"], [], 10),
    ("brown", ["Chris Brown"], [], 10),
    ("moz", ["Wolfgang Amadeus Mozart"], [], 5),
    ("beeth", ["Ludwig van Beethoven"], [], 3),
    ("boss", ["Bruce Springsteen"], [], 10),
    ("beatles", ["The Beatles"], [], 1),
    ("rolling", ["The Rolling Stones"], [], 3),
    ("stones", ["The Rolling Stones"], [], 10),
    ("rosal", ["Rosalía"], [], 3),
    ("bring me", ["Bring Me the Horizon"], [], 3),
    ("metal", ["Metallica"], [], 10),
    ("los del r", ["Los del Río"], [], 3),
    ("camaron", ["Camarón de la Isla"], [], 3),
    ("bad bun", ["Bad Bunny"], [], 3),
    ("br", [], ["Cabrera"], 10**9),
    ("depp", [], ["Johnny Depp"], 10**9),
    ("scarlett", [], ["Scarlett Johansson"], 10**9),
    ("hannah mont", [], ["Hannah Montana"], 10**9),
]


def casos_de_prueba(artistas):
    filas = []
    for texto, si, no, top in CASOS:
        res = buscar(texto, artistas, n=min(top, 10**6))
        nombres = [sin_tildes(a["nombre"]) for a in res]
        fallos = [f"falta «{s}»" for s in si if sin_tildes(s) not in nombres[:top]]
        fallos += [f"aparece «{x}»" for x in no if any(sin_tildes(x) in n for n in nombres)]
        primeros = ", ".join(a["nombre"] for a in res[:5])
        filas.append((texto, not fallos, "; ".join(fallos), primeros))
    return filas


# ---------------------------------------------------------------- programa principal

def main():
    prueba = "--prueba" in sys.argv
    t0 = time.time()
    os.makedirs(SALIDA, exist_ok=True)

    profesiones = subclases(MUSICO)
    tipos_grupo = subclases(GRUPO)
    grupo_set = set(tipos_grupo)
    print(f"{len(profesiones)} profesiones musicales, {len(tipos_grupo)} tipos de grupo", flush=True)
    if prueba:
        profesiones = ["Q855091", "Q1075651"]  # guitarrista, cantaor
        tipos_grupo = ["Q5741069"]            # grupo de rock

    personas, grupos = set(), set()
    for i, o in enumerate(profesiones, 1):
        personas |= candidatos("P106", o, f"?p wdt:P31 wd:{HUMANO}.")
        if i % 50 == 0:
            print(f"  profesiones {i}/{len(profesiones)}: {len(personas)} personas", flush=True)
    for t in tipos_grupo:
        grupos |= candidatos("P31", t)
    grupos -= personas
    print(f"Candidatos: {len(personas)} personas, {len(grupos)} grupos", flush=True)

    ids = sorted(personas | grupos, key=lambda x: int(x[1:]))
    # Detalles por lotes, 3 consultas a la vez (Wikidata permite hasta 5 en paralelo).
    info = {}
    lotes = [ids[i:i + LOTE] for i in range(0, len(ids), LOTE)]
    with ThreadPoolExecutor(max_workers=3) as ex:
        for n, parte in enumerate(ex.map(detalles, lotes), 1):
            info.update(parte)
            if n % 25 == 0 or n == len(lotes):
                print(f"  detalles: lote {n}/{len(lotes)} ({(time.time() - t0) / 60:.0f} min)", flush=True)

    motivos = Counter()
    artistas = []
    for i in ids:
        e = info[i]
        if e["wp"] == 0:
            motivos["sin artículo en Wikipedia"] += 1
            continue
        nombre = e["nombres"].get("es") or e["nombres"].get("en") or e["nombres"].get("mul")
        if not nombre:
            motivos["sin nombre en español, inglés ni multilingüe"] += 1
            continue
        if i in personas:
            if HUMANO not in e["tipos"]:
                motivos["no es una persona real"] += 1
                continue
            desc = e["desc"].get("es") or e["desc"].get("en") or ""
            if not (descripcion_musical(e["desc"].get("es")) or descripcion_musical(e["desc"].get("en"))):
                motivos["persona sin descripción" if not desc else "descripción no musical"] += 1
                continue
            tipo = "p"
        else:
            if not (e["tipos"] & grupo_set):
                motivos["ya no figura como grupo musical"] += 1
                continue
            tipo = "g"
        alias = sorted({a for a in set(e["nombres"].values()) | e["alias"] if a != nombre})
        artistas.append(dict(id=i, tipo=tipo, fama=e["wp"], nombre=nombre, alias=alias,
                             anio=e["anio"], pais=e["pais"]))

    artistas.sort(key=lambda a: (-a["fama"], a["nombre"]))
    ruta = os.path.join(SALIDA, "artistas.tsv.gz")
    limpio = lambda s: s.replace("\t", " ").replace("\n", " ").replace("|", "/")
    with gzip.open(ruta, "wt", encoding="utf-8", compresslevel=9) as f:
        f.write("\t".join(CABECERA) + "\n")
        for a in artistas:
            f.write("\t".join([a["id"], a["tipo"], str(a["fama"]), limpio(a["nombre"]),
                               "|".join(limpio(x) for x in a["alias"]),
                               str(a["anio"] or ""), a["pais"] or ""]) + "\n")
    tam = os.path.getsize(ruta)

    n_p = sum(a["tipo"] == "p" for a in artistas)
    n_g = len(artistas) - n_p
    fecha = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    json.dump(dict(fecha=fecha, artistas=len(artistas), personas=n_p, grupos=n_g,
                   bytes=tam, prueba=prueba), open(os.path.join(SALIDA, "meta.json"), "w"),
              ensure_ascii=False, indent=1)

    num = lambda n: f"{n:,}".replace(",", ".")
    pct = lambda n: f"{100 * n / max(len(artistas), 1):.1f} %"
    sin_anio = sum(a["anio"] is None for a in artistas)
    sin_pais = sum(a["pais"] is None for a in artistas)
    casos = casos_de_prueba(artistas)
    L = [f"# Informe de calidad — {fecha}", ""]
    if prueba:
        L += ["**Ejecución de prueba (solo algunas profesiones). No es la lista real.**", ""]
    L += [f"- Artistas en la lista: **{num(len(artistas))}** ({num(n_p)} personas, {num(n_g)} grupos)",
          f"- Tamaño del archivo: **{tam / 1e6:.1f} MB**".replace(".", ","),
          f"- Sin año: {num(sin_anio)} ({pct(sin_anio)})",
          f"- Sin país: {num(sin_pais)} ({pct(sin_pais)})",
          f"- Tiempo de generación: {(time.time() - t0) / 60:.0f} min", "",
          "## Descartados", ""]
    L += [f"- {m}: {num(n)}" for m, n in motivos.most_common()]
    L += ["", "## Casos de prueba de las reglas de búsqueda (D14)", "",
          "| Búsqueda | Resultado | Fallos | Primeros resultados |", "|---|---|---|---|"]
    L += [f"| {t} | {'✅' if ok else '❌'} | {fa} | {pr} |" for t, ok, fa, pr in casos]
    ok = sum(c[1] for c in casos)
    L += ["", f"**{ok} de {len(casos)} casos correctos.**", ""]
    open(os.path.join(SALIDA, "informe.md"), "w").write("\n".join(L))
    print("\n".join(L))


if __name__ == "__main__":
    main()
