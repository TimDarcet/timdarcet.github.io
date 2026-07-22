#!/usr/bin/env python3
"""Bake area boundary polygons (and quartier tags) into paris.js so the progress panel can outline
the exact arrondissement / quartier / bank you hover, instead of a convex-hull approximation.
Run with the geo venv:  .ign-venv/bin/python build_areas.py

Adds to window.PARIS:
  arrPoly  – one boundary (list of [lat,lon] rings) per data.arr entry, from IGN arrondissement_municipal
  rivPoly  – three boundaries [right bank, left bank, islands]; banks are the union of their arrondissements
  qua/q    – quartier label list + per-edge quartier index (needs the 80 quartier boundaries)
  quaPoly  – one boundary per quartier

Arrondissement + rive are built offline from the IGN gpkg. Quartiers aren't in BD TOPO, so they're
fetched from Paris Open Data (or read from a local quartiers.geojson); if that's unavailable the script
still bakes arr + rive and just skips quartiers. Re-runnable and otherwise non-destructive."""
import json, sys, os, re, glob, urllib.request
import geopandas as gpd
from shapely import STRtree
from shapely.geometry import Point
from shapely.ops import unary_union

OUT = "paris.js"
LOCAL = "quartiers.geojson"
URL = "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets/quartier_paris/exports/geojson"
GAUCHE = {5, 6, 7, 13, 14, 15}                       # left-bank arrondissement numbers

def rings_of(geom, ndp=5):                            # exterior ring(s) of a (Multi)Polygon as [[lat,lon],...]
    if geom is None or geom.is_empty: return []
    polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    return [[[round(y, ndp), round(x, ndp)] for x, y in p.exterior.coords] for p in polys]

def find_gpkg():
    hits = glob.glob("bdtopo_d075_extracted/**/*.gpkg", recursive=True)
    if not hits: sys.exit("no IGN .gpkg found (run build_ign extraction first)")
    return hits[0]

def island_rings(gpkg):                               # the two islands = holes in the Seine water surface
    g = gpd.read_file(gpkg, layer="surface_hydrographique").to_crs(4326)
    u = unary_union(g.geometry.values)
    polys = list(u.geoms) if u.geom_type == "MultiPolygon" else [u]
    holes = [r for p in polys for r in p.interiors
             if 2.34 < r.centroid.x < 2.37 and 48.849 < r.centroid.y < 48.860]
    from shapely.geometry import Polygon
    out = []
    for r in sorted(holes, key=lambda r: -r.envelope.area)[:2]:
        p = Polygon(r).buffer(0.0003).simplify(0.0004)
        out.append([[round(y, 5), round(x, 5)] for x, y in p.exterior.coords])
    return out

def bake_arr_rive(data, gpkg):
    adm = gpd.read_file(gpkg, layer="arrondissement_municipal").to_crs(4326)
    namecol = next(c for c in adm.columns if c.lower() in ("nom", "nom_officiel", "nom_m"))
    by_num = {}                                       # arrondissement number -> geometry
    for _, row in adm.iterrows():
        m = re.search(r"(\d+)", str(row[namecol]))
        if m: by_num[int(m.group(1))] = row.geometry
    arrpoly, droite, gauche = [], [], []
    for lab in data["arr"]:
        num = int(re.sub(r"\D", "", str(lab)) or -1)
        g = by_num.get(num)
        arrpoly.append(rings_of(g.simplify(0.0002)) if g is not None else [])
        if g is not None: (gauche if num in GAUCHE else droite).append(g)
    data["arrPoly"] = arrpoly
    data["rivPoly"] = [rings_of(unary_union(droite).simplify(0.0003)),
                       rings_of(unary_union(gauche).simplify(0.0003)),
                       island_rings(gpkg)]
    print(f"arr polygons={sum(1 for a in arrpoly if a)}  rive polygons=3", file=sys.stderr)

def bake_quartiers(data):
    if os.path.exists(LOCAL):
        q = gpd.read_file(LOCAL).to_crs(4326)
    else:
        try:
            print("fetching quartier boundaries from Paris Open Data...", file=sys.stderr)
            with urllib.request.urlopen(URL, timeout=20) as r: open(LOCAL, "wb").write(r.read())
            q = gpd.read_file(LOCAL).to_crs(4326)
        except Exception as e:
            print(f"  quartiers unavailable ({type(e).__name__}) — arr+rive baked, quartier skipped", file=sys.stderr)
            return
    namecol = next((c for c in ("l_qu", "nom_quartier", "nom", "name") if c in q.columns), None)
    if not namecol: sys.exit(f"no quartier name column in {list(q.columns)}")
    polys, names = list(q.geometry), [str(n) for n in q[namecol]]
    labels = sorted(set(names)); idx = {l: i for i, l in enumerate(labels)}
    tree = STRtree(polys)
    miss = 0
    for e in data["edges"]:
        la, lo = e["p"][0][len(e["p"][0]) // 2]       # representative point, matching e.a assignment
        pt = Point(lo, la); e["q"] = -1
        for ci in tree.query(pt):
            if polys[ci].contains(pt): e["q"] = idx[names[ci]]; break
        if e["q"] < 0: miss += 1
    data["qua"] = labels
    data["quaPoly"] = [rings_of(unary_union([polys[i] for i, n in enumerate(names) if n == lab]).simplify(0.0001))
                       for lab in labels]
    print(f"quartiers={len(labels)}  tagged={len(data['edges'])-miss}/{len(data['edges'])}", file=sys.stderr)

def main():
    src = open(OUT, encoding="utf-8").read()
    if not src.startswith("window.PARIS="): sys.exit("unexpected paris.js format")
    data = json.loads(src[len("window.PARIS="):].strip().rstrip(";"))
    gpkg = find_gpkg(); print(f"reading {gpkg}", file=sys.stderr)
    bake_arr_rive(data, gpkg)
    bake_quartiers(data)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("window.PARIS="); json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

if __name__ == "__main__":
    main()
