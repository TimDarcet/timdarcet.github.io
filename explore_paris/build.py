#!/usr/bin/env python3
"""Fetch Paris named streets from OpenStreetMap (Overpass) and emit paris.json.
Streets = edges (ways merged by name + connectivity). Intersections = nodes shared
by two different streets. Only the largest connected component is kept so 100% is reachable."""
import json, math, os, sys, urllib.request, urllib.parse, collections
sys.setrecursionlimit(100000)

AREA = 3600007444  # Paris commune boundary relation 7444
RAW = "paris_raw.json"
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
        if a < 300 or a > 6e5: continue                               # slivers / huge regions
        ring = poly.exterior
        es = []
        for q in tree.query(ring):                                    # streets whose bbox meets the block
            if edge_lines[q][1].intersection(ring.buffer(1.2)).length > 4:
                es.append(idxs[q])
        if not es: continue
        pts = [ll(x, y) for x, y in poly.exterior.coords[:-1]]
        pts = dp_simplify(pts + [pts[0]], 1.2e-5)[:-1]
        if len(pts) < 3: continue
        out_faces.append({"e": sorted(set(es)), "p": [[round(la, 5), round(lo, 5)] for la, lo in pts]})
    return out_faces

def main():
    d = fetch()
    coord = {}; ways = []
    for e in d["elements"]:
        if e["type"] == "node":
            coord[e["id"]] = (e["lat"], e["lon"])
        elif e["type"] == "way":
            hw = e["tags"].get("highway"); nm = e["tags"].get("name", "")
            if hw in EXCLUDE_HW or not nm: continue
            if e["tags"].get("footway") in {"sidewalk", "crossing", "traffic_island", "link"}: continue
            if nm.startswith("Voie ") and "/" in nm: continue  # internal Paris code names
            ways.append((nm, [n for n in e["nodes"] if n in coord]))

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

    data = {"nodes": nodes, "edges": out_edges, "faces": faces}
    with open(OUT, "w") as f:                # JS assignment so the page works from file://
        f.write("window.PARIS=")
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"edges(streets)={len(out_edges)}  intersections={len(nodes)}  "
          f"faces(blocks)={len(faces)}  total_km={total_km:.1f}", file=sys.stderr)
    print(f"{OUT} size={os.path.getsize(OUT)/1e6:.2f} MB", file=sys.stderr)

if __name__ == "__main__":
    main()
