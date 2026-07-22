#!/usr/bin/env python3
"""Fetch Paris' commemorative plaques (opendata.paris.fr) into a compact plaques.js for the game.
Run with the geo venv:  .ign-venv/bin/python build_plaques.py

window.PLAQUES = [{la, lo, t: title, r: text, a: address, ei: nearest edge index}, ...]
  * ei  – index of the closest street in paris.js, so the client only shows a plaque once you've
          revealed that street. Recompute (re-run) if paris.js is rebuilt, since indices shift.
  * r   – transcriptions are mostly SHOUTED IN CAPS on the real plaques; recase the predominantly
          uppercase ones to sentence case (one capital per sentence). Already-mixed text is left as-is.
Raw API response is cached to plaques_raw.json so re-runs need no network."""
import json, sys, os, urllib.request
from shapely import STRtree
from shapely.geometry import Point, LineString, MultiLineString

URL = "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets/plaques_commemoratives/exports/json"
RAW, PARIS, OUT = "plaques_raw.json", "paris.js", "plaques.js"

def fetch():
    if os.path.exists(RAW): return json.load(open(RAW, encoding="utf-8"))
    print("fetching commemorative plaques...", file=sys.stderr)
    with urllib.request.urlopen(URL, timeout=120) as r: data = json.load(r)
    json.dump(data, open(RAW, "w", encoding="utf-8"), ensure_ascii=False)
    return data

def coord(x):
    gp = x.get("geo_point_2d")
    if isinstance(gp, dict) and gp.get("lat") is not None: return float(gp["lat"]), float(gp["lon"])
    if x.get("y_4326") and x.get("x_4326"): return float(x["y_4326"]), float(x["x_4326"])
    return None

def recap(s):                                     # SHOUTING CAPS -> sentence case; leave already-mixed text alone
    letters = [c for c in s if c.isalpha()]
    if not letters or sum(c.isupper() for c in letters) / len(letters) <= 0.7: return s
    out, cap = [], True
    for ch in s.lower():
        if cap and ch.isalpha(): out.append(ch.upper()); cap = False
        else:
            out.append(ch)
            if ch in ".!?…\n": cap = True
    return "".join(out)

def edge_geoms(edges):                             # one (Multi)LineString per edge, in (lon,lat)
    geoms = []
    for e in edges:
        lines = [[(lo, la) for la, lo in path] for path in e["p"] if len(path) >= 2]
        geoms.append(MultiLineString(lines) if len(lines) > 1 else LineString(lines[0]))
    return geoms

def main():
    recs = fetch()
    edges = json.loads(open(PARIS, encoding="utf-8").read().split("=", 1)[1].strip().rstrip(";"))["edges"]
    tree = STRtree(edge_geoms(edges))
    out = []
    for x in recs:
        c = coord(x)
        if not c: continue
        t = (x.get("titre") or "").strip()
        r = recap((x.get("retranscription") or "").strip())
        if not (t or r): continue
        ei = int(tree.nearest(Point(c[1], c[0])))
        out.append({"la": round(c[0], 5), "lo": round(c[1], 5), "t": t, "r": r,
                    "a": (x.get("adresse") or "").strip(), "ei": ei})
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("window.PLAQUES="); json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"wrote {len(out)}/{len(recs)} plaques to {OUT}", file=sys.stderr)

if __name__ == "__main__":
    main()
