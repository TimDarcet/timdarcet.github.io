#!/usr/bin/env python3
"""Shared text formatting for the lore datasets (plaques.js, sucettes.js). One ordered pipeline:
date ranges -> spacing -> ellipsis -> quotes -> accent restoration -> sentence-start capital.

Accent restoration (the hard part) uses Lexique 3.83 (lexique.org) for word VALIDITY and frequency,
plus the dataset's own accented text for domain form-choice:
  * never re-accent a word that is already a valid French word (so 'sur','cote','tombe' are kept),
  * for an invalid bare word, prefer the accented form our own corpus uses (so 'arrete'->'arrete'
    the plaque sense 'arreted'), else Lexique's dominant form (recovers the rare tail 'cedes'->'cedes').
Guards: English words (bilingual plaques), a small proper-noun keep-list (name/toponym collisions such
as 'londres'->'londres' the cigar), single-letter tokens, and a determiner/pronoun-aware 'tombe' rule
(participle 'tombe' vs grave/present 'tombe'). Callers pass a reaccenter from make_reaccent()."""
import re, os, sys, unicodedata, collections, urllib.request

LEXIQUE_URL = "http://www.lexique.org/databases/Lexique383/Lexique383.tsv"
ACCENTS = "àâäéèêëîïôöùûüÿçœæ"
_strip = lambda w: "".join(c for c in unicodedata.normalize("NFD", w) if not unicodedata.combining(c))
_hasacc = lambda w: any(c in ACCENTS for c in w)

def frac_acc(s):                                  # share of vowels that carry an accent (accent-density proxy)
    v = [c for c in s.lower() if c in "aeiouàâäéèêëîïôöùûüÿ"]
    return sum(c in ACCENTS for c in s.lower()) / max(1, len(v))

def load_lexique(path):
    """Return (VALID, ACC, FREQ): valid orthos w/ freq, strip-key -> accented forms w/ freq, freq per ortho.
    Fetches Lexique383 to `path` if absent (a re-fetchable build input, like paris_raw.json)."""
    if not os.path.exists(path):
        print("fetching Lexique383 ...", file=sys.stderr)
        urllib.request.urlretrieve(LEXIQUE_URL, path)
    VALID, ACC, FREQ = collections.Counter(), collections.defaultdict(collections.Counter), collections.Counter()
    for i, line in enumerate(open(path, encoding="utf-8")):
        if i == 0: continue
        c = line.split("\t")
        if len(c) < 10 or not re.fullmatch(r"[a-zà-ÿ'-]+", c[0].lower() or ""): continue
        o = c[0].lower(); f = (float(c[9]) if c[9] else 0) + (float(c[8]) if c[8] else 0)
        VALID[o] += f; FREQ[o] += f
        if _hasacc(o): ACC[_strip(o)][o] += f
    return VALID, ACC, FREQ

def domain_forms(texts):                          # strip-key -> accented forms actually used in our own texts
    D = collections.defaultdict(collections.Counter)
    for s in texts:
        for w in re.findall(r"[A-Za-zÀ-ÿ]+", s or ""):
            if _hasacc(w.lower()): D[_strip(w.lower())][w.lower()] += 1
    return D

ENGLISH = set("the here there was were are been his her its our your their this that with from during who which "
              "what when where world war house born died prince passed away american british english for and".split())
NAMES = {"denis", "londres", "malte", "honorat", "lappe"}   # proper nouns that collide with a rarer accented word
OVERRIDE = {"reside": "résidé"}                              # plaque-domain participle Lexique frequency gets wrong
_mc = lambda o, r: r.upper() if o.isupper() else (r[:1].upper() + r[1:] if o[:1].isupper() else r)

def make_reaccent(lex, domain, log=None):
    """Build a word-level reaccenter. `log` (optional Counter) records applied (bare -> accented) mappings."""
    VALID, ACC, FREQ = lex
    def reacc(w):
        wl = w.lower()
        if len(wl) <= 1 or _hasacc(wl) or wl in ENGLISH or wl in NAMES: return w
        if wl in OVERRIDE: r = OVERRIDE[wl]
        elif wl in VALID: return w                                   # a real French word -> keep
        else:
            r = None
            d = domain.get(wl)
            if d:
                top, n = d.most_common(1)[0]
                if n >= 3 and n / sum(d.values()) >= 0.8 and top != wl: r = top
            if r is None:
                f = ACC.get(wl)
                if f:
                    top, tf = f.most_common(1)[0]
                    if top != wl and tf / sum(f.values()) >= 0.6 and FREQ.get(top, 0) >= 0.05: r = top
            if r is None: return w
        if log is not None: log[(wl, r)] += 1
        return _mc(w, r)
    return reacc

_KEEP = {"la","sa","une","cette","leur","leurs","ma","ta","notre","votre","ne",     # determiners -> grave (noun)
         "il","elle","on","qui","y","se","nuit","jour","soir"}                       # subjects/context -> present tense
def _dates(s):
    s = re.sub(r"\b(\d{4})\s*[-–]\s*(\d{4})\b", r"\1–\2", s)
    s = re.sub(r"\b(\d{4})\.(\d{4})\b", r"\1–\2", s)
    return re.sub(r"\b(\d{4}) \. (\d{4})\b", r"\1–\2", s)          # NOT "1985. 1990" (sentence boundary)
def _spacing(s):
    s = re.sub(r"\s+([,.)])", r"\1", s)                            # drop space before , . )  (French keeps it for ; : ! ?)
    s = re.sub(r"([a-zà-ÿ]{2}[.,])([A-Za-zÀ-ÿ])", r"\1 \2", s)     # add missing space after mid-word . ,
    return " ".join(s.split())
def _ellipsis(s):
    s = re.sub(r"(?:\.|…)(?:[ \t]*(?:\.|…))+", "…", s)             # collapse 2+ dot/ellipsis runs
    s = re.sub(r"[ \t]*…[ \t]*", "…", s)
    return re.sub(r"…(?=[0-9A-Za-zÀ-ÿ«\"“])", "… ", s)             # single space only before following content
def _quotes(s):
    s = re.sub(r'"{2,}', '"', s)
    return re.sub(r"(?<=\w)'{2,}(?=\w)", "'", s)
def _tombe(s):                                                     # 'tombe(s)' -> participle 'tombé(s)' unless noun/present
    return re.sub(r"([A-Za-zÀ-ÿ']+)(\s+)tombe(s?)\b",
                  lambda m: m.group(0) if m.group(1).lower() in _KEEP else m.group(1)+m.group(2)+"tombé"+m.group(3), s)
def _cap(s): return re.sub(r"^\s*([a-zà-ÿ])", lambda m: m.group(1).upper(), s)

def format_text(s, reacc):
    if not s: return s
    s = _dates(s); s = _spacing(s); s = _ellipsis(s); s = _quotes(s)
    s = _tombe(re.sub(r"[A-Za-zÀ-ÿ]+", lambda m: reacc(m.group()), s))   # accents (ungated), then tombe context
    return _cap(s)
