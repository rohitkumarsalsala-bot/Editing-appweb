"""
Auto Director - bina AI ke smart video editor (FFmpeg + Flask)
Engine khud analyse karta hai (awaaz, motion, brightness) aur decide karta hai:
video ka type, kitna cut, kahan zoom, sound chain, background, colour, fades, music ducking.
Run: pip install flask ; python server.py   (FFmpeg chahiye)
"""
import os, re, json, uuid, time, queue, shutil, threading, subprocess, statistics, urllib.request
from pathlib import Path
from flask import Flask, request, jsonify, send_file, abort

WORK = Path(os.environ.get("WORK_DIR", "work")); WORK.mkdir(exist_ok=True)
FONT = os.environ.get("FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
KEEP = int(os.environ.get("KEEP_SECONDS", 3600))
BIN = 0.25  # analysis window (sec)
DIMS = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)}
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_MB", 800)) * 1024 * 1024
jobs, q = {}, queue.Queue()
STYLES = {
 "auto": dict(grade="", trans=["fade"]),
 "cinematic": dict(grade="colorbalance=rs=-0.05:bs=0.08:rh=0.06:bh=-0.04,eq=contrast=1.1:saturation=0.95,vignette=PI/5", trans=["fade", "circleopen"]),
 "vivid": dict(grade="eq=contrast=1.08:saturation=1.3", trans=["slideleft", "wipeleft", "fade"]),
 "warm": dict(grade="colorbalance=rs=0.06:gs=0.02:bs=-0.06:rh=0.04:bh=-0.04", trans=["fade", "dissolve"]),
 "vintage": dict(grade="curves=preset=vintage,noise=alls=8:allf=t,vignette=PI/4", trans=["fade"]),
 "bw": dict(grade="hue=s=0,eq=contrast=1.15", trans=["fade", "wipeleft"]),
}


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(p.stderr[-800:])
    return p.stdout + "\n" + p.stderr


def pct(a, p):
    s = sorted(a)
    return s[min(len(s) - 1, int(p / 100 * len(s)))] if s else 0.0


# ---------------- 1. ANALYSE ----------------
def probe(path):
    j = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]))
    v = next(s for s in j["streams"] if s["codec_type"] == "video")
    has_a = any(s["codec_type"] == "audio" for s in j["streams"])
    w, h = int(v["width"]), int(v["height"])
    rot = int(float(v.get("tags", {}).get("rotate", 0) or 0))
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    if abs(rot) % 180 == 90:
        w, h = h, w
    return float(j["format"]["duration"]), has_a, w, h


def analyze(src, dur, has_a):
    n = int(dur / BIN) + 1
    rms = [-90.0] * n
    if has_a:
        out = run(["ffmpeg", "-v", "error", "-i", str(src), "-vn", "-af",
                   "aresample=16000,asetnsamples=4000,astats=metadata=1:reset=1,"
                   "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-", "-f", "null", "-"])
        t = None
        for line in out.splitlines():
            m = re.search(r"pts_time:([\d.]+)", line)
            if m:
                t = float(m.group(1)); continue
            m = re.search(r"RMS_level=(\S+)", line)
            if m and t is not None:
                v = m.group(1)
                rms[min(n - 1, int(t / BIN))] = -90.0 if v in ("-inf", "nan") else max(-90.0, float(v))
    mot, cnt, bright, t = [0.0] * n, [0] * n, [], None
    out = run(["ffmpeg", "-v", "error", "-i", str(src), "-an", "-vf",
               "fps=4,scale=160:-2,signalstats,metadata=print:file=-", "-f", "null", "-"])
    for line in out.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            t = float(m.group(1)); continue
        if t is None:
            continue
        i = min(n - 1, int(t / BIN))
        m = re.search(r"YDIF=([\d.]+)", line)
        if m:
            mot[i] += float(m.group(1)); cnt[i] += 1
        m = re.search(r"YAVG=([\d.]+)", line)
        if m:
            bright.append(float(m.group(1)))
    mot = [a / c if c else 0.0 for a, c in zip(mot, cnt)]
    return rms, mot, bright


# ---------------- 2. DECIDE ----------------
def to_segs(mask, gap, minlen, pad, dur):
    segs, s = [], None
    for i, k in enumerate(mask + [False]):
        if k and s is None: s = i
        if not k and s is not None: segs.append([s * BIN, i * BIN]); s = None
    def merge(lst, g):
        m = []
        for a, b in lst:
            if m and a - m[-1][1] <= g: m[-1][1] = max(m[-1][1], b)
            else: m.append([a, b])
        return m
    m = [x for x in merge(segs, gap) if x[1] - x[0] >= minlen]
    m = merge([[max(0, a - pad), min(dur, b + pad)] for a, b in m], 0)
    return m or [[0.0, dur]]


def decide(info, rms, mot, bright, o):
    dur, has_a, sw, sh = info
    n = len(rms)
    nf, lo = pct(rms, 10), pct(rms, 90)
    thr = max(-55, min(-28, nf + 0.4 * (lo - nf))) if has_a else -99
    loud = [r > thr for r in rms]
    quiet = 1 - sum(loud) / n if has_a else 1.0
    mstd = statistics.pstdev(rms) if has_a and n > 1 else 0
    mmed = pct(mot, 50)
    if not has_a: kind = "visual"
    elif 0.06 <= quiet <= 0.75 and mstd >= 5 and mmed < 10: kind = "talk"
    elif mmed >= 4: kind = "action"
    else: kind = "general"

    # output size
    asp = o["aspect"]
    if asp == "auto": asp = "9:16" if sh > sw else "16:9"
    W, H = DIMS[asp]
    q = o.get("quality", "standard"); k = 2 / 3 if q == "draft" else 1
    W, H = int(W * k) // 2 * 2, int(H * k) // 2 * 2
    mode = "crop" if abs((sw / sh) / (W / H) - 1) < 0.12 else "blur"

    # score per window
    an = [min(1, max(0, (r - nf) / (lo - nf + 1e-6))) for r in rms] if has_a else [0] * n
    m95 = pct(mot, 95) + 1e-6
    mn = [min(1, x / m95) for x in mot]
    sc = [(0.5 * a + 0.5 * b) if has_a else b for a, b in zip(an, mn)]
    sc = [sum(sc[max(0, i - 1):i + 2]) / len(sc[max(0, i - 1):i + 2]) for i in range(n)]

    if kind == "talk":  # pauses > 0.5s kaato, chhote breath pauses rakho
        mask = loud[:]
        i = 0
        while i < n:
            if not mask[i]:
                j = i
                while j < n and not mask[j]: j += 1
                if j - i <= 2: mask[i:j] = [True] * (j - i)
                i = j
            else: i += 1
        segs = to_segs(mask, 0.2, 0.6, 0.15, dur)
    else:
        ratio = {"action": 0.55, "visual": 0.7, "general": 0.85}[kind]
        t = pct(sc, (1 - ratio) * 100)
        mask = [s >= t and not (m < 0.3 and (not has_a or r < thr)) for s, m, r in zip(sc, mot, rms)]
        segs = to_segs(mask, 1.0, 1.5 if kind == "action" else 1.0, 0.2, dur)

    cap = o["max_duration"] or (59 if H > W else 0)
    def mean_sc(s): 
        b = sc[int(s[0] / BIN):int(s[1] / BIN) + 1] or [0]; return sum(b) / len(b)
    while cap and sum(b - a for a, b in segs) > cap and len(segs) > 1:
        segs.remove(min(segs, key=mean_sc))
    if cap and sum(b - a for a, b in segs) > cap:
        segs[-1][1] = segs[-1][0] + cap
    total = sum(b - a for a, b in segs)
    segsc = [mean_sc(s) for s in segs]
    y = sum(bright) / len(bright) if bright else 112
    zooms = [1.14 if (kind == "talk" and i % 2) else 1.0 for i in range(len(segs))]
    return dict(segsc=segsc[:150], preset={"draft": "ultrafast", "standard": "veryfast", "high": "slow"}[q],
                crf={"draft": 27, "standard": 21, "high": 18}[q], kind=kind, segs=segs[:150], zooms=zooms[:150], W=W, H=H, aspect=asp, mode=mode, total=total,
                bright=max(-0.12, min(0.15, (112 - y) / 255 * 0.8)),
                sat=1.15 if kind in ("action", "visual") else 1.06,
                denoise=has_a and nf > -50, comp=(kind == "talk"), loud=-14 if H > W else -16,
                kept=round(100 * total / dur))



# ---------------- extras: captions, beats ----------------
def _ts(s):
    h, r = divmod(int(s * 1000), 3600000); m, r = divmod(r, 60000); sec, ms = divmod(r, 1000)
    return f"{h:02}:{m:02}:{sec:02},{ms:03}"


def _sec(t):
    h, m, s = t.replace(",", ".").split(":"); return int(h) * 3600 + int(m) * 60 + float(s)


def remap_srt(src, segs, d, out, spd=None):
    """Captions ka time cuts ke hisaab se khud adjust karta hai."""
    txt = Path(src).read_text(encoding="utf-8", errors="ignore").replace("\r", "")
    cues = []
    for blk in re.split(r"\n\s*\n", txt.strip()):
        ln = blk.split("\n"); k = next((i for i, l in enumerate(ln) if "-->" in l), None)
        if k is None: continue
        a, b = [_sec(x.strip()) for x in ln[k].split("-->")]
        cues.append((a, b, "\n".join(ln[k + 1:])))
    res, off = [], 0.0
    for i, (sa, sb) in enumerate(segs):
        base = off - i * d; sp = (spd or [1.0] * len(segs))[i]
        for a, b, t in cues:
            x, y = max(a, sa), min(b, sb)
            if y - x > 0.15: res.append((base + (x - sa) / sp, base + (y - sa) / sp, t))
        off += (sb - sa) / sp
    res.sort()
    out.write_text("".join(f"{i + 1}\n{_ts(a)} --> {_ts(b)}\n{t}\n\n" for i, (a, b, t) in enumerate(res)), encoding="utf-8")
    return len(res)


def beat_times(music, limit):
    out = run(["ffmpeg", "-v", "error", "-t", str(limit), "-i", str(music), "-vn", "-af",
               "aresample=22050,asetnsamples=441,astats=metadata=1:reset=1,"
               "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-", "-f", "null", "-"])
    r = []
    for line in out.splitlines():
        m = re.search(r"RMS_level=(\S+)", line)
        if m: r.append(-90.0 if m.group(1) in ("-inf", "nan") else max(-90.0, float(m.group(1))))
    fl = [max(0, r[i] - r[i - 1]) for i in range(1, len(r))]
    thr, peaks = pct(fl, 88), []
    for i, f in enumerate(fl):
        if f > 0 and f >= thr and f == max(fl[max(0, i - 5):i + 6]):
            t = (i + 1) * 0.02
            if not peaks or t - peaks[-1] >= 0.3: peaks.append(t)
    return peaks


def beat_slice(segs, beats, minlen=1.2):
    """Kept footage ko music ke beat par kaat-ta hai (beat-sync)."""
    total = sum(b - a for a, b in segs)
    cuts = [0.0]
    for t in beats:
        if t - cuts[-1] >= minlen and t < total - minlen: cuts.append(t)
    cuts.append(total)
    starts, o = [], 0.0
    for a, b in segs: starts.append(o); o += b - a
    ns, nz = [], []
    for k in range(len(cuts) - 1):
        c0, c1 = cuts[k], cuts[k + 1]
        for (a, b), so in zip(segs, starts):
            x, y = max(c0, so), min(c1, so + b - a)
            if y - x >= 0.04: ns.append([a + x - so, a + y - so]); nz.append(1.1 if k % 2 else 1.0)
    return ns[:150], nz[:150]


# ---------------- AI (optional, server par install ho to chalta hai) ----------------
try:
    import cv2, numpy as np
    HAS_CV = True
except Exception:
    HAS_CV = False
try:
    from faster_whisper import WhisperModel
    HAS_WHISPER = True
except Exception:
    HAS_WHISPER = False
FILLERS = {"um", "uh", "umm", "uhh", "uhm", "erm", "er", "ah", "hmm", "hm", "mm", "mmm"}
_wm, _wl = None, threading.Lock()


def warn(job, msg):
    if msg not in job["warnings"]: job["warnings"].append(msg)


def transcribe(src, tmp, lang=None):
    """Whisper (faster-whisper) se word-by-word transcript: [(start, end, word)]."""
    global _wm
    wav = tmp / "a.wav"
    run(["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(wav)])
    with _wl:
        if _wm is None:
            _wm = WhisperModel(os.environ.get("WHISPER_MODEL", "small"), device="cpu", compute_type="int8")
        segs, info = _wm.transcribe(str(wav), language=lang, word_timestamps=True, vad_filter=True,
                                    initial_prompt="Umm, uh, hmm, like, you know, so..." if lang in (None, "en") else None)
        words = [(w.start, w.end, w.word.strip()) for sg in segs for w in (sg.words or []) if w.word.strip()]
    return words, info.language


def words_to_srt(words, out, maxw=4, maxdur=2.6):
    """Words ko chhote, padhne layak caption chunks mein todta hai."""
    cues, cur = [], []
    def flush():
        if cur: cues.append((cur[0][0], cur[-1][1], " ".join(w[2] for w in cur))); cur.clear()
    for w in words:
        if cur and (len(cur) >= maxw or w[0] - cur[-1][1] > 0.6 or w[1] - cur[0][0] > maxdur): flush()
        cur.append(w)
        if re.search(r"[.?!\u0964]$", w[2]): flush()
    flush()
    out.write_text("".join(f"{i + 1}\n{_ts(a)} --> {_ts(max(b, a + 0.3))}\n{t}\n\n" for i, (a, b, t) in enumerate(cues)), encoding="utf-8")
    return len(cues)


def subtract(segs, cuts, minlen=0.25):
    out = []
    for a, b in segs:
        pieces = [[a, b]]
        for c0, c1 in cuts:
            nxt = []
            for x, y in pieces:
                if c1 <= x or c0 >= y: nxt.append([x, y]); continue
                if c0 > x: nxt.append([x, c0])
                if c1 < y: nxt.append([c1, y])
            pieces = nxt
        out += [q for q in pieces if q[1] - q[0] >= minlen]
    return out


def sample_subject(src, dur, maxn=240):
    """OpenCV: face dhundta hai, na mile to sabse zyada movement wali jagah (0..1 x-position)."""
    cap = cv2.VideoCapture(str(src)); n = min(maxn, max(2, int(dur * 2))); step = dur / n
    face = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    out, prev = [], None
    for i in range(n):
        t = i * step; cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000); ok, fr = cap.read()
        if not ok: continue
        h, w = fr.shape[:2]; fr = cv2.resize(fr, (480, max(2, int(h * 480 / w))))
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY); fx = mx = None
        fs = face.detectMultiScale(g, 1.15, 5, minSize=(30, 30))
        if len(fs): x, y, ww, hh = max(fs, key=lambda r: r[2] * r[3]); fx = (x + ww / 2) / g.shape[1]
        if prev is not None and prev.shape == g.shape:
            _, m = cv2.threshold(cv2.absdiff(g, prev), 25, 255, cv2.THRESH_BINARY)
            cols = m.sum(axis=0).astype(float)
            if cols.sum() > 0: mx = float((cols * np.arange(len(cols))).sum() / cols.sum() / len(cols))
        prev = g; out.append((t, fx, mx))
    cap.release()
    return out


def seg_center(samples, a, b, default=0.5):
    xs = [x[1] for x in samples if a <= x[0] <= b and x[1] is not None]
    if len(xs) < 2: xs = [x[2] for x in samples if a <= x[0] <= b and x[2] is not None]
    return statistics.median(xs) if xs else default


def seg_track(samples, a, b, K=6):
    """Segment ke andar kai keypoints: crop subject ke saath smooth pan karta hai."""
    L = b - a; k = max(2, min(K, int(L / 1.2) + 1)); pts, last = [], seg_center(samples, a, b)
    for j in range(k):
        t = L * j / (k - 1); last = seg_center(samples, a + t - 0.8, a + t + 0.8, last); pts.append((t, last))
    return pts


def pan_expr(pts, sw, bw):
    xs = [(t, c * sw - bw / 2) for t, c in pts]; e = f"{xs[-1][1]:.1f}"
    for k in range(len(xs) - 2, -1, -1):
        (t0, x0), (t1, x1) = xs[k], xs[k + 1]
        e = f"if(lt(t,{t1:.3f}),{x0:.1f}+({x1 - x0:.1f})*(t-{t0:.3f})/{max(t1 - t0, 0.01):.3f},{e})"
    return f"max(0,min({sw - bw},{e}))"


def ai_meta(text, job):
    """Claude API se title + description + hashtags (ANTHROPIC_API_KEY chahiye)."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not text.strip(): warn(job, "AI title: transcript nahi mila."); return None
    if not key: warn(job, "AI title ke liye server par ANTHROPIC_API_KEY set karo."); return None
    prompt = ('Neeche ek video ka transcript hai. Sirf JSON do: {"title": "...", "description": "...", "hashtags": ["#..."]}. '
              "Title 70 akshar se kam aur catchy, description 2 line, 8 hashtags. Jis bhasha mein transcript hai usi mein likho.\n\n" + text[:6000])
    body = json.dumps({"model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-5"), "max_tokens": 600,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST",
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r: t = json.load(r)["content"][0]["text"]
        return json.loads(t[t.index("{"):t.rindex("}") + 1])
    except Exception as e:
        warn(job, "AI title fail: " + str(e)[:120]); return None


# ---------------- 3. RENDER ----------------
def render(src, dst, o, info, pl, tmp):
    dur, has_a, sw, sh = info
    sw, sh = sw // 2 * 2, sh // 2 * 2
    segs, N, W, H = pl["segs"], len(pl["segs"]), pl["W"], pl["H"]
    d = 0.25 if (pl.get("trans") and N > 1) else 0
    spd = pl.get("speeds") or [1.0] * N
    lens = [(b - a) / sp for (a, b), sp in zip(segs, spd)]
    T = sum(lens) - d * (N - 1)
    pre = "deshake," if o.get("stabilize") else ""
    L = ["[0:v]%sfps=30,setsar=1,split=%d%s;" % (pre, N, "".join(f"[s{i}]" for i in range(N)))]
    if has_a:
        L.append("[0:a]aresample=44100,asplit=%d%s;" % (N, "".join(f"[a{i}]" for i in range(N))))
    for i, (a, b) in enumerate(segs):
        z = pl["zooms"][i]; tk = ""; bw, bh = sw, sh
        if pl.get("track"):
            bw = int(sh * W / H) // 2 * 2
            tk = f",crop={bw}:{bh}:x='{pan_expr(pl['track'][i], sw, bw)}':y=0"
        zoom = f",crop={int(bw / z) // 2 * 2}:{int(bh / z) // 2 * 2},scale={bw}:{bh}" if z > 1 else ""
        sp = spd[i]
        vs = f",setpts=PTS/{sp}" if sp != 1 else ""
        L.append(f"[s{i}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS{tk}{zoom}{vs},setsar=1[v{i}];")
        if has_a:
            at = f",atempo={sp}" if sp != 1 else ""
            L.append(f"[a{i}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS{at},"
                     f"afade=t=in:d=0.02,afade=t=out:st={max(0, lens[i] - .02):.3f}:d=0.02[x{i}];")
    if d:
        cur, vp, ap = lens[0], "v0", "x0"
        for i in range(1, N):
            tr = pl["trans"][(i - 1) % len(pl["trans"])]
            L.append(f"[{vp}][v{i}]xfade=transition={tr}:duration={d}:offset={cur - d:.3f}[xv{i}];")
            if has_a: L.append(f"[{ap}][x{i}]acrossfade=d={d}[xa{i}];")
            vp, ap, cur = f"xv{i}", f"xa{i}", cur + lens[i] - d
        L.append(f"[{vp}]null[cv];")
        if has_a: L.append(f"[{ap}]anull[ca];")
    else:
        cat = "".join(f"[v{i}][x{i}]" if has_a else f"[v{i}]" for i in range(N))
        L.append(f"{cat}concat=n={N}:v=1:a={1 if has_a else 0}[cv]" + ("[ca];" if has_a else ";"))
    if pl["mode"] == "crop":
        L.append(f"[cv]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}[r];")
    else:
        L.append(f"[cv]split[b][f];[b]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                 f"boxblur=30:6,eq=brightness=-0.08[bb];[f]scale={W}:{H}:force_original_aspect_ratio=decrease[ff];"
                 f"[bb][ff]overlay=(W-w)/2:(H-h)/2[r];")
    ch = [f"eq=brightness={pl['bright']:.3f}:contrast=1.06:saturation={pl['sat']}", "unsharp=5:5:0.5"]
    if o.get("enhance"): ch.insert(0, "hqdn3d=2:1.5:3:3")
    if pl.get("grade"): ch.append(pl["grade"])
    if pl.get("srt"):
        fn = str(pl["srt"]).replace("\\", "/").replace(":", "\\:")
        ch.append(f"subtitles='{fn}':force_style='Fontsize=15,Bold=1,Outline=2,Shadow=0,MarginV={60 if H > W else 30},Alignment=2'")
    if o["text"]:
        t = o["text"].replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        ch.append(f"drawtext=fontfile={FONT}:text='{t}':fontcolor=white:fontsize=h/26:box=1:boxcolor=black@0.4:"
                  f"boxborderw=12:x=(w-text_w)/2:y={'h/14' if o.get('text_pos', 'top') == 'top' else 'h-text_h-h/10'}")
    L.append("[r]" + ",".join(ch) + "[g];")
    nxt = 1
    if pl.get("logo"):
        m = int(W * 0.03); lp = o.get("logo_pos", "tr")
        lx = f"W-w-{m}" if lp[1] == "r" else str(m); ly = str(m) if lp[0] == "t" else f"H-h-{m}"
        L.append(f"[{nxt}:v]scale={int(W * 0.14)}:-1[lg];[g][lg]overlay={lx}:{ly}[g];"); nxt += 1
    if o.get("progress"):
        L.append(f"color=c=white@0.9:s={W}x{max(6, H // 180)}:r=30:d={T:.2f}[pb];[g][pb]overlay=x='-w+w*t/{T:.2f}':y=H-h[g];")
    L.append(f"[g]fade=t=in:st=0:d=0.25,fade=t=out:st={max(0, T - .35):.3f}:d=0.35,format=yuv420p[vout]")
    if has_a:
        ac = ["highpass=f=80"]
        if pl["denoise"]: ac.append("afftdn=nf=-30")
        if pl["comp"]: ac += ["acompressor=threshold=0.125:ratio=3:attack=5:release=80", "equalizer=f=3000:width_type=q:width=1.2:g=2"]
        ac += [f"loudnorm=I={pl['loud']}:TP=-1.5:LRA=11", "aresample=44100", "aformat=channel_layouts=stereo",
               "afade=t=in:d=0.15", f"afade=t=out:st={max(0, T - .35):.3f}:d=0.35"]
        L.append(";[ca]" + ",".join(ac) + "[aout]")
    fs = tmp / "f.txt"; fs.write_text("\n".join(L))
    cmd = ["ffmpeg", "-y", "-i", str(src)]
    if pl.get("logo"): cmd += ["-i", str(pl["logo"])]
    if not has_a: cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    cmd += ["-filter_complex_script", str(fs), "-map", "[vout]", "-map", "[aout]" if has_a else f"{nxt}:a"]
    if not has_a: cmd += ["-shortest"]
    cmd += ["-c:v", "libx264", "-preset", pl["preset"], "-crf", str(pl["crf"]), "-c:a", "aac", "-b:a", "160k",
            "-ar", "44100", "-movflags", "+faststart", str(dst)]
    run(cmd)
    return T


def make_card(text, W, H, path):
    t = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    fs = min(int(W * 0.86 / (max(len(text), 6) * 0.8)), int(H / 8))
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=0x101828:s={W}x{H}:d=2.5:r=30", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
         "-vf", f"drawtext=fontfile={FONT}:text='{t}':fontcolor=white:fontsize={fs}:x=(w-text_w)/2:y=(h-text_h)/2,"
                f"fade=t=in:d=0.4,fade=t=out:st=2.1:d=0.4,format=yuv420p", "-t", "2.5", "-shortest", "-c:v", "libx264",
         "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-ar", "44100", "-b:a", "160k", str(path)])


def mix_music(v, m, out, delay=0.0):  # music awaaz ke neeche khud dab jaata hai (ducking)
    run(["ffmpeg", "-y", "-i", str(v), "-stream_loop", "-1", "-i", str(m), "-filter_complex",
         f"[0:a]asplit=2[sc][vo];[1:a]adelay={int(delay * 1000)}|{int(delay * 1000)},volume=0.5[mu];[mu][sc]sidechaincompress=threshold=0.04:ratio=10:"
         "attack=20:release=500[du];[vo][du]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]",
         "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", str(out)])


def process(jid):
    job = jobs[jid]; d = WORK / jid; o = job["options"]; job["status"] = "processing"
    parts, files, alltxt = [], job["inputs"], []
    for i, f in enumerate(files):
        tmp = d / f"t{i}"; tmp.mkdir(exist_ok=True)
        job["step"] = f"Clip {i + 1}: analyse ho raha hai"
        info = probe(f); rms, mot, br = analyze(f, info[0], info[1])
        words, nfill, need_ai = None, 0, o.get("autocap") or o.get("cleanspeech") or o.get("aimeta")
        if need_ai and info[1]:
            if HAS_WHISPER:
                job["step"] = f"Clip {i + 1}: AI bol-chaal samajh raha hai (Whisper)"
                try: words, _ = transcribe(f, tmp, o.get("lang"))
                except Exception as e: warn(job, "Whisper fail: " + str(e)[:150])
            else: warn(job, "Whisper install nahi hai - auto captions / filler hatana / AI title skip hue.")
        pl = decide(info, rms, mot, br, o)
        st = STYLES.get(o["style"], STYLES["auto"]); pl["grade"] = st["grade"]; pl["logo"] = job.get("logo")
        pl["trans"] = None; beat = False
        if pl["kind"] != "talk" and job.get("music") and o["beat"]:
            if "beats" not in job: job["beats"] = beat_times(job["music"], 240)
            if len(job["beats"]) >= 4:
                pl["segs"], pl["zooms"] = beat_slice(pl["segs"], job["beats"]); beat = True
        if pl["kind"] != "talk" and not beat and 2 <= len(pl["segs"]) <= 60: pl["trans"] = st["trans"]
        pl["speeds"] = None
        if o.get("smartspeed") and pl["kind"] != "talk" and not beat and len(pl["segs"]) >= 3:
            scs = pl["segsc"]; hi = max(range(len(scs)), key=scs.__getitem__); lo_t = pct(scs, 30)
            sp = [1.0] * len(scs)
            for k, (a, b) in enumerate(pl["segs"]):
                if b - a < 1.5: continue
                if k == hi: sp[k] = 0.6
                elif scs[k] <= lo_t: sp[k] = 1.6
            pl["speeds"] = sp
        if words and o.get("cleanspeech") and pl["kind"] == "talk":
            cuts = [(w0 - 0.03, w1 + 0.03) for w0, w1, tx in words if re.sub(r"[^a-z]", "", tx.lower()) in FILLERS]
            ns = subtract(pl["segs"], cuts)
            if cuts and ns: pl["segs"], nfill = ns, len(cuts); pl["zooms"] = [1.14 if k % 2 else 1.0 for k in range(len(ns))]
        pl["track"] = None
        if o.get("track"):
            if not HAS_CV: warn(job, "OpenCV install nahi hai - subject tracking skip hua.")
            elif info[2] / info[3] > (pl["W"] / pl["H"]) * 1.12:
                job["step"] = f"Clip {i + 1}: AI subject dhundh raha hai"
                smp = sample_subject(f, info[0]); pl["track"] = [seg_track(smp, a, b) for a, b in pl["segs"]]; pl["mode"] = "crop"
        ncap = 0; srt_src = job.get("srt") if len(files) == 1 else None
        if not srt_src and words and o.get("autocap"):
            srt_src = tmp / "auto.srt"; words_to_srt(words, srt_src)
        if srt_src:
            pl["srt"] = tmp / "caps.srt"; ncap = remap_srt(srt_src, pl["segs"], 0.25 if pl["trans"] else 0, pl["srt"], pl["speeds"])
            if not ncap: pl["srt"] = None
        if words and o.get("aimeta"): alltxt.append(" ".join(w[2] for w in words))
        job["plan"].append({"clip": i + 1, "type": pl["kind"], "source_sec": round(info[0], 1),
                            "final_sec": round(pl["total"], 1), "kept_percent": pl["kept"], "cuts": len(pl["segs"]),
                            "size": pl["aspect"], "background": "blur" if pl["mode"] == "blur" else "crop-fill",
                            "zoom_punch": any(z > 1 for z in pl["zooms"]), "denoise": pl["denoise"],
                            "style": o["style"], "beat_sync": beat, "transitions": bool(pl["trans"]), "captions": ncap, "smart_speed": bool(pl["speeds"]), "quality": o["quality"], "tracked": bool(pl["track"]), "fillers_removed": nfill,
                            "auto_captions": bool(words and o.get("autocap") and not (job.get("srt") and len(files) == 1))})
        job["step"] = f"Clip {i + 1}: edit ho raha hai"
        dst = d / f"part{i}.mp4"; job["plan"][-1]["final_sec"] = round(render(f, dst, o, info, pl, tmp), 1); parts.append(dst)
        job["progress"] = round((i + 1) / (len(files) + 1) * 90)
    if o.get("aimeta"):
        job["step"] = "AI title / hashtags bana raha hai"; job["meta"] = ai_meta(" ".join(alltxt), job)
    out = d / "final.mp4"; delay = 0.0
    W, H = pl["W"], pl["H"]
    if o.get("intro"): c = d / "intro.mp4"; make_card(o["intro"], W, H, c); parts.insert(0, c); delay = 2.5
    if o.get("outro"): c = d / "outro.mp4"; make_card(o["outro"], W, H, c); parts.append(c)
    if len(parts) == 1: shutil.copy(parts[0], out)
    else:
        lst = d / "list.txt"; lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
        enc = (["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-b:a", "160k", "-ar", "44100"]
               if (delay or o.get("outro")) else ["-c", "copy"])
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), *enc, "-movflags", "+faststart", str(out)])
    if job.get("music"):
        job["step"] = "Music mix ho raha hai"
        mixed = d / "mixed.mp4"; mix_music(out, job["music"], mixed, delay); out = mixed
    job["step"] = "Thumbnail ban raha hai"
    try:
        th = d / "thumb.jpg"
        run(["ffmpeg", "-y", "-i", str(out), "-vf", "thumbnail=150,scale=640:-2", "-frames:v", "1", str(th)]); job["thumb"] = str(th)
    except Exception:
        job["thumb"] = None
    job.update(output=str(out), progress=100, status="done", step="Ho gaya")


def worker():
    while True:
        jid = q.get()
        try: process(jid)
        except Exception as e: jobs[jid].update(status="failed", error=str(e))
        q.task_done()


def cleaner():
    while True:
        time.sleep(300)
        for d in WORK.iterdir():
            if d.is_dir() and time.time() - d.stat().st_mtime > KEEP:
                shutil.rmtree(d, ignore_errors=True); jobs.pop(d.name, None)


for _ in range(int(os.environ.get("WORKERS", 2))): threading.Thread(target=worker, daemon=True).start()
threading.Thread(target=cleaner, daemon=True).start()


# ---------------- API ----------------
@app.get("/")
def home(): return send_file(Path(__file__).with_name("index.html"))


@app.post("/jobs")
def create():
    files = [f for f in request.files.getlist("files") if f.filename]
    if not files: return jsonify(detail="Video file do"), 400
    asp = request.form.get("aspect", "auto")
    if asp not in ("auto", *DIMS): return jsonify(detail="aspect galat hai"), 400
    jid = uuid.uuid4().hex[:12]; d = WORK / jid; d.mkdir(parents=True)
    ins = []
    for i, f in enumerate(files):
        p = d / f"in{i}{Path(f.filename).suffix or '.mp4'}"; f.save(p); ins.append(p)
    music = None
    mf = request.files.get("music")
    if mf and mf.filename:
        music = d / f"music{Path(mf.filename).suffix or '.mp3'}"; mf.save(music)
    def save(name, pre):
        f = request.files.get(name)
        if f and f.filename:
            p = d / f"{pre}{Path(f.filename).suffix}"; f.save(p); return p
    logo, srt = save("logo", "logo"), save("srt", "caps")
    tf = lambda k, dflt: request.form.get(k, dflt) in ("true", "1", "on")
    jobs[jid] = dict(status="queued", progress=0, step="Queue mein", inputs=ins, music=music, logo=logo, srt=srt, output=None, error=None, plan=[], warnings=[], meta=None,
                     options=dict(aspect=asp, text=request.form.get("text", ""), style=request.form.get("style", "auto"),
                                  stabilize=tf("stabilize", "false"), beat=tf("beat", "true"), smartspeed=tf("smartspeed", "false"),
                                  progress=tf("progress", "false"), enhance=tf("enhance", "false"),
                                  quality=(request.form.get("quality") if request.form.get("quality") in ("draft", "standard", "high") else "standard"),
                                  logo_pos=request.form.get("logo_pos", "tr"), text_pos=request.form.get("text_pos", "top"),
                                  autocap=tf("autocap", "false"), cleanspeech=tf("cleanspeech", "false"), track=tf("track", "false"), aimeta=tf("aimeta", "false"),
                                  lang=(request.form.get("lang") if re.fullmatch(r"[a-z]{2}", request.form.get("lang", "")) else None),
                                  intro=request.form.get("intro", "")[:60], outro=request.form.get("outro", "")[:60],
                                  max_duration=float(request.form.get("max_duration") or 0)))
    q.put(jid)
    return jsonify(job_id=jid)


@app.get("/jobs/<jid>")
def status(jid):
    j = jobs.get(jid) or abort(404)
    return jsonify({**{k: j[k] for k in ("status", "progress", "step", "error", "plan")}, "thumb": bool(j.get("thumb")), "warnings": j["warnings"], "meta": j["meta"]})


@app.get("/jobs/<jid>/download")
def download(jid):
    j = jobs.get(jid)
    if not j or j["status"] != "done": abort(409)
    return send_file(j["output"], mimetype="video/mp4", download_name=f"{jid}.mp4")


KEY = os.environ.get("ACCESS_KEY", "")


@app.before_request
def guard():
    if KEY and request.path.startswith("/jobs") and (request.headers.get("X-Key") or request.cookies.get("key")) != KEY:
        abort(401)


@app.get("/health")
def health(): return jsonify(ok=True, ffmpeg=bool(shutil.which("ffmpeg")), jobs=len(jobs), queue=q.qsize(),
                   ai=dict(whisper=HAS_WHISPER, opencv=HAS_CV, llm=bool(os.environ.get("ANTHROPIC_API_KEY"))))


@app.get("/jobs/<jid>/thumb")
def thumb(jid):
    j = jobs.get(jid)
    if not j or not j.get("thumb"): abort(404)
    return send_file(j["thumb"], mimetype="image/jpeg")


@app.get("/manifest.json")
def manifest():
    return jsonify(name="Auto Director", short_name="Director", start_url="/", display="standalone",
                   background_color="#0f1117", theme_color="#5b5bf0", icons=[dict(src="/icon.svg", sizes="any", type="image/svg+xml")])


@app.get("/icon.svg")
def icon():
    return app.response_class('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="22" fill="#5b5bf0"/>'
                              '<path d="M38 28v44l36-22z" fill="#fff"/></svg>', mimetype="image/svg+xml")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), threaded=True)
