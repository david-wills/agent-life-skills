"""Decide whether a video filename is already named per the house naming convention."""
import re, os

PREFIXES = ["SPOTLIGHT","HOSTED","TRENDING","BRAND","MEME","IIWW","NBK",
            "WHF","WHR","WH","RWR","RW","NS"]
PFX   = re.compile(r'^(%s)\b[\s\-]*' % "|".join(PREFIXES))
ANNOT = re.compile(r'^\((DO NOT RECYCLE|TOO LONG|OVER [^)]+)\)\s*', re.I)

JUNK = [
    (re.compile(r'_'),                              "underscore"),
    (re.compile(r'[-_.\s](SPOTLIGHT|Spotlight)\b'), "trailing -SPOTLIGHT"),
    (re.compile(r'\bH264\b', re.I),                 "codec tag"),
    (re.compile(r'\bv\d+\b', re.I),                 "version tag"),
    (re.compile(r'\(\d{1,2}\)'),                    "duplicate marker"),
    (re.compile(r'\b\d{5,}\b'),                     "date/id number"),
    (re.compile(r'\b[A-Z]{2,4}\d{2,}'),             "internal code"),
    (re.compile(r'\.p\d\b'),                        "broken extension"),
    (re.compile(r'^[-_.]'),                         "leading symbol"),
]

def is_named(filename):
    """-> (already_named: bool, reason: str)"""
    stem = os.path.splitext(filename)[0]
    if stem.upper().startswith("MEME"):
        return True, "MEME codename (intentional convention)"
    has_prefix = bool(PFX.match(stem))
    body = ANNOT.sub("", PFX.sub("", stem)).strip()
    for rx, why in JUNK:
        if rx.search(body):
            return False, why
    words = body.split()
    # "SPOTLIGHT Biosphere" is a complete name; a bare "Biosphere" is not.
    if len(words) < (1 if has_prefix else 2):
        return False, "too few words"
    if not re.search(r'[a-z]', body) and len(words) < 4:
        return False, "all-caps codename"
    return True, "looks like a finished title"
