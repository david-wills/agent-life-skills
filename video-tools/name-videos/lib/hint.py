"""Recover a usable title from a raw export filename.

Cutdowns frequently open mid-segment and have no title card, but their export
filename still carries the topic (WHR_Mayan_Life_SPOTLIGHT -> "Mayan Life").
"""
import re

SERIES = ["SPOTLIGHT","HOSTED","TRENDING","BRAND","MEME","IIWW","NBK","WHF","WHR","WH","RWR","RW","NS"]

JUNK_TOKEN = re.compile(
    r'\b(SPOTLIGHT|H264|SC|NEW|FINAL|ACTUAL|ORIGINAL|LOOPED|REVERSE|COPY|'
    r'WATERMARK|REMOVED|MP4|MOV)\b', re.I)
JUNK_PAT = [
    re.compile(r'\bv\d+\b', re.I),          # v1, v2
    re.compile(r'\bp\d\b', re.I),           # stray .p4
    re.compile(r'\b\d{5,}\b'),              # 121225 date stamps
    re.compile(r'\(\d{1,2}\)'),             # (2)
]
SERIES_HEAD = re.compile(r'^\s*(%s)\s*\d{0,4}\s*(?:[-–:]+\s*|\s+)' % "|".join(SERIES), re.I)
CODE_HEAD   = re.compile(r'^\s*[A-Za-z]{2,4}\s*\d{2,4}\s*(?:[-–:]+\s*|\s+|(?=[A-Z]))')
SMALL = {"a","an","and","as","at","but","by","for","in","nor","of","on","or",
         "the","to","up","vs","via","with","from","is","its"}
ACRONYM = {"TV","MCU","SNL","GOT","AI","US","UK","NBA","NFL","HBO","DC","UFC","FBI","CIA"}

def _title_word(w, first_or_last, shouty=False):
    if w.upper() in ACRONYM:            return w.upper()
    if not shouty and re.fullmatch(r"[A-Z0-9&'.]{2,5}", w) and not w.isdigit():
        return w                         # keep a genuine acronym as written
    parts = re.split(r'([-/])', w.lower())
    out = []
    for p in parts:
        if p in "-/": out.append(p)
        elif p and (first_or_last or len(parts) > 1 or p not in SMALL):
            out.append(p[:1].upper()+p[1:])
        elif p: out.append(p)
    s = "".join(out)
    return s

def title_case(s):
    ws = s.split()
    shouty = not re.search(r'[a-z]', s)   # whole name is caps -> not acronyms
    return " ".join(_title_word(w, i in (0, len(ws)-1), shouty) for i, w in enumerate(ws))

def filename_hint(filename):
    s = re.sub(r'\.[A-Za-z0-9]{2,4}$', '', filename)   # extension
    s = re.sub(r'[_]+', ' ', s)                        # separators FIRST
    s = JUNK_TOKEN.sub(' ', s)
    for rx in JUNK_PAT: s = rx.sub(' ', s)
    s = re.sub(r'\s+', ' ', s).strip(" -–.")
    for _ in range(3):                                 # e.g. "WHR - WH014 - Anastasia"
        new = CODE_HEAD.sub('', SERIES_HEAD.sub('', s)).strip(" -–.")
        if new == s: break
        s = new
    s = re.sub(r'\s*[-–]\s*$', '', s)
    s = re.sub(r'\s+', ' ', s).strip(" -–.")
    return title_case(s) if s else None

if __name__ == "__main__":
    import sys
    for line in sys.stdin:
        fn = line.strip()
        if fn: print(f"{fn[:54]:56} -> {filename_hint(fn)}")
