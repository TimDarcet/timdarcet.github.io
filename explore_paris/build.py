#!/usr/bin/env python3
"""Fetch Paris named streets from OpenStreetMap (Overpass) and emit paris.json.
Streets = edges (ways merged by name + connectivity). Intersections = nodes shared
by two different streets. Only the largest connected component is kept so 100% is reachable."""
import json, math, os, sys, re, urllib.request, urllib.parse, collections, unicodedata
sys.setrecursionlimit(100000)

AREA = 3600007444  # Paris commune boundary relation 7444
RAW = "paris_raw.json"
ARR_RAW = "arr_raw.json"
OUT = "paris.js"
LAT0 = math.radians(48.86); COSLAT = math.cos(LAT0)
EXCLUDE_HW = {"steps", "elevator", "construction", "proposed", "platform", "bus_stop",
              "cycleway", "corridor", "raceway", "escape", "services", "rest_area"}

def fetch():
    if os.path.exists(RAW):
        return json.load(open(RAW))
    q = f'[out:json][timeout:300];area({AREA})->.a;way(area.a)["highway"]["name"];(._;>;);out body;'
    print("querying Overpass...", file=sys.stderr)
    req = urllib.request.Request("https://overpass-api.de/api/interpreter",
                                 data=("data=" + urllib.parse.quote(q)).encode(),
                                 headers={"User-Agent": "paris-streets-game/1.0"})
    d = json.loads(urllib.request.urlopen(req, timeout=300).read())
    json.dump(d, open(RAW, "w"))
    return d

def fetch_arr():
    if os.path.exists(ARR_RAW):
        return json.load(open(ARR_RAW))
    q = f'[out:json][timeout:120];area({AREA})->.a;relation(area.a)["boundary"="administrative"]["admin_level"="9"];out geom;'
    print("querying arrondissements...", file=sys.stderr)
    req = urllib.request.Request("https://overpass-api.de/api/interpreter",
                                 data=("data=" + urllib.parse.quote(q)).encode(),
                                 headers={"User-Agent": "paris-streets-game/1.0"})
    d = json.loads(urllib.request.urlopen(req, timeout=120).read())
    json.dump(d, open(ARR_RAW, "w"))
    return d

def arr_polys():
    """Return [(label, [ (lat,lon)... ring ]), ...] for the 20 arrondissements,
    stitching each relation's outer ways into a single ring."""
    out = []
    for r in fetch_arr()["elements"]:
        if r["type"] != "relation": continue
        m = re.search(r"(\d+)(?:er|e)?\s*Arrondissement", r["tags"].get("name", ""), re.I)
        label = m.group(1) if m else r["tags"].get("name", "?")
        segs = [[(p["lat"], p["lon"]) for p in mem["geometry"]]
                for mem in r["members"] if mem["type"] == "way" and mem.get("role") == "outer" and "geometry" in mem]
        # greedily chain segments end-to-end into one ring
        if not segs: continue
        ring = list(segs.pop(0))
        while segs:
            for i, s in enumerate(segs):
                if   ring[-1] == s[0]:  ring += s[1:];              segs.pop(i); break
                elif ring[-1] == s[-1]: ring += s[::-1][1:];        segs.pop(i); break
                elif ring[0]  == s[-1]: ring = s[:-1] + ring;       segs.pop(i); break
                elif ring[0]  == s[0]:  ring = s[::-1][:-1] + ring; segs.pop(i); break
            else:
                break  # no more connectable segments
        out.append((label, ring))
    return out

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

def build_faces(edges, keep_list, outidx, inter, coord):
    """Find city blocks as planar faces. Streets are noded (split at every crossing
    and T-junction) via Shapely's polygonize, so blocks stay minimal instead of
    merging across unnoded touch points. Returns [{e:[outedge idx...], p:[[lat,lon]...]}]."""
    from shapely.geometry import LineString
    from shapely.ops import unary_union, polygonize
    from shapely.strtree import STRtree
    M = 111320.0
    def xy(n): la, lo = coord[n]; return (lo * COSLAT * M, la * M)     # metres, planar
    def ll(x, y): return (y / M, x / (COSLAT * M))                     # back to lat, lon

    # one merged LineString per kept street (for mapping faces -> streets),
    # plus the flat list of raw segments to node the whole network.
    edge_lines, segs = [], []
    for ei in keep_list:
        parts = [LineString([xy(n) for n in w]) for w in edges[ei]["ways"] if len(w) >= 2]
        edge_lines.append((outidx[ei], unary_union(parts)))
        segs += parts

    polys = list(polygonize(unary_union(segs)))                        # planar noding + face extraction
    idxs = [oi for oi, _ in edge_lines]
    tree = STRtree([g for _, g in edge_lines])

    out_faces = []
    for poly in polys:
        a = poly.area
        ring = poly.exterior
        es = []
        for q in tree.query(ring):                                    # streets whose bbox meets the block
            if edge_lines[q][1].intersection(ring.buffer(1.2)).length > 4:
                es.append(idxs[q])
        if not es: continue
        pts = [ll(x, y) for x, y in poly.exterior.coords[:-1]]
        pts = dp_simplify(pts + [pts[0]], 1.2e-5)[:-1]
        if len(pts) < 3: continue
        out_faces.append({"e": sorted(set(es)), "p": [[round(la, 5), round(lo, 5)] for la, lo in pts],
                          "m": round(a)})    # block area in m^2
    return out_faces

def planarize(ways, coord, merge_m=6.0):
    """Turn the raw street graph into a planar one AND merge near-coincident junctions in a single
    step. Sequence: node the whole network (unary_union) so every crossing/T-junction is split and
    shared; snap junction nodes within merge_m metres to their cluster centroid (dual carriageways,
    staggered junctions -> one visual crossing); then re-node, so relocating a junction can never
    leave a crossing unsplit. The result is guaranteed planar. Only junction endpoints move —
    interior shape vertices pass through untouched, so street curves stay intact. Returns
    (ways2, coord2) as (name, [nodeid,...]) on synthetic integer ids."""
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

    # junctions = piece endpoints touched by >=2 distinct street names
    epn = collections.defaultdict(set)
    for nm, cs in pieces:
        epn[key(*cs[0])].add(nm); epn[key(*cs[-1])].add(nm)
    junc = [k for k, nms in epn.items() if len(nms) >= 2]

    # union-find merge junctions within merge_m (true distance) -> cluster centroid
    parent = {k: k for k in junc}
    def find(a):
        while parent[a] != a: parent[a] = parent[parent[a]]; a = parent[a]
        return a
    grid = collections.defaultdict(list)
    def gk(k): return (int(k[0] / merge_m), int(k[1] / merge_m))
    for k in junc: grid[gk(k)].append(k)
    for k in junc:
        gi, gj = gk(k)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for m in grid.get((gi + di, gj + dj), []):
                    if m > k and math.hypot(k[0]-m[0], k[1]-m[1]) <= merge_m: parent[find(k)] = find(m)
    cl = collections.defaultdict(list)
    for k in junc: cl[find(k)].append(k)
    moved = {}                                                    # junction point -> merged centroid
    for ks in cl.values():
        cx = sum(p[0] for p in ks) / len(ks); cy = sum(p[1] for p in ks) / len(ks)
        for k in ks: moved[k] = (cx, cy)

    # relocate merged endpoints (interiors untouched), then re-node -> guaranteed planar
    lines2, names2 = [], []
    for nm, cs in pieces:
        cs[0] = moved.get(key(*cs[0]), cs[0]); cs[-1] = moved.get(key(*cs[-1]), cs[-1])
        lines2.append(LineString(cs)); names2.append(nm)
    pieces2 = node_name(lines2, names2)

    # connect distinct-named streets that pass within connect_m of each other but never touch (OSM
    # left them unnoded): give each such NAME pair ONE shared junction at their closest approach.
    # This makes a near-touching street reachable/guessable without welding the two together along
    # their length (parallel streets get a single junction, not a seam).
    from shapely.geometry import Point
    from shapely.ops import nearest_points
    connect_m = 3.0
    byname = collections.defaultdict(list)                        # name -> piece indices
    for idx, (nm, cs) in enumerate(pieces2): byname[nm].append(idx)
    gnames = list(byname)
    ggeoms = [unary_union([LineString(pieces2[i][1]) for i in byname[nm]]) for nm in gnames]
    gtree = STRtree(ggeoms)
    inserts = collections.defaultdict(list)                       # piece idx -> [junction point,...]
    def add_insert(nm, p, J):                                     # queue J onto the piece of nm nearest p
        best, bd = None, 1e9
        for i in byname[nm]:
            d = LineString(pieces2[i][1]).distance(Point(p))
            if d < bd: bd, best = d, i
        inserts[best].append(J)
    for a in range(len(gnames)):
        ga = ggeoms[a]
        for b in gtree.query(ga.buffer(connect_m)):
            if b <= a or ga.distance(ggeoms[b]) >= connect_m or ga.intersects(ggeoms[b]): continue
            pa, pb = nearest_points(ga, ggeoms[b])
            J = ((pa.x + pb.x) / 2, (pa.y + pb.y) / 2)           # single junction between the pair
            add_insert(gnames[a], (pa.x, pa.y), J); add_insert(gnames[b], (pb.x, pb.y), J)

    def insert_pts(cs, pts):                                      # splice each J into cs at its projected spot
        for J in pts:
            ln = LineString(cs); d = ln.project(Point(J)); cum = 0; done = False
            for i in range(len(cs) - 1):
                seg = math.hypot(cs[i+1][0]-cs[i][0], cs[i+1][1]-cs[i][1])
                if cum + seg >= d: cs = cs[:i+1] + [J] + cs[i+1:]; done = True; break
                cum += seg
            if not done: cs = cs[:-1] + [J, cs[-1]]
        return cs
    lines3, names3 = [], []
    for idx, (nm, cs) in enumerate(pieces2):
        cs = insert_pts(cs, inserts[idx]) if idx in inserts else cs
        lines3.append(LineString(cs)); names3.append(nm)
    pieces3 = node_name(lines3, names3)                           # re-node: pairs now share their junction

    # assign synthetic node ids; shared nodes coincide exactly after noding so exact keying is safe
    coord2 = {}; idof = {}
    def nid(x, y):
        k = key(x, y); i = idof.get(k)
        if i is None: i = idof[k] = len(idof); coord2[i] = ll(x, y)
        return i
    out = []
    for nm, cs in pieces3:
        nds = [nid(x, y) for x, y in cs]
        nds = [nds[i] for i in range(len(nds)) if i == 0 or nds[i] != nds[i-1]]
        if len(nds) >= 2: out.append((nm, nds))
    return out, coord2

def dl1(a, b):
    """Damerau-Levenshtein (adjacent transpositions cost 1), short-circuited: returns True iff
    distance <= 1. Used to spot cycleway names that are just typos of an existing street."""
    m, n = len(a), len(b)
    if abs(m - n) > 1: return False
    D = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1): D[i][0] = i
    for j in range(n + 1): D[0][j] = j
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            D[i][j] = min(D[i-1][j]+1, D[i][j-1]+1, D[i-1][j-1]+(a[i-1] != b[j-1]))
            if i > 1 and j > 1 and a[i-1] == b[j-2] and a[i-2] == b[j-1]:
                D[i][j] = min(D[i][j], D[i-2][j-2] + 1)
    return D[m][n] <= 1

# named cycleways to keep even though highway=cycleway: real public ways with no road twin.
# "La Coulée Verte René-Dumont" (the Promenade Plantée) has no street prefix so the rule below
# misses it; whitelist it explicitly.
CYCLE_KEEP = {"La Coulée Verte René-Dumont"}
CYCLE_PREFIX = ("Rue ", "Avenue ", "Boulevard ", "Place ", "Allée ", "Mail ", "Promenade ",
                "Tunnel ", "Cours ", "Quai ", "Square ", "Impasse ", "Passage ", "Villa ",
                "Carrefour ", "Sentier ", "Esplanade ")

def keep_cycleway(nm, road_names, road_norms):
    """Decide whether a highway=cycleway way named nm is worth keeping. Kept when it is explicitly
    whitelisted or looks like a genuine standalone street (street-type prefix and not a
    near-duplicate/typo of an existing road name). We deliberately do NOT keep every cycleway that
    merely shares a road's name — those are mostly separated cycle tracks running parallel to the
    road for kilometres (Rivoli, Voltaire, ...), which would duplicate geometry and spawn slivers."""
    if nm in CYCLE_KEEP: return True
    if nm in road_names: return False             # same name as a real road -> parallel track, skip
    if not nm.startswith(CYCLE_PREFIX): return False
    if nm.startswith("Voie ") and "/" in nm: return False
    n = norm_name(nm)
    return not any(dl1(n, rn) for rn in road_norms)

def norm_name(s):
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c) and c.isalnum())

def main():
    d = fetch()
    coord = {}; ways = []
    def base_ok(hw, nm, t):                       # shared non-highway filters
        if not nm: return False
        if t.get("footway") in {"sidewalk", "crossing", "traffic_island", "link"}: return False
        if nm.startswith("Voie ") and "/" in nm: return False   # internal Paris code names
        if "parking" in nm.lower(): return False                # parking ramps/aisles, not real streets
        return True
    # first pass: names of kept non-cycleway roads (for the cycleway continue/near-dup test)
    road_names = set()
    for e in d["elements"]:
        if e["type"] == "way":
            t = e["tags"]; hw = t.get("highway"); nm = t.get("name", "")
            if hw not in EXCLUDE_HW and base_ok(hw, nm, t): road_names.add(nm)
    road_norms = {norm_name(nm) for nm in road_names}
    for e in d["elements"]:
        if e["type"] == "node":
            coord[e["id"]] = (e["lat"], e["lon"])
        elif e["type"] == "way":
            t = e["tags"]; hw = t.get("highway"); nm = t.get("name", "")
            if not base_ok(hw, nm, t): continue
            if hw in EXCLUDE_HW and not (hw == "cycleway" and keep_cycleway(nm, road_names, road_norms)):
                continue
            ways.append((nm, [n for n in e["nodes"] if n in coord]))

    # planarize + merge near-coincident junctions in one step: node all crossings/T-junctions,
    # snap junctions within 6 m together (dual carriageways, staggered junctions), then re-node so
    # the result is guaranteed planar. Interior shape vertices are left untouched.
    ways, coord = planarize(ways, coord)

    # group ways by name, split each group into connected components (shared node ids) -> street edges
    edges = []  # each: {name, ways:[[nodeid,...]], nodeset}
    byname = collections.defaultdict(list)
    for nm, nds in ways:
        if len(nds) >= 2: byname[nm].append(nds)
    for nm, group in byname.items():
        parent = list(range(len(group)))
        def find(x):
            while parent[x] != x: parent[x] = parent[parent[x]]; x = parent[x]
            return x
        seen = {}
        for i, nds in enumerate(group):
            for n in nds:
                if n in seen: parent[find(i)] = find(seen[n])
                else: seen[n] = i
        comp = collections.defaultdict(list)
        for i, nds in enumerate(group): comp[find(i)].append(nds)
        for ws in comp.values():
            edges.append({"name": nm, "ways": ws, "nodeset": set().union(*map(set, ws))})

    # intersections = nodes belonging to >=2 distinct edges
    use = collections.Counter()
    for ed in edges:
        for n in ed["nodeset"]: use[n] += 1
    inter = {n for n, c in use.items() if c >= 2}

    # edge graph: edges connected if they share an intersection node; keep largest component
    node_edges = collections.defaultdict(list)
    for ei, ed in enumerate(edges):
        for n in ed["nodeset"] & inter: node_edges[n].append(ei)
    adj = collections.defaultdict(set)
    for es in node_edges.values():
        for a in es:
            for b in es:
                if a != b: adj[a].add(b)
    seen = set(); best = []
    for s in range(len(edges)):
        if s in seen: continue
        stack = [s]; comp = []
        while stack:
            x = stack.pop()
            if x in seen: continue
            seen.add(x); comp.append(x)
            stack.extend(adj[x] - seen)
        if len(comp) > len(best): best = comp
    keep_list = best                        # stable order -> output edge index
    outidx = {ei: k for k, ei in enumerate(keep_list)}

    # compact node indices for intersections that survive
    nidx = {}; nodes = []
    for ei in keep_list:
        for n in edges[ei]["nodeset"] & inter:
            if n not in nidx: nidx[n] = len(nodes); nodes.append([round(coord[n][0], 5), round(coord[n][1], 5)])

    out_edges = []; total_km = 0
    for ei in keep_list:
        ed = edges[ei]
        km = sum(haversine(coord[a], coord[b]) for w in ed["ways"] for a, b in zip(w, w[1:])) / 1000
        total_km += km
        paths = [[[round(p[0], 5), round(p[1], 5)] for p in
                  dp_simplify([coord[n] for n in w])] for w in ed["ways"]]
        inodes = sorted({nidx[n] for n in ed["nodeset"] & inter})
        out_edges.append({"n": ed["name"], "k": round(km, 3), "i": inodes, "p": paths})

    faces = build_faces(edges, keep_list, outidx, inter, coord)

    # assign each street + block to an arrondissement (by representative point)
    arr = arr_polys()
    arr_labels = [lab for lab, _ in arr]
    def which_arr(lat, lon):
        for k, (lab, ring) in enumerate(arr):
            if pt_in_ring(lat, lon, ring): return k
        return -1
    for e in out_edges:
        pt = e["p"][0][len(e["p"][0]) // 2]
        e["a"] = which_arr(pt[0], pt[1])
    for f in faces:
        cx = sum(p[0] for p in f["p"]) / len(f["p"]); cy = sum(p[1] for p in f["p"]) / len(f["p"])
        f["a"] = which_arr(cx, cy)

    data = {"nodes": nodes, "edges": out_edges, "faces": faces, "arr": arr_labels}
    with open(OUT, "w") as f:                # JS assignment so the page works from file://
        f.write("window.PARIS=")
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    unassigned = sum(1 for e in out_edges if e["a"] < 0)
    print(f"edges(streets)={len(out_edges)}  intersections={len(nodes)}  "
          f"faces(blocks)={len(faces)}  arrondissements={len(arr_labels)}  "
          f"unassigned_edges={unassigned}  total_km={total_km:.1f}", file=sys.stderr)
    print(f"{OUT} size={os.path.getsize(OUT)/1e6:.2f} MB", file=sys.stderr)

if __name__ == "__main__":
    main()
