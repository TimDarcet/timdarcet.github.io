#!/usr/bin/env python3
"""Build paris.js from IGN BD TOPO instead of OSM (see build.py for the OSM path).
Run with the geo venv:  .ign-venv/bin/python build_ign.py <path-to .7z or extracted dir>

BD TOPO gives a professionally curated, already-noded road network: the `nature` field cleanly
separates real roads from paths/stairs, and official BAN names avoid OSM's typos. So the whole
OSM-specific front-end (EXCLUDE_HW guesswork, cycleway rescue, typo detection) is gone here — we
just filter `troncon_de_route` by nature+name, clip to the Paris commune, and hand the segments to
build.build_from_ways(), which planarizes and emits the identical window.PARIS contract."""
import sys, glob, os
import geopandas as gpd
from shapely.geometry import shape
import build

OUT = "paris.js"
# BD TOPO `nature` values kept as streets. Sentier/Chemin/Escalier are included: they recover real
# named pedestrian ways (park allées, villas, passages) AND repair connectivity — stairs/paths are
# often the missing link between two stretches of the same street (like Ordener's cycleway block).
ROAD_NATURE = {"Route à 1 chaussée", "Route à 2 chaussées", "Type autoroutier", "Bretelle",
               "Rond-point", "Route empierrée", "Sentier", "Chemin", "Escalier"}
# collaboratif names are UPPERCASE + type-abbreviated ("R DES ECLUSES"); expand the leading type word
# and Title-case so they read like the BAN names. Only used to fill streets BAN doesn't name.
ABBR = {"R": "Rue", "AV": "Avenue", "BD": "Boulevard", "PL": "Place", "ALL": "Allée", "IMP": "Impasse",
        "SQ": "Square", "PAS": "Passage", "VLA": "Villa", "QU": "Quai", "CHE": "Chemin", "RTE": "Route",
        "CRS": "Cours", "SEN": "Sente", "GAL": "Galerie", "SNT": "Sentier", "PROM": "Promenade",
        "CAR": "Carrefour", "RPT": "Rond-point", "PRV": "Parvis", "HAM": "Hameau", "PTE": "Porte",
        "ARC": "Arcades", "PSTY": "Péristyle", "CITE": "Cité", "RUELL": "Ruelle", "ESP": "Esplanade",
        "PLE": "Passerelle", "VGE": "Village", "RPE": "Rampe", "TSSE": "Terrasse", "RES": "Résidence",
        "VOI": "Voie", "TUN": "Tunnel", "AUT": "Autoroute", "COR": "Corniche", "MAIL": "Mail"}
def expand_abbr(s):
    if not isinstance(s, str) or not s: return s
    LOW = {"De", "Du", "Des", "La", "Le", "Les", "À", "A", "Aux", "Au", "En", "Et", "Sur", "D'", "L'"}
    toks = s.split()
    head = ABBR.get(toks[0].upper(), toks[0].capitalize()) if toks else ""
    tail = []
    for t in toks[1:]:
        w = t.title() if t.isupper() else t          # Title-case ALL-CAPS body words
        tail.append(w.lower() if w in LOW else w)    # but lowercase French connectors
    return " ".join([head] + tail)

def find_gpkg(arg):
    import py7zr
    if os.path.isfile(arg) and arg.lower().endswith(".7z"):
        out = arg[:-3] + "_extracted"
        if not os.path.isdir(out):
            print(f"extracting {arg} ...", file=sys.stderr)
            with py7zr.SevenZipFile(arg) as z: z.extractall(out)
        arg = out
    if os.path.isfile(arg): return arg
    hits = glob.glob(os.path.join(arg, "**", "*.gpkg"), recursive=True)
    if not hits: sys.exit(f"no .gpkg under {arg}")
    return hits[0]

def ring_of(geom):
    """Largest exterior ring of a (Multi)Polygon as [(lat,lon),...]."""
    polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    big = max(polys, key=lambda p: p.area)
    return [(y, x) for x, y in big.exterior.coords]

def main(arg, extra_nature=(), out=OUT):
    gpkg = find_gpkg(arg)
    print(f"reading {gpkg}", file=sys.stderr)
    nature_keep = ROAD_NATURE | set(extra_nature)

    # arrondissements from BD TOPO's own layer (no OSM fetch). Label = the arrondissement number.
    import re
    adm = gpd.read_file(gpkg, layer="arrondissement_municipal").to_crs(4326)
    namecol = next(c for c in adm.columns if c.lower() in ("nom", "nom_officiel", "nom_m"))
    arr = []
    for _, row in adm.iterrows():
        m = re.search(r"(\d+)", str(row[namecol]))
        arr.append((m.group(1) if m else str(row[namecol]), ring_of(row.geometry)))
    arr.sort(key=lambda x: int(x[0]) if x[0].isdigit() else 999)
    # commune polygon (Paris) to clip streets to the city proper
    com = gpd.read_file(gpkg, layer="commune").to_crs(4326)
    paris_poly = com[com["nom_officiel"] == "Paris"].geometry.union_all()

    # roads: keep real street natures; clip to Paris. Name = official BAN name when present,
    # else the OSM-derived collaboratif name (uppercase+abbreviated -> expand it). BAN is
    # authoritative/clean; collaboratif only fills the ~23% of streets BAN doesn't name.
    g = gpd.read_file(gpkg, layer="troncon_de_route").to_crs(4326)
    g = g[g["nature"].isin(nature_keep)]
    g = g[g.geometry.intersects(paris_poly)]
    ban = g["nom_voie_ban_gauche"].fillna(g["nom_voie_ban_droite"]).fillna(g["cpx_toponyme_route_nommee"])
    col = g["nom_collaboratif_gauche"].fillna(g["nom_collaboratif_droite"]).map(expand_abbr)
    g["_nm"] = ban.fillna(col)
    g = g[g["_nm"].notna() & (g["_nm"] != "")]
    g = g[~g["_nm"].str.contains(r"\b\w{1,3}/\d+\b", regex=True)]   # drop internal codes ("Voie M/18", "Vc Fi/13")
    n_ban = ban.notna().sum(); n_col = (ban.isna() & col.notna()).sum()
    print(f"{len(g)} named road segments in Paris ({n_ban} BAN, {n_col} collaboratif-only)", file=sys.stderr)

    # -> (name, [nodeid,...]) with a fresh nodeid per vertex; planarize() re-nodes shared points.
    ways, coord = [], {}
    nid = 0
    for nm, geom in zip(g["_nm"], g.geometry):
        lines = list(geom.geoms) if geom.geom_type == "MultiLineString" else [geom]
        for ln in lines:
            ids = []
            for c in ln.coords:
                x, y = c[0], c[1]                        # drop Z (BD TOPO geometries are 3D)
                coord[nid] = (y, x); ids.append(nid); nid += 1
            if len(ids) >= 2: ways.append((nm, ids))

    build.build_from_ways(ways, coord, arr=arr, out=out)

if __name__ == "__main__":
    if len(sys.argv) < 2: sys.exit("usage: build_ign.py <path-to .7z or dir> [extra_nature,...] [out.js]")
    extra = sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2] else ()
    outp = sys.argv[3] if len(sys.argv) > 3 else OUT
    main(sys.argv[1], extra_nature=extra, out=outp)
