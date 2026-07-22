#!/usr/bin/env python3
"""Build paris.js from IGN BD TOPO instead of OSM (see build.py for the OSM path).
Run with the geo venv:  .ign-venv/bin/python build_ign.py <path-to .7z or extracted dir>

BD TOPO gives a professionally curated, already-noded road network: the `nature` field cleanly
separates real roads from paths/stairs, and official BAN names avoid OSM's typos. So the whole
OSM-specific front-end (EXCLUDE_HW guesswork, cycleway rescue, typo detection) is gone here — we
just filter `troncon_de_route` by nature+name, clip to the Paris commune, and hand the segments to
build.build_from_ways(), which planarizes and emits the identical window.PARIS contract."""
import sys, glob, os, collections
import geopandas as gpd
import shapely
from shapely.geometry import shape, LineString
from shapely.ops import unary_union
from shapely.strtree import STRtree
import build

OUT = "paris.js"
# BD TOPO leaves some real streets unnamed (e.g. the Opéra-side block of Rue des Mathurins). Fill those
# from OSM: an unnamed segment adopts an OSM street's name only if it lies essentially ON that street AND
# the name is one BD TOPO already uses elsewhere (so we only ever complete streets the base map knows,
# never import OSM-only features). "On that street" = both: the segment's MEAN distance to the OSM line
# is small (they're the same line, not a parallel neighbour) and its MAX distance is bounded (tolerating
# one local bulge where the two surveys disagree, like the wide Mathurins-at-Opéra frontage).
AVG_TOL = 2.0       # metres: mean inf-distance (segment -> OSM street)
SUP_TOL = 10.0      # metres: max  inf-distance (directed Hausdorff)
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

def osm_donors(ign_named):
    """OSM named streets eligible to donate a name: same filtering as an OSM build (build.py), then
    the semantic gate — keep only names BD TOPO already uses (normalized). Returns (names, geoms@2154)."""
    d = build.fetch()
    nodes = {e["id"]: (e["lat"], e["lon"]) for e in d["elements"] if e["type"] == "node"}
    def base_ok(hw, nm, t):
        if not nm: return False
        if t.get("footway") in {"sidewalk", "crossing", "traffic_island", "link"}: return False
        if nm.startswith("Voie ") and "/" in nm: return False
        if "parking" in nm.lower(): return False
        return True
    road_names = {t.get("name", "") for e in d["elements"] if e["type"] == "way"
                  for t in [e["tags"]] if t.get("highway") not in build.EXCLUDE_HW and base_ok(t.get("highway"), t.get("name", ""), t)}
    road_norms = {build.norm_name(x) for x in road_names}
    osm = collections.defaultdict(list)
    for e in d["elements"]:
        if e["type"] != "way": continue
        t = e["tags"]; hw = t.get("highway"); nm = t.get("name", "")
        if not base_ok(hw, nm, t): continue
        if hw in build.EXCLUDE_HW and not (hw == "cycleway" and build.keep_cycleway(nm, road_names, road_norms)): continue
        if build.norm_name(nm) not in ign_named: continue           # semantic gate
        pts = [(nodes[n][1], nodes[n][0]) for n in e["nodes"] if n in nodes]
        if len(pts) >= 2: osm[nm].append(LineString(pts))
    names = list(osm)
    geoms = list(gpd.GeoSeries([unary_union(v) for v in osm.values()], crs=4326).to_crs(2154).values)
    return names, geoms

def recover_names(g, ign_named, avg_tol=AVG_TOL, sup_tol=SUP_TOL):
    """Fill BD TOPO's unnamed road segments with an OSM street name when the segment lies essentially on
    that street: mean inf-distance <= avg_tol AND max inf-distance <= sup_tol. Among qualifying donors,
    pick the closest by mean. Mutates g['_nm'] in place; returns the count recovered."""
    names, geoms = osm_donors(ign_named)
    if not geoms: return 0
    tree = STRtree(geoms)
    unn = g[g["_nm"].isna()].to_crs(2154)
    got = {}
    for idx, seg in unn.geometry.items():
        if seg is None or seg.length < 1: continue
        pts = shapely.points(shapely.get_coordinates(seg.segmentize(1.0)))   # 1 m samples, handles Multi
        best = None
        for qi in tree.query(seg, predicate="dwithin", distance=sup_tol):
            d = shapely.distance(pts, geoms[qi]); avg = float(d.mean()); sup = float(d.max())
            if avg <= avg_tol and sup <= sup_tol and (best is None or avg < best[1]): best = (names[qi], avg)
        if best: got[idx] = best[0]
    if got: g.loc[list(got), "_nm"] = list(got.values())
    print(f"recovered {len(got)} unnamed segments from OSM (mean<={avg_tol:.0f} m, sup<={sup_tol:.0f} m)", file=sys.stderr)
    return len(got)

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
    ign_named = {build.norm_name(x) for x in g["_nm"].dropna().unique() if str(x).strip()}
    n_rec = recover_names(g, ign_named)                             # borrow OSM names for gap segments
    g = g[g["_nm"].notna() & (g["_nm"] != "")]
    g = g[~g["_nm"].str.contains(r"\b\w{1,3}/\d+\b", regex=True)]   # drop internal codes ("Voie M/18", "Vc Fi/13")
    n_ban = ban.notna().sum(); n_col = (ban.isna() & col.notna()).sum()
    print(f"{len(g)} named road segments in Paris ({n_ban} BAN, {n_col} collaboratif-only, {n_rec} OSM-recovered)", file=sys.stderr)

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
