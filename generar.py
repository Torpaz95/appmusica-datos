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
CABECERA = ["id", "tipo", "fama", "nombre", "alias", "anio", "pais", "papel"]

# Papel principal que muestra la app («Cantante», «Guitarrista»...), por orden de prioridad.
# Debe coincidir con PERSON_ROLES y GROUP_ROLES de la app (WikidataRepository.kt).
PAPELES_PERSONA = ["Q177220", "Q488205", "Q2252262", "Q130857", "Q158852", "Q36834", "Q486748",
                   "Q855091", "Q584301", "Q386854", "Q183945", "Q753110", "Q806349", "Q639669"]
PAPELES_GRUPO = ["Q9212979"]
MUJER = {"Q6581072", "Q1052281"}

MUSICO = "Q639669"   # músico
GRUPO = "Q215380"    # grupo musical
HUMANO = "Q5"

# Profesiones que, en la descripción (español o inglés), indican dedicación a la música. Se
# comparan sin tildes y en minúsculas, al principio de una palabra («compos» vale para
# «compositor», «composer», «compositora»...). Las de varias palabras van unidas con «_».
PALABRAS_MUSICA = """
music musico musica cantant cantaor cantaora cantautor singer songwriter vocalist
compos rapper rapero rapera trapero trapera mc dj disc_jockey deejay
guitar bajista bassist drummer baterista percusionista percussionist pianist organist
organista violin viola cellist chelista violonchel saxofon saxophon trumpet trompet
trombon clarinet flaut flute oboe fagot bassoon harpist arpista accordion acordeon
banjo mandolin ukulele sitar keyboardist teclista conductor director_de_orquesta
director_musical musical_director bandleader director_de_banda productor_musical
music_producer record_producer beatmaker opera tenor soprano baritone baritono
mezzosoprano mezzo-soprano contralto countertenor contratenor bass-baritone
jazz rock pop punk metal hip_hop reggae reggaeton folk blues soul funk gospel country
flamenco salsa bachata tango fado k-pop j-pop idol band banda grupo_musical
duo_musical trio choir coro chorister corista lyricist letrista arranger arreglista
instrumentalist instrumentista multi-instrumentalist multiinstrumentista frontman
frontwoman troubadour trovador chansonnier yodel beatbox turntablist
""".split()

# Profesiones no musicales. Si una de ellas aparece en la descripción ANTES que cualquier
# profesión musical («actriz, cantante y modelo»), se entiende que la persona no se dedica
# principalmente a la música (D14: «Cantante…» sí, «Actriz…» no).
PALABRAS_OTRAS = """
actor actriz actress modelo model presentador presentadora presenter host tv_host
television_personality personalidad_de_television youtuber influencer streamer tiktoker
comedian comediante humorista escritor escritora writer novelist novelista poet poeta
poetisa playwright dramaturgo dramaturga painter pintor pintora sculptor escultor
escultora photographer fotografo fotografa politician politico politica journalist
periodista film_director director_de_cine filmmaker cineasta screenwriter guionista
dancer bailarin bailarina choreographer coreografo coreografa athlete atleta
footballer futbolista wrestler luchador boxer boxeador businessman empresario
empresaria entrepreneur emprendedor socialite philosopher filosofo filosofa scientist
cientifico cientifica physician medico medica lawyer abogado abogada priest sacerdote
monk monje theologian teologo nun monja king rey queen reina prince principe
princess princesa emperor emperador soldier soldado military militar architect
arquitecto arquitecta voice_actor voice_actress actor_de_voz actriz_de_voz
pornographic_actress actriz_pornografica
""".split()


def sin_tildes(s):
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _regex(lista):
    trozos = sorted({re.escape(p).replace("_", r"\s+") for p in lista}, key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(" + "|".join(trozos) + ")")


_RE_MUSICA = _regex(PALABRAS_MUSICA)
_RE_OTRAS = _regex(PALABRAS_OTRAS)


def descripcion_musical(desc):
    """True si la descripción presenta a la persona, en primer lugar, como dedicada a la
    música: aparece una profesión musical y ninguna otra profesión va antes que ella."""
    if not desc:
        return False
    d = sin_tildes(desc)
    m = _RE_MUSICA.search(d)
    if not m:
        return False
    o = _RE_OTRAS.search(d)
    return o is None or m.start() < o.start()


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
    d = {i: dict(nombres={}, alias=set(), desc={}, wp=0, tipos=set(), anio=None, pais=None,
                 ocup=set(), titulos={})
         for i in ids}

    # Número de Wikipedias con artículo (las webs *.wikipedia.org/ entre sus enlaces).
    # De paso se guarda el título del artículo en español e inglés: es el nombre con el que se
    # conoce al artista (por ejemplo, «Miley Cyrus», aunque en Wikidata figure como «Miley»).
    for x in sparql(f"""SELECT ?i ?w ?t WHERE {{ VALUES ?i {{ {vals} }}
        ?a schema:about ?i; schema:isPartOf ?w; schema:name ?t }}"""):
        w = x["w"]["value"]
        if w.endswith(".wikipedia.org/"):
            e = d[qid(x["i"]["value"])]
            e["wp"] += 1
            if w in ("https://es.wikipedia.org/", "https://en.wikipedia.org/"):
                titulo = re.sub(r"\s*\([^)]*\)\s*$", "", x["t"]["value"]).strip()
                if titulo:
                    e["titulos"][w[8:10]] = titulo

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

    # Profesiones y sexo (para el papel: «Cantante» / «Cantante», «Compositor» / «Compositora»).
    for x in sparql(f"""SELECT ?i ?o WHERE {{ VALUES ?i {{ {vals} }} ?i wdt:P106|wdt:P21 ?o }}"""):
        d[qid(x["i"]["value"])]["ocup"].add(qid(x["o"]["value"]))

    for x in sparql(f"""SELECT ?i (MIN(YEAR(?f)) AS ?a) WHERE {{ VALUES ?i {{ {vals} }}
        ?i wdt:P569|wdt:P571|wdt:P2031 ?f }} GROUP BY ?i"""):
        if "a" in x:
            d[qid(x["i"]["value"])]["anio"] = int(x["a"]["value"])

    # País: de origen (grupos) o nacionalidad (personas), sin las que tienen fecha de fin. Si una
    # persona tiene varias nacionalidades, se elige la del país donde nació; si no, la marcada
    # como preferida en Wikidata.
    paises = {i: {} for i in vivos}
    for x in sparql(f"""SELECT ?i ?c ?r WHERE {{ VALUES ?i {{ {vals} }}
        ?i p:P495|p:P27 ?st. ?st ps:P495|ps:P27 ?pa; wikibase:rank ?r. ?pa wdt:P297 ?c.
        FILTER NOT EXISTS {{ ?st pq:P582 ?fin }} FILTER(?r != wikibase:DeprecatedRank) }}"""):
        c, pref = x["c"]["value"], x["r"]["value"].endswith("PreferredRank")
        paises[qid(x["i"]["value"])][c] = paises[qid(x["i"]["value"])].get(c, False) or pref
    nacio = {}
    for x in sparql(f"""SELECT ?i ?c WHERE {{ VALUES ?i {{ {vals} }}
        ?i wdt:P19 ?lugar. ?lugar wdt:P17 ?pa. ?pa wdt:P297 ?c }}"""):
        nacio.setdefault(qid(x["i"]["value"]), set()).add(x["c"]["value"])
    for i, opciones in paises.items():
        if not opciones:
            continue
        en_nacimiento = sorted(c for c in opciones if c in nacio.get(i, ()))
        preferidas = sorted(c for c, pref in opciones.items() if pref)
        d[i]["pais"] = (en_nacimiento or preferidas or sorted(opciones))[0]
    return d


# ---------------------------------------------------------------- búsqueda (réplica de D14)

ARTICULOS = {"the", "a", "an", "el", "la", "los", "las", "le", "les", "l", "il", "i", "lo",
             "gli", "o", "os", "as", "die", "der", "das", "de", "het", "en"}


def palabras(texto):
    return [w for w in re.split(r"[\W_]+", sin_tildes(texto)) if w]


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


# Orden (D14, regla 3, revisada el 26/11/2026): empezar por lo escrito da ventaja (la fama
# cuenta el triple), pero un artista mucho más famoso puede adelantar a uno poco conocido.
VENTAJA_EMPIEZA = 3


def buscar(consulta, artistas, n=10):
    res = []
    for a in artistas:
        c = coincidencia(consulta, [a["nombre"]] + a["alias"])
        if c is not None:
            puntos = a["fama"] * (VENTAJA_EMPIEZA if c == 0 else 1)
            res.append((-puntos, c, a["nombre"], a))
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
    """Comprueba los ejemplos de D14. Un artista cuenta como encontrado si su nombre o alguno
    de sus alias coincide exactamente (sin tildes ni mayúsculas) con el esperado."""
    def es(a, nombre):
        n = sin_tildes(nombre)
        return any(sin_tildes(x) == n for x in [a["nombre"]] + a["alias"])
    filas = []
    for texto, si, no, top in CASOS:
        res = buscar(texto, artistas, n=top if top < 10**6 else len(artistas))
        fallos = []
        for s in si:
            pos = next((k for k, a in enumerate(buscar(texto, artistas, n=len(artistas)), 1)
                        if es(a, s)), None)
            if pos is None or pos > top:
                fallos.append(f"«{s}» " + ("no aparece" if pos is None else f"sale en el puesto {pos}"))
        fallos += [f"aparece «{x}»" for x in no if any(es(a, x) for a in res)]
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
        nombre = (e["titulos"].get("es") or e["nombres"].get("es") or e["titulos"].get("en")
                  or e["nombres"].get("en") or e["nombres"].get("mul"))
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
            tipo = "f" if e["ocup"] & MUJER else "p"
            papel = next((r for r in PAPELES_PERSONA if r in e["ocup"]), "")
        else:
            if not (e["tipos"] & grupo_set):
                motivos["ya no figura como grupo musical"] += 1
                continue
            tipo = "g"
            papel = next((r for r in PAPELES_GRUPO if r in e["tipos"]), "")
        alias = sorted({a for a in set(e["nombres"].values()) | set(e["titulos"].values()) | e["alias"]
                        if a != nombre})
        artistas.append(dict(id=i, tipo=tipo, fama=e["wp"], nombre=nombre, alias=alias,
                             anio=e["anio"], pais=e["pais"], papel=papel))

    artistas.sort(key=lambda a: (-a["fama"], a["nombre"]))
    ruta = os.path.join(SALIDA, "artistas.tsv.gz")
    limpio = lambda s: s.replace("\t", " ").replace("\n", " ").replace("|", "/")
    with gzip.open(ruta, "wt", encoding="utf-8", compresslevel=9) as f:
        f.write("\t".join(CABECERA) + "\n")
        for a in artistas:
            f.write("\t".join([a["id"], a["tipo"], str(a["fama"]), limpio(a["nombre"]),
                               "|".join(limpio(x) for x in a["alias"]),
                               str(a["anio"] or ""), a["pais"] or "", a["papel"]]) + "\n")
    tam = os.path.getsize(ruta)

    n_p = sum(a["tipo"] != "g" for a in artistas)
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
