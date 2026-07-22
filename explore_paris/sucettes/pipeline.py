#!/usr/bin/env python3
"""Pipeline to recover the text of Paris' "Histoire de Paris" panels (Starck sucettes).

Stages (each caches raw fetches in cache/ and writes an intermediate JSON in data/):
  1 osm    — panels from OpenStreetMap: coords, title, transcribed inscription where present, photo link
  2 rohee  — transcriptions scraped from the 2012 claude.rohee.com catalogue (archived on the Wayback Machine)
  3 ocr    — OCR (French Tesseract) of panel photos from Wikimedia Commons, for panels still missing text
  4 merge  — unify all sources by location, resolve conflicts by confidence, format -> sucettes.js (+ review json)

Run:  .ign-venv/bin/python sucettes/pipeline.py <stage|all>
Network uses HTTPS_PROXY if set (the sandbox relay); on a normal machine it is direct. Re-runs reuse cache/."""
import os, sys, json, time, re, subprocess, urllib.request, urllib.parse, pathlib, unicodedata

HERE = pathlib.Path(__file__).parent
CACHE, DATA, TESS = HERE/"cache", HERE/"data", HERE/"tessdata"
CACHE.mkdir(exist_ok=True); DATA.mkdir(exist_ok=True)
BBOX = (48.81, 2.22, 48.91, 2.47)                 # Paris + margin (S,W,N,E)
PARIS_JS = HERE.parent/"paris.js"

PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
def http(url, timeout=90, data=None):             # shell out to curl: the sandbox permits curl's network but not python's sockets.
    cmd = ["curl", "-sS", "-L", "--retry", "6", "--retry-all-errors", "--retry-connrefused", "--retry-delay", "2",
           "--max-time", str(timeout), "-A", "paris-sucettes/1.0 (game lore)"]   # --retry rides out the flaky egress
    if PROXY: cmd += ["-x", PROXY]
    if data is not None: cmd += ["--data-binary", "@-"]
    cmd += [url]
    p = subprocess.run(cmd, input=data if data is not None else None, capture_output=True)
    if p.returncode != 0: raise RuntimeError(f"curl {p.returncode}: {p.stderr.decode('utf-8','replace')[:140]}")
    return p.stdout

def cached(key, fn):                              # persistent raw-bytes cache so re-runs need no network
    p = CACHE/key
    if p.exists() and p.stat().st_size: return p.read_bytes()
    b = fn(); p.write_bytes(b); return b

OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter"]
def overpass(query, key):
    def fetch():
        for attempt in range(8):
            ep = OVERPASS[attempt % len(OVERPASS)]
            try:
                raw = http(ep, timeout=120, data=urllib.parse.urlencode({"data": query}).encode())
                if raw[:1] in (b"{", b"["): return raw
                print(f"  overpass {ep} -> {raw[:60]!r}", file=sys.stderr)
            except Exception as e:
                print(f"  overpass {ep} error: {e}", file=sys.stderr)
            time.sleep(3)
        raise RuntimeError("all overpass endpoints failed (relay down?)")
    return json.loads(cached(key, fetch))

# ---- shared text formatting -------------------------------------------------
def fix_mojibake(s):                              # repair UTF-8 mistakenly decoded as cp1252/latin-1 ("Ã©"->"é", "Ã‰"->"É")
    if re.search(r"Ã|Â|â€", s):
        for enc in ("cp1252", "latin-1"):
            try:
                fixed = s.encode(enc).decode("utf-8")
                if "Ã" not in fixed and "Â" not in fixed: return fixed
            except Exception: pass
    return s
def norm_ws(s):
    s = s.replace(" ", " ").replace("\r", "\n")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()
def dehyphenate(s):                              # join words split by an end-of-line hyphen ("cons-\ntruit" -> "construit")
    return re.sub(r"(\w)-\n(\w)", r"\1\2", s)
def recap(s):                                    # SHOUTING CAPS -> sentence case; leave already-mixed text alone
    letters = [c for c in s if c.isalpha()]
    if not letters or sum(c.isupper() for c in letters) / len(letters) <= 0.7: return s
    out, cap = [], True
    for ch in s.lower():
        if cap and ch.isalpha(): out.append(ch.upper()); cap = False
        else:
            out.append(ch); cap = ch in ".!?…"
    return "".join(out)
def clean(s):
    if not s: return ""
    return recap(norm_ws(dehyphenate(fix_mojibake(s)))).strip()
def norm_key(s):                                 # loose key for matching titles across sources
    s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()

# ---- stage 1: OSM -----------------------------------------------------------
def osm_text(t):
    return "\n".join(t[k] for k in ("inscription","inscription:2","inscription:3","inscription:4","inscription:5") if t.get(k)).strip()
def is_starck(title, operator):                  # the Starck "Histoire de Paris" set: city-operated history boards
    return "de paris" in operator.lower() or title.lower().startswith("histoire de paris")
def stage_osm():
    q = (f'[out:json][timeout:90];(node["board_type"="history"]({",".join(map(str,BBOX))});'
         f' way["board_type"="history"]({",".join(map(str,BBOX))}););out center;')
    d = overpass(q, "osm_history.json")
    out = []
    for e in d["elements"]:
        t = e.get("tags", {}); lat = e.get("lat") or (e.get("center") or {}).get("lat"); lon = e.get("lon") or (e.get("center") or {}).get("lon")
        if lat is None: continue
        out.append({"osm": f'{e["type"]}/{e["id"]}', "lat": round(lat,6), "lon": round(lon,6),
                    "title": clean(t.get("name") or t.get("board:title") or ""),
                    "subject": (t.get("description") or "").strip(), "text": clean(osm_text(t)),
                    "commons": t.get("wikimedia_commons",""), "image": t.get("image",""),
                    "wikidata": t.get("wikidata",""), "operator": t.get("operator","")})
        out[-1]["starck"] = is_starck(out[-1]["title"], out[-1]["operator"])
    json.dump(out, open(DATA/"osm.json","w"), ensure_ascii=False, indent=1)
    st = [p for p in out if p["starck"]]
    print(f"stage osm: {len(out)} boards | {len(st)} Starck | {sum(1 for p in st if p['text'])} with text | "
          f"{sum(1 for p in st if p['commons'] or p['image'])} with photo", file=sys.stderr)

# ---- stage 2: rohee 2012 catalogue (Wayback) --------------------------------
WB = "https://web.archive.org/web/2012id_/http://claude.rohee.com/"
def wb_page(path):                              # fetch an archived page, decoded from its legacy cp1252
    raw = cached("rohee_" + re.sub(r"[^a-z0-9]+","_",path.lower()), lambda: http(WB+path, timeout=45))
    return raw.decode("cp1252", "replace")
def wb_text(html):
    return norm_ws(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)))
def stage_rohee():
    idx = wb_page("paris.htm")
    listpages = sorted(set(re.findall(r'href="((?:paris/)?[^"]*?(?:_liste|/\d\d)[^"]*\.htm)"', idx)))
    if not listpages: listpages = sorted(set(re.findall(r'href="(paris/[^"]+\.htm)"', idx)))
    panels = []
    for lp in listpages:
        try: html = wb_page(lp)
        except Exception as e: print(f"  rohee list {lp}: {e}", file=sys.stderr); continue
        base = lp.rsplit("/",1)[0]+"/" if "/" in lp else ""
        for href in re.findall(r'href="([^"]+\.htm)"', html):
            if "liste" in href or href.startswith("http") or href.startswith("#"): continue
            panelpath = href if href.startswith("paris/") else base+href
            try: ph = wb_page(panelpath)
            except Exception: continue
            body = wb_text(ph)
            m = re.search(r"Histoire de Paris\s+(.*)", body)      # panel body follows the "Histoire de Paris <title>" header
            txt = clean(m.group(1) if m else "")
            txt = re.sub(r"(?i)\bpelle n.?\s*\d+.*$", "", txt).strip()
            txt = re.sub(r"(?i)mise . jour.*$", "", txt).strip()
            if len(txt) >= 60:
                title = clean(re.sub(r"\s+Histoire de Paris.*", "", body)[:80])
                panels.append({"path": panelpath, "title": title, "text": txt})
    json.dump(panels, open(DATA/"rohee.json","w"), ensure_ascii=False, indent=1)
    print(f"stage rohee: {len(listpages)} list pages -> {len(panels)} panels with text", file=sys.stderr)

# ---- stage 3: OCR panel photos ----------------------------------------------
# Only top-level curl has network in this sandbox, so ocr_list emits a download list, a shell curl
# loop fills cache/img_<key>.bin (Commons Special:FilePath redirects straight to the file), then ocr
# reads those images offline. On a normal machine, `fetch_images` can be driven by curl or extended.
def ocr_targets(osm): return [p for p in osm if p["starck"] and not p["text"] and (p["commons"] or p["image"])]
def img_key(p): return re.sub(r"[^a-z0-9]+", "_", p["osm"].lower())
def img_url(p):
    if p["image"].startswith("http"): return p["image"]
    f = p["commons"].split(";")[0]                # take the first file if several are listed
    if f.startswith("File:"): f = f[5:]
    return "https://commons.wikimedia.org/wiki/Special:FilePath/" + urllib.parse.quote(f) + "?width=1600"
def stage_ocr_list():
    osm = json.load(open(DATA/"osm.json")); rows = []
    for p in ocr_targets(osm):
        k = img_key(p)
        if not (CACHE/f"img_{k}.bin").exists() or not (CACHE/f"img_{k}.bin").stat().st_size:
            rows.append(f"{k}\t{img_url(p)}")
    (DATA/"ocr_fetch.tsv").write_text("\n".join(rows))
    print(f"ocr_list: {len(rows)} images still to download -> data/ocr_fetch.tsv", file=sys.stderr)
def _tess(png, psm):
    env = {**os.environ, "TESSDATA_PREFIX": str(TESS)}
    return subprocess.run(["tesseract","stdin","stdout","-l","fra","--psm",str(psm)], input=png,
                          capture_output=True, env=env).stdout.decode("utf-8","replace")
def _frenchness(t):                              # proxy for real French text: count of vowel-bearing words len>=3
    return sum(1 for w in re.findall(r"[A-Za-zÀ-ÿ]{3,}", t) if re.search(r"[aeiouyàâéèêîïôûüAEIOUY]", w))
FRSHORT = {"à","la","le","de","du","des","en","un","une","et","au","aux","ce","ces","se","sa","son","est",
           "ne","par","sur","qui","que","les","il","on","ou","où"}
def _isword(w):                                 # a real French word, a common short word, or an all-caps acronym (SNCF, RATP)
    core = w.strip(".,;:!?\"'()[]«»"); wl = core.lower()
    if (len(wl) >= 3 and bool(re.search(r"[aeiouyàâéèêîïôûü]", wl))) or wl in FRSHORT: return True
    return 2 <= len(core) <= 5 and core.isalpha() and core.isupper()
def trim_edges(line):                           # drop stray edge tokens (fence/panel-border fragments) from a line's ends
    toks = line.split(); i, j = 0, len(toks)
    while i < j and not _isword(toks[i]): i += 1
    while j > i and not _isword(toks[j-1]): j -= 1
    return " ".join(toks[i:j])
def ocr_clean(t):                               # drop OCR noise lines (fence bars, graffiti, the red header) around the body
    keep = []
    for line in t.splitlines():
        line = trim_edges(line.strip())
        if not line or re.search(r"histoire\s+de\s+paris", line, re.I): continue   # skip the panel's title band
        nonspace = sum(not c.isspace() for c in line); letters = sum(c.isalpha() for c in line)
        lower = sum(c.islower() for c in line)
        vowelwords = sum(1 for w in re.findall(r"[A-Za-zÀ-ÿ]{3,}", line) if re.search(r"[aeiouyàâéèêîïôûü]", w, re.I))
        # real French prose lines: dense with letters, mostly lowercase, several vowel-bearing words
        if nonspace and letters/nonspace >= 0.6 and vowelwords >= 2 and letters and lower/letters >= 0.55:
            keep.append(line)
    return "\n".join(keep)
def _widest_run(on):                            # longest contiguous True run in a bool array -> (start,end)
    runs=[]; s=None
    for j,v in enumerate(on):
        if v and s is None: s=j
        if not v and s is not None: runs.append((s,j)); s=None
    if s is not None: runs.append((s,len(on)))
    return max(runs, key=lambda r: r[1]-r[0]) if runs else (0,len(on))
def crop_panel(g):                              # crop horizontally to the panel (widest contiguous dark column band), full height
    import numpy as np
    a = np.asarray(g); cd = (a < 100).mean(0)
    x0, x1 = _widest_run(cd > max(0.30, cd.max()*0.5))
    if (x1-x0) < a.shape[1]*0.2: return g       # detection failed -> keep the whole image
    pad = int((x1-x0)*0.07); return g.crop((max(0,x0-pad), 0, min(a.shape[1],x1+pad), a.shape[0]))
def ocr_file(path):
    # Starck panels are light text on a dark panel photographed in the street: crop to the panel to drop
    # the fence/sky/road, then feed tesseract the INVERTED image (and normal, several PSMs) and keep the
    # variant that reads most like French.
    from PIL import Image, ImageOps
    import io
    g = crop_panel(Image.open(path).convert("L"))
    if max(g.size) < 2200: r = 2200/max(g.size); g = g.resize((int(g.width*r), int(g.height*r)))
    base = ImageOps.autocontrast(g, cutoff=1)
    best = ""
    for variant in (ImageOps.invert(base), base):    # inverted first (the common case); try normal only if inverted read poorly
        b = io.BytesIO(); variant.save(b, "PNG"); png = b.getvalue()
        for psm in (6, 4):
            t = _tess(png, psm)
            if _frenchness(t) > _frenchness(best): best = t
        if _frenchness(best) >= 25: break            # good enough -> don't bother with the other polarity
    return best
def stage_ocr():
    # raw tesseract output is cached per image (ocrraw_*.txt) so tweaking the text cleaning is instant;
    # only newly downloaded images actually run the (slow) OCR.
    osm = json.load(open(DATA/"osm.json")); out = {}; done = 0
    for p in ocr_targets(osm):
        f = CACHE/f"img_{img_key(p)}.bin"
        if not f.exists() or not f.stat().st_size: continue
        done += 1
        rawp = CACHE/f"ocrraw_{img_key(p)}.txt"
        try:
            raw = rawp.read_text(encoding="utf-8") if rawp.exists() else ocr_file(f)
            if not rawp.exists(): rawp.write_text(raw, encoding="utf-8")
            txt = clean(ocr_clean(raw))
            if len(txt) >= 40: out[p["osm"]] = {"text": txt}
        except Exception as e: print(f"  ocr {p['osm']}: {e}", file=sys.stderr)
    json.dump(out, open(DATA/"ocr.json","w"), ensure_ascii=False, indent=1)
    print(f"stage ocr: {done} cached images -> {len(out)} OCR'd with usable text", file=sys.stderr)

# ---- stage 4: merge + attach to street + format -----------------------------
def load_edges():
    src = open(PARIS_JS, encoding="utf-8").read()
    return json.loads(src.split("=",1)[1].strip().rstrip(";"))["edges"]
def nearest_edges(pts):                          # pts=[(lat,lon)] -> [edge index]; shapely STRtree over paris.js
    from shapely import STRtree; from shapely.geometry import Point, LineString, MultiLineString
    edges = load_edges()
    geoms = [MultiLineString([[(lo,la) for la,lo in path] for path in e["p"] if len(path)>=2])
             if len(e["p"])>1 else LineString([(lo,la) for la,lo in e["p"][0]]) for e in edges]
    tree = STRtree(geoms)
    return [int(tree.nearest(Point(lo,la))) for la,lo in pts]
def stage_merge():
    osm = json.load(open(DATA/"osm.json"))
    rohee = json.load(open(DATA/"rohee.json")) if (DATA/"rohee.json").exists() else []
    ocr = json.load(open(DATA/"ocr.json")) if (DATA/"ocr.json").exists() else {}
    rohee_by = {}
    for r in rohee: rohee_by.setdefault(norm_key(r["title"]), r["text"])
    panels = [p for p in osm if p["starck"]]
    ORDER = {"osm": 0, "rohee": 1, "ocr": 2}      # human-curated sources outrank machine OCR
    def toks(s): return set(re.findall(r"[a-zà-ÿ]{4,}", s.lower()))
    review, final = [], []
    for p in panels:
        cand = {}                                # source -> text
        if p["text"]: cand["osm"] = p["text"]
        rk = norm_key(p["title"])
        if rk and rk in rohee_by: cand["rohee"] = rohee_by[rk]
        if p["osm"] in ocr: cand["ocr"] = ocr[p["osm"]]["text"]
        # pick the most-trusted source that isn't obviously truncated; fall back to the longest
        pick = next((s for s in sorted(cand, key=lambda s: ORDER[s]) if len(cand[s]) >= 80), None) \
               or (max(cand, key=lambda s: len(cand[s])) if cand else None)
        # confidence: two independent sources sharing most words is a strong signal
        conf = "none"
        if pick == "osm" or pick == "rohee": conf = "high"
        elif pick: conf = "low"
        if pick and len(cand) >= 2:
            others = [toks(cand[s]) for s in cand if s != pick]
            base = toks(cand[pick])
            if base and any(len(base & o)/len(base) >= 0.5 for o in others): conf = "high"
        review.append({**{k: p[k] for k in ("osm","title","lat","lon")}, "sources": cand, "chosen": pick, "conf": conf})
        if pick: final.append({"lat":p["lat"],"lon":p["lon"],"t":p["title"],"r":cand[pick],"src":pick,"conf":conf})
    if final:
        eis = nearest_edges([(f["lat"],f["lon"]) for f in final])
        for f,ei in zip(final, eis): f["ei"]=ei
    out = [{"la":round(f["lat"],5),"lo":round(f["lon"],5),"t":f["t"],"r":f["r"],"ei":f["ei"],"src":f["src"],"conf":f["conf"]} for f in final]
    json.dump(review, open(DATA/"review.json","w"), ensure_ascii=False, indent=1)
    with open(HERE.parent/"sucettes.js","w",encoding="utf-8") as fh:
        fh.write("window.SUCETTES="); json.dump(out, fh, ensure_ascii=False, separators=(",",":"))
    bysrc = {};
    for f in final: bysrc[f["src"]] = bysrc.get(f["src"],0)+1
    print(f"stage merge: {len(panels)} panels | {len(out)} with text {bysrc} -> sucettes.js", file=sys.stderr)

STAGES = {"osm": stage_osm, "rohee": stage_rohee, "ocr_list": stage_ocr_list, "ocr": stage_ocr, "merge": stage_merge}
def main(which):
    for name in (list(STAGES) if which=="all" else [which]):
        if name not in STAGES: sys.exit(f"unknown stage {name}; have {list(STAGES)} or all")
        print(f"== {name} ==", file=sys.stderr); STAGES[name]()

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "all")
