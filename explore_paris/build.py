#!/usr/bin/env python3
"""Build paris.js from the Ville de Paris street network: opendata.paris.fr `troncon_voie` (street segments)
named by `voie` (official names). Run:  uv run --with shapely python build.py

Streets = same-name segments, planarised together (planarize): every 2D crossing is a junction, whatever the
level, so a tunnel meets the streets above it (lenient: more reachable streets, never fewer). Dropped: coded
voies ("Voie AB/12"), the périphérique, motorways and their interchanges. Some boulevards list a segment twice
(once per side): kept once.
Blocks = planar faces of the GROUND-level streets plus the city border. The border bounds blocks but is never
required to reveal them, so the strip between the outermost streets and the city limit is revealable.
Area polygons (arr/rive/quartier) are carried over from the current paris.js, and the lore files'
nearest-street indices (plaques.js, sucettes.js) are recomputed for the new streets."""
import json, math, os, re, sys, gzip, collections, urllib.request
import shapely

OUT = "paris.js"
API = "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets/%s/exports/geojson"
SKIP = re.compile(r"\b\w{1,3}/\d+\b|^Boulevard Périphérique|^Autoroute|^Echangeur")
LAT0 = math.radians(48.86); COSLAT = math.cos(LAT0)

def pt_in_ring(lat, lon, ring):
    inside = False; n = len(ring); j = n - 1
    for i in range(n):
        yi, xi = ring[i]; yj, xj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def haversine(a, b):
    R = 6371000
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = math.radians(b[0] - a[0]); dl = math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))

def perp(p, a, b):  # perpendicular distance in projected (deg) space
    ax, ay = a[1] * COSLAT, a[0]; bx, by = b[1] * COSLAT, b[0]; px, py = p[1] * COSLAT, p[0]
    dx, dy = bx - ax, by - ay
    if dx == dy == 0: return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0, min(1, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

def dp_simplify(pts, eps=2e-5):
    if len(pts) < 3: return pts
    a, b = pts[0], pts[-1]; dmax = idx = 0
    for i in range(1, len(pts) - 1):
        d = perp(pts[i], a, b)
        if d > dmax: dmax, idx = d, i
    if dmax > eps:
        return dp_simplify(pts[:idx + 1], eps)[:-1] + dp_simplify(pts[idx:], eps)
    return [a, b]

def build_faces(edges, nodes):
    """City blocks = planar faces of the output street network (Shapely polygonize), holes included so a
    block never covers an island of streets inside it. A block needs every street touching it: its
    outline, its holes' outlines and the dead ends poking into it. Junctions are first inserted into
    every street meeting them, so DP-simplified polylines still node exactly at T-junctions.
    Returns [{e:[edge idx...], p:[outer ring, hole rings...] of [lat,lon], m: area m^2}]."""
    from shapely.geometry import LineString, Point
    from shapely.ops import unary_union, polygonize
    from shapely.strtree import STRtree
    M = 111320.0
    def xy(p): return (p[1] * COSLAT * M, p[0] * M)                    # metres, planar
    lines = []
    for e in edges:
        ways = [[xy(p) for p in w] for w in e["p"] if len(w) >= 2]
        for ni in e["i"]:
            n = xy(nodes[ni])
            if any(n in w for w in ways): continue
            d, wi, k = min((Point(n).distance(LineString(w[k:k + 2])), wi, k) for wi, w in enumerate(ways) for k in range(len(w) - 1))
            if d < 3: ways[wi].insert(k + 1, n)
        lines.append(unary_union([LineString(w) for w in ways]))
    tree = STRtree(lines)
    faces = []
    for poly in polygonize(unary_union(lines)):
        if poly.area < 20: continue                                     # rounding slivers
        near = poly.buffer(0.05)                                        # corner-only contact stays < 0.5 m
        es = sorted(int(q) for q in tree.query(near) if lines[q].intersection(near).length > 0.5)
        rings = [dp_simplify([(y / M, x / (COSLAT * M)) for x, y in r.coords], 1.2e-5)[:-1] for r in [poly.exterior, *poly.interiors]]
        if not es or len(rings[0]) < 3: continue
        faces.append({"e": es, "p": [[[round(la, 5), round(lo, 5)] for la, lo in r] for r in rings if len(r) >= 3], "m": round(poly.area)})
    return faces

def planarize(ways, coord, connect_m=10.0):
    """Turn the raw street graph into a planar one: node the whole network (unary_union) so every
    crossing/T-junction is split and shared, then give each pair of differently named streets passing
    within connect_m metres without touching one shared junction. Returns (ways2, coord2) as
    (name, [nodeid,...]) on synthetic integer ids."""
    from shapely.geometry import LineString
    from shapely.ops import unary_union
    from shapely.strtree import STRtree
    M = 111320.0
    def xy(la, lo): return (lo * COSLAT * M, la * M)
    def ll(x, y): return (y / M, x / (COSLAT * M))
    def key(x, y): return (round(x, 3), round(y, 3))              # mm snap: absorbs float noise only

    def node_name(lines, names):                                  # planar-node, tag each piece with a street name
        tree = STRtree(lines)
        u = unary_union(lines)
        pcs = list(u.geoms) if u.geom_type == "MultiLineString" else [u]
        out = []
        for pc in pcs:
            mid = pc.interpolate(0.5, normalized=True); nm = None; best = 0.6
            for q in tree.query(pc):
                d = lines[q].distance(mid)
                if d < best: best = d; nm = names[q]
            if nm is not None and len(pc.coords) >= 2: out.append((nm, list(pc.coords)))
        return out

    lines = [LineString([xy(*coord[n]) for n in nds]) for nm, nds in ways if len(nds) >= 2]
    names = [nm for nm, nds in ways if len(nds) >= 2]
    pieces = node_name(lines, names)                              # planar pieces (name, [(x,y),...])

    # connect differently named streets that pass within connect_m of each other but never touch (the
    # source left them unnoded): ONE junction per name pair, between their closest points. Topological
    # only: both streets list it as a junction, but neither line is bent towards it.
    from shapely.ops import nearest_points
    byname = collections.defaultdict(list)                        # name -> piece indices
    for idx, (nm, cs) in enumerate(pieces): byname[nm].append(idx)
    plines = [LineString(cs) for _, cs in pieces]
    gnames = list(byname)
    ggeoms = [unary_union([plines[i] for i in byname[nm]]) for nm in gnames]
    gtree = STRtree(ggeoms)
    near = lambda nm, p: min(byname[nm], key=lambda i: plines[i].distance(p))   # piece of nm closest to p
    links = []                                                    # (piece, piece, junction point)
    for a in range(len(gnames)):
        ga = ggeoms[a]
        for b in gtree.query(ga.buffer(connect_m)):
            if b <= a or ga.distance(ggeoms[b]) >= connect_m or ga.intersects(ggeoms[b]): continue
            pa, pb = nearest_points(ga, ggeoms[b])
            links.append((near(gnames[a], pa), near(gnames[b], pb), ll((pa.x + pb.x) / 2, (pa.y + pb.y) / 2)))

    # assign synthetic node ids; shared nodes coincide exactly after noding so exact keying is safe
    coord2 = {}; idof = {}; first = {}
    def nid(x, y):
        k = key(x, y); i = idof.get(k)
        if i is None: i = idof[k] = len(idof); coord2[i] = ll(x, y)
        return i
    out = []
    for idx, (nm, cs) in enumerate(pieces):
        nds = [nid(x, y) for x, y in cs]
        nds = [nds[i] for i in range(len(nds)) if i == 0 or nds[i] != nds[i-1]]
        if len(nds) >= 2: out.append((nm, nds)); first[idx] = nds[0]
    return out, coord2, [(pieces[a][0], first[a], pieces[b][0], first[b], p) for a, b, p in links if a in first and b in first]

LOW = {"de", "du", "des", "la", "le", "les", "l", "d", "aux", "au", "à", "en", "et", "sur", "sous", "lès", "point"}
def tidy(nm):                                     # official names, display fixes: "Servan-schreiber" -> "Servan-Schreiber",
    nm = re.sub(r"\s+\d+\s?ème$", "", nm)        # "Cour de Rome 8ème" -> "Cour de Rome" (disambiguating suffix)
    nm = re.sub(r"(?<=\w[-'’])([a-zà-ÿ])(\w*)", lambda m: m[0] if m[0].lower() in LOW else m[1].upper() + m[2], nm)
    return nm.replace("révolution de janvi", "révolution de janvier 2011") if nm.endswith("de janvi") else nm   # truncated upstream

def fetch(ds):                                    # cached raw export (the server gzips regardless)
    f = ds + "_raw.geojson"
    if not os.path.exists(f):
        raw = urllib.request.urlopen(API % ds, timeout=300).read()
        open(f, "wb").write(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
    return json.load(open(f, encoding="utf-8"))["features"]

def load_js(f):
    s = open(f, encoding="utf-8").read(); return json.loads(s[s.index("=") + 1:].strip().rstrip(";"))

def save_js(f, var, data):
    with open(f, "w", encoding="utf-8") as fh: fh.write(f"window.{var}="); json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))

def network(ways, coord, links):
    """same-name pieces connected through shared nodes (or ends < 1 m apart) -> streets, plus planarize's
    near-miss junctions; streets sharing a node -> networks.
    Returns (streets, junction nodes, network id per street, id of the largest network)."""
    byname = collections.defaultdict(list)
    for (nm, lv), nds in ways: byname[nm].append((lv, nds))
    edges = []
    for nm, group in byname.items():
        parent = list(range(len(group)))
        def find(x):
            while parent[x] != x: parent[x] = parent[parent[x]]; x = parent[x]
            return x
        at = {}
        for k, (_, nds) in enumerate(group):
            for n in nds:
                if n in at: parent[find(k)] = find(at[n])
                else: at[n] = k
        ends = [(k, coord[n]) for k, (_, nds) in enumerate(group) for n in (nds[0], nds[-1])]
        for k1, p1 in ends:
            for k2, p2 in ends:
                if k1 < k2 and haversine(p1, p2) < 1: parent[find(k1)] = find(k2)
        comp = collections.defaultdict(list)
        for k, w in enumerate(group): comp[find(k)].append(w)
        edges += [{"n": nm, "ways": ws, "nodes": {n for _, nds in ws for n in nds}} for ws in comp.values()]
    owner = {(e["n"], n): k for k, e in enumerate(edges) for n in e["nodes"]}
    for (na, _), a, (nb, _), b, p in links:                    # a new node, on neither street's line
        coord[len(coord)] = p; edges[owner[na, a]]["nodes"].add(len(coord) - 1); edges[owner[nb, b]]["nodes"].add(len(coord) - 1)
    use = collections.Counter(n for e in edges for n in e["nodes"])
    inter = {n for n, c in use.items() if c >= 2}
    on = collections.defaultdict(list)
    for k, e in enumerate(edges):
        for n in e["nodes"] & inter: on[n].append(k)
    net = [-1] * len(edges)
    for s in range(len(edges)):
        if net[s] >= 0: continue
        stack = [s]
        while stack:
            x = stack.pop()
            if net[x] >= 0: continue
            net[x] = s; stack.extend(k for n in edges[x]["nodes"] & inter for k in on[n] if net[k] < 0)
    return edges, inter, net, collections.Counter(net).most_common(1)[0][0]

def main():
    voies = {f["properties"]["n_sq_vo"]: f for f in fetch("voie")}
    troncons = fetch("troncon_voie")
    used = {f["properties"]["n_sq_vo"] for f in troncons}
    # a few streets (Rue de Reuilly, the newest names) have no segments yet: use the street's own line
    troncons += [{"properties": {"n_sq_vo": k, "c_niveau": 0}, "geometry": {"coordinates": cs}} for k, f in voies.items()
                 if k not in used and f["geometry"] and f["properties"]["c_coinsee"] == 75056
                 for cs in ([f["geometry"]["coordinates"]] if f["geometry"]["type"] == "LineString" else f["geometry"]["coordinates"])]
    raw, coord0, seen = [], {}, set()
    for f in troncons:
        p, g = f["properties"], f["geometry"]
        nm = tidy(voies[p["n_sq_vo"]]["properties"]["l_longmin"] or "") if p["n_sq_vo"] in voies else None
        if not g or not nm or SKIP.search(nm): continue
        cs = g["coordinates"]; side = (nm, frozenset([tuple(cs[0]), tuple(cs[-1])]))
        if side in seen: continue
        seen.add(side); ids = []
        for lon, lat in cs: coord0[len(coord0)] = (lat, lon); ids.append(len(coord0) - 1)
        raw.append(((nm, p["c_niveau"] or 0), ids))   # level rides along in the name through planarize

    # a few streets stop short of the network (an impasse ending before the street axis, or reached only
    # through a coded voie): extend each such island to the nearest network street if within LINK_M
    LINK_M, K = 40, COSLAT * 111320          # metres; metres per degree of longitude
    for rnd in range(5):
        ways, coord, links = planarize(raw, coord0)
        edges, inter, net, big = network(ways, coord, links)
        if rnd == 4: break
        xy = lambda n: (coord[n][1] * K, coord[n][0] * 111320)
        rawgeo = collections.defaultdict(list)       # planarize re-nodes the RAW lines, so aim at those
        for (nm, _), ids in raw: rawgeo[nm].append([(coord0[n][1] * K, coord0[n][0] * 111320) for n in ids])
        main_ = sorted({e["n"] for e, c in zip(edges, net) if c == big})
        tree = shapely.STRtree([shapely.MultiLineString(rawgeo[nm]) for nm in main_])
        linked = 0
        for c in set(net) - {big}:                   # from an island vertex, overshoot the street line by 0.5 m so it gets noded
            near = [(tree.geometries[j].distance(v), v, j, e) for e, x in zip(edges, net) if x == c
                    for v in {shapely.Point(xy(n)) for _, nds in e["ways"] for n in nds}
                    for j in tree.query(v, predicate="dwithin", distance=LINK_M) if main_[j] != e["n"]]
            if not near: continue
            d, v, j, e = min(near, key=lambda t: t[0])
            b = shapely.shortest_line(v, tree.geometries[j]).coords[1]
            b = (b[0] + (b[0] - v.x) / d * .5, b[1] + (b[1] - v.y) / d * .5)
            for x, y in ((v.x, v.y), b): coord0[len(coord0)] = (y / 111320, x / K)
            raw.append(((e["n"], 0), [len(coord0) - 2, len(coord0) - 1])); linked += 1
        if not linked: break
        print(f"linked {linked} street islands to the network", file=sys.stderr)
    lost = sorted({e["n"] for e, c in zip(edges, net) if c != big})
    print(f"dropped {len(lost)} streets farther than {LINK_M} m from the network: {', '.join(lost)}", file=sys.stderr)
    edges = [e for e, c in zip(edges, net) if c == big]

    old = load_js(OUT)
    arr = list(zip(old["arr"], [max(rings, key=len) for rings in old["arrPoly"]]))
    which_arr = lambda la, lo: next((k for k, (_, r) in enumerate(arr) if pt_in_ring(la, lo, r)), -1)
    city = shapely.union_all([shapely.Polygon([(lo, la) for la, lo in r]).buffer(1e-5) for _, r in arr]).buffer(-1e-5)
    city = shapely.union_all([shapely.Polygon(p.exterior) for p in shapely.get_parts(city)])   # fill slivers between arrondissements
    border = [[[round(la, 5), round(lo, 5)] for lo, la in p.exterior.coords] for p in shapely.get_parts(city)]

    nidx, nodes, out, ground = {}, [], [], []
    path = lambda nds: [[round(la, 5), round(lo, 5)] for la, lo in dp_simplify([coord[n] for n in nds])]
    for e in edges:
        for n in sorted(e["nodes"] & inter):
            if n not in nidx: nidx[n] = len(nodes); nodes.append([round(coord[n][0], 5), round(coord[n][1], 5)])
        p = [path(nds) for _, nds in e["ways"]]
        i = sorted(nidx[n] for n in e["nodes"] & inter)
        km = sum(haversine(coord[a], coord[b]) for _, nds in e["ways"] for a, b in zip(nds, nds[1:])) / 1000
        out.append({"n": e["n"], "k": round(km, 3), "i": i, "p": p, "a": which_arr(*p[0][len(p[0]) // 2])})
        gp = [path(nds) for lv, nds in e["ways"] if lv == 0]
        ground.append({"p": gp, "i": i if gp else []})

    qua = [shapely.union_all([shapely.Polygon([(lo, la) for la, lo in r]).buffer(0) for r in rings]) for rings in old.get("quaPoly", [])]
    faces = []
    for f in build_faces(ground + [{"p": border, "i": []}], nodes):
        f["e"] = [x for x in f["e"] if x != len(ground)]   # the border counts as always discovered
        poly = shapely.make_valid(shapely.Polygon([(lo, la) for la, lo in f["p"][0]], [[(lo, la) for la, lo in r] for r in f["p"][1:]]))
        if not f["e"] or poly.intersection(city).area < .5 * poly.area: continue   # outside the city limit
        ring = f["p"][0]; pt = shapely.Point(sum(p[1] for p in ring) / len(ring), sum(p[0] for p in ring) / len(ring))
        if not city.contains(pt): pt = poly.representative_point()          # concave block: mean falls outside
        f["a"] = which_arr(pt.y, pt.x)
        if qua: f["q"] = next((k for k, q in enumerate(qua) if q.contains(pt)), -1)
        faces.append(f)

    data = {"nodes": nodes, "edges": out, "faces": faces, "arr": old["arr"], "border": border}
    data.update({k: old[k] for k in ("arrPoly", "rivPoly", "qua", "quaPoly") if k in old})
    save_js(OUT, "PARIS", data)
    print(f"streets={len(out)} intersections={len(nodes)} blocks={len(faces)} km={sum(e['k'] for e in out):.1f} "
          f"unassigned streets={sum(e['a'] < 0 for e in out)} blocks={sum(f['a'] < 0 for f in faces)}", file=sys.stderr)

    geoms = [shapely.MultiLineString([[(lo, la) for la, lo in pl] for pl in e["p"] if len(pl) > 1]) for e in out]
    tree = shapely.STRtree(geoms)
    for f, var in (("plaques.js", "PLAQUES"), ("sucettes.js", "SUCETTES")):
        items = load_js(f)
        for it in items: it["ei"] = int(tree.nearest(shapely.Point(it["lo"], it["la"])))
        save_js(f, var, items)

if __name__ == "__main__":
    main()
