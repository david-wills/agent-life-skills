#!/usr/bin/env python3
"""Probe videos locally: title-band OCR + talking-head detection + first-clip frame.

Everything here runs on-device (ffmpeg + Apple Vision). No network, no tokens.
Emits JSON on stdout; writes first-clip JPEGs into --outdir.

Usage:
  probe.py --outdir <scratch>/clips "<file>" ["<file>" ...]
  probe.py --outdir <scratch>/clips --from-file paths.txt     # one path per line

Needs ffmpeg on PATH and the pyobjc Vision/Quartz frameworks (requirements.txt);
both are checked when a video is probed, not at import, so --help works anywhere.
"""
import sys, os, re, json, argparse, subprocess, tempfile, shutil, difflib
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hint import filename_hint

SECS, FPS, OCR_W, CLIP_W = 20, 1, 720, 480
CLIP_SECS = 6            # stills for the first-clip frame; the third one (~3s) is kept

def _frameworks():
    """Import the Apple frameworks lazily: macOS-only and only needed to probe."""
    try:
        import Vision, Quartz
        from Foundation import NSURL
    except ImportError as e:
        raise SystemExit("probe.py needs the pyobjc Vision/Quartz frameworks: "
                         "pip install -r requirements.txt (macOS only)") from e
    return Vision, Quartz, NSURL

def _cg(path):
    _, Quartz, NSURL = _frameworks()
    src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(path), None)
    return Quartz.CGImageSourceCreateImageAtIndex(src, 0, None) if src else None

def _run(req, img):
    Vision, _, _ = _frameworks()
    Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
        img, None).performRequests_error_([req], None)

def ocr(path):
    Vision, _, _ = _frameworks()
    img = _cg(path)
    if img is None: return []
    out = []
    def h(req, err):
        for r in (req.results() or []):
            c = r.topCandidates_(1)[0]; bb = r.boundingBox()
            out.append({"t": c.string(), "c": float(c.confidence()),
                        "h": float(bb.size.height), "y": float(bb.origin.y),
                        "x": float(bb.origin.x)})
    _run(Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(h), img)
    return out

def faces(path):
    Vision, _, _ = _frameworks()
    img = _cg(path)
    if img is None: return []
    out = []
    def h(req, err):
        for r in (req.results() or []):
            bb = r.boundingBox()
            out.append({"area": bb.size.width*bb.size.height,
                        "cx": bb.origin.x + bb.size.width/2,
                        "cy": bb.origin.y + bb.size.height/2})
    _run(Vision.VNDetectFaceRectanglesRequest.alloc().initWithCompletionHandler_(h), img)
    return out

def require_ffmpeg():
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg not found on PATH (brew install ffmpeg)")

def _frames(video, tmp):
    """One decode, two outputs: OCR frames at 720px for SECS, clip stills at 480px for CLIP_SECS."""
    subprocess.run(["ffmpeg","-nostdin","-v","error","-y","-ss","0","-t",str(SECS),"-i",video,
                    "-vf",f"fps={FPS},scale={OCR_W}:-2","-q:v","3",os.path.join(tmp,"ocr_%03d.jpg"),
                    "-t",str(CLIP_SECS),"-vf",f"fps=1,scale={CLIP_W}:-2","-q:v","3",
                    os.path.join(tmp,"clip_%03d.jpg")], check=False)
    names = sorted(os.listdir(tmp))
    return ([os.path.join(tmp,f) for f in names if f.startswith("ocr_")],
            [os.path.join(tmp,f) for f in names if f.startswith("clip_")])

def norm(s): return re.sub(r'[^a-z0-9 ]','',s.lower()).strip()
WATERMARK = "yourbrand"   # the channel watermark OCR reads off every frame; never a title
SKIPLINE = re.compile(r'^(source[:\s]|@|\W*$|' + re.escape(WATERMARK) + r'\W*$)', re.I)

EARLY = 10   # the title band lives in the opening seconds

def title_bands(per_frame):
    """Cluster OCR lines across frames; persistent clusters are the title band."""
    n = len(per_frame) or 1
    ne = min(EARLY, n)
    clusters = []
    for fi, lines in enumerate(per_frame):
        for L in lines:
            t, nt = L["t"].strip(), norm(L["t"])
            if len(nt) < 6 or SKIPLINE.match(t): continue
            best, bs = None, 0.0
            for c in clusters:
                r = difflib.SequenceMatcher(None, nt, c["norm"]).ratio()
                if r > bs: best, bs = c, r
            if best and bs >= 0.75:
                best["frames"].add(fi); best["variants"].append(t)
                if len(nt) > len(best["norm"]): best["norm"] = nt
                best["h"] = max(best["h"], L["h"]); best["ys"].append(L["y"])
            else:
                clusters.append({"norm": nt, "frames": {fi}, "variants": [t],
                                 "h": L["h"], "ys": [L["y"]]})
    out = []
    for c in clusters:
        p  = len(c["frames"])/n
        pe = len([f for f in c["frames"] if f < EARLY])/ne
        out.append({"text": Counter(c["variants"]).most_common(1)[0][0],
                    "first_seen": min(c["frames"]),
                    "persist": round(p, 2), "early_persist": round(pe, 2),
                    "height": round(c["h"], 3),
                    "y": round(sum(c["ys"])/len(c["ys"]), 2),
                    "score": round(pe*3 + p*1.0 + min(c["h"], .12)*8, 2)})
    out.sort(key=lambda d: -d["score"])
    return out

def talking_head(face_frames):
    """A hosted video holds one face at steady size/position; montages don't."""
    n = len(face_frames) or 1
    hit = [f[0] for f in face_frames if f]
    cover = len(hit)/n
    if not hit: return {"cover": 0.0, "verdict": "no"}
    areas = sorted(f["area"] for f in hit)
    med, mx = areas[len(areas)//2], areas[-1]
    ratio = med/mx if mx else 0
    cxs = [f["cx"] for f in hit]; cys = [f["cy"] for f in hit]
    spread = max(max(cxs)-min(cxs), max(cys)-min(cys))
    # A host is on screen almost continuously at a modest framing. A clip montage
    # either loses the face between cuts (low cover) or fills the frame with a
    # celebrity close-up (large median_area).
    if   cover >= 0.90 and med <= 0.15: v = "yes"
    elif cover >= 0.75 and med <= 0.20: v = "maybe"
    else:                               v = "no"
    return {"cover": round(cover,2), "size_ratio": round(ratio,2),
            "pos_spread": round(spread,2), "median_area": round(med,4),
            "verdict": v}

def probe(video, outdir):
    require_ffmpeg()
    stem = re.sub(r'[^A-Za-z0-9]+','_', os.path.splitext(os.path.basename(video))[0])[:60]
    clip = None
    tmp = tempfile.mkdtemp()
    try:
        fs, cf = _frames(video, tmp)
        per_ocr  = [ocr(f) for f in fs]
        per_face = [faces(f) for f in fs]
        if len(cf) >= 3:
            os.makedirs(outdir, exist_ok=True)
            clip = os.path.join(outdir, f"{stem}__clip.jpg")
            shutil.copy(cf[2], clip)              # ~3s in, past the cold open
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return {"path": video, "filename": os.path.basename(video),
            "filename_hint": filename_hint(os.path.basename(video)),
            "frames_analyzed": len(fs),
            "title_bands": title_bands(per_ocr)[:8],
            "talking_head": talking_head(per_face),
            "clip_frame": clip}

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("videos", nargs="*", help="video files to probe; several per call is faster")
    ap.add_argument("--from-file", help="newline-delimited list of video paths, added to the positionals")
    ap.add_argument("--outdir", required=True, help="where the first-clip stills are written")
    a = ap.parse_args()
    vids = list(a.videos)
    if a.from_file:
        with open(a.from_file) as fh:
            vids += [l.rstrip("\n") for l in fh if l.strip()]
    if not vids:
        ap.error("no videos given (positionals or --from-file)")
    require_ffmpeg()
    _frameworks()
    print(json.dumps([probe(v, a.outdir) for v in vids], indent=1))

if __name__ == "__main__":
    main()
