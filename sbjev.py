"""sbjev - draft SponsorBlock segments with TypeSafe's Jev, open them as an import link.

Pipeline per video:
  1. yt-dlp pulls the auto-caption track (json3, word-level timings).
  2. Words are grouped into short timestamped lines (L000, L001, ...).
  3. Jev classifies every line (one Choice question per line, all lines of a
     window in one request, the window's transcript as state).
  4. Code merges labelled runs into segments, then a second Jev pass snaps
     each boundary to the exact word it starts/ends on.
  5. Segments already on SponsorBlock are dropped, and the rest become a
     https://www.youtube.com/watch?v=ID#segments=[...] link. Opening it with
     the SponsorBlock extension loads them as *unsubmitted* segments, so you
     preview/adjust them and press submit yourself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

import requests

API_URL = "https://api.typesafe.ai/v1/systemone"
SB_API = "https://sponsor.ajay.app/api/skipSegments"
PRICE_PER_MTOK = 0.042  # input only; output tokens are free
CACHE = Path(__file__).with_name("cache")
CONFIG_FILE = Path(__file__).with_name("config.json")


def load_config() -> dict:
    """config.json (see config.example.json) merged over the example's defaults. Keys starting
    with "_" are comments. A broken config.json is reported, not fatal."""
    def clean(d):
        return {k: clean(v) if isinstance(v, dict) else v for k, v in d.items() if not k.startswith("_")}
    cfg = {}
    example = Path(__file__).with_name("config.example.json")
    for p in (example, CONFIG_FILE):
        if p.exists():
            try:
                data = clean(json.loads(p.read_text(encoding="utf-8")))
            except ValueError as e:
                print(f"config: can't read {p.name} ({e}); using defaults", file=sys.stderr)
                continue
            keys = {**cfg.get("keys", {}), **data.get("keys", {})}
            cfg.update(data)
            cfg["keys"] = keys
    return cfg


CONFIG = load_config()


def ytdlp_cmd() -> list[str]:
    """yt-dlp on PATH (e.g. installed with scoop/winget), else the pip-installed module."""
    return ["yt-dlp"] if shutil.which("yt-dlp") else [sys.executable, "-m", "yt_dlp"]

CATEGORIES = {
    "sponsor": (
        "Paid promotion: the creator advertises a product or service they were paid by, "
        "including the lead-in (\"this video is sponsored by\", \"but first\"), discount codes, "
        "and \"link in the description\" for that product."
    ),
    "selfpromo": (
        "Unpaid self-promotion: the creator's own merch, Patreon/memberships, courses, their "
        "other channels or podcasts, or a shoutout for a friend. Not a paid sponsor."
    ),
    "interaction": (
        "Interaction reminder: asking viewers to like, subscribe, comment, share, ring the bell, "
        "or follow on social media."
    ),
    "intro": (
        "Intro: an opening animation, title card, or generic greeting that carries no information "
        "about the video's topic."
    ),
    "outro": (
        "Outro: end cards, credits, or a sign-off after the video's actual content has finished."
    ),
    "preview": (
        "Preview/recap: a teaser of what's coming later in this video (\"coming up...\", \"later we'll...\") "
        "or a recap of a previous episode. Not the video's own introduction of its topic."
    ),
    "filler": (
        "Filler tangent: an off-topic joke, skit, or tangent that is not needed to understand the video's "
        "main content."
    ),
    "poi_highlight": "Highlight: the single moment most viewers are looking for (a timestamp, not a range).",
}
# intro and outro come from dedicated detectors, not per-line classification
LINE_CATS = ["sponsor", "selfpromo", "interaction", "preview", "filler"]
CONTENT = (
    "Regular video content: the topic of the video, the creator's opinions, stories, or jokes. "
    "Mentioning a product that is the subject of the video is content, not a sponsor."
)
DEFAULT_CATS = ["sponsor", "selfpromo", "interaction", "intro", "outro"]
PEAK = {"selfpromo": 0.85, "interaction": 0.85, "preview": 0.85, "filler": 0.9}
MIN_SECONDS = {"sponsor": 5.0, "selfpromo": 2.0, "interaction": 1.0, "intro": 2.0, "outro": 3.0,
               "preview": 3.0, "filler": 5.0, "poi_highlight": 0.0}

DESCRIPTIONS = {
    "sponsor": "paid sponsor read",
    "selfpromo": "self-promotion",
    "interaction": "like/subscribe reminder",
    "intro": "intro",
    "outro": "outro",
    "preview": "preview or recap",
    "filler": "off-topic tangent",
    "poi_highlight": "highlight",
}


# ---------------------------------------------------------------- transcript

@dataclass
class Word:
    text: str
    t: float  # seconds


@dataclass
class Line:
    words: list[Word]
    start: float
    end: float = 0.0

    @property
    def text(self) -> str:
        return "".join(w.text for w in self.words).strip()


def fmt(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def ytdlp_extra(cookies_from: str | None = None) -> list[str]:
    """Flags every yt-dlp call gets. YouTube extraction needs a JavaScript runtime to solve
    its challenges (without one it hits "confirm you're not a bot" far more often). yt-dlp
    uses deno by default; only fall back to node when deno isn't installed (node must be
    >= 20.19 or yt-dlp ignores it as unsupported)."""
    extra = ["--js-runtimes", "node"] if not shutil.which("deno") and shutil.which("node") else []
    if cookies_from:
        extra += ["--cookies-from-browser", cookies_from]
    return extra


def video_info(video_id: str) -> dict:
    """yt-dlp's info JSON as cached by fetch_subs (title, channel, duration, ...)."""
    p = CACHE / "subs" / f"{video_id}.info.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def fetch_subs(video_id: str, cookies_from: str | None) -> tuple[list[Word], float, str, bool, list[float]]:
    """Return (words, duration, title, word_timed, music_tag_times). Prefers the auto-caption
    original-language track since it carries per-word offsets."""
    d = CACHE / "subs"
    d.mkdir(parents=True, exist_ok=True)
    info_path = d / f"{video_id}.info.json"
    base = ytdlp_cmd() + ["--no-warnings", "--quiet"] + ytdlp_extra(cookies_from)

    def ytdlp(args: list[str]) -> None:
        # YouTube 429s caption downloads when hit often; back off instead of failing at once
        for wait in (0, 20, 60):
            time.sleep(wait)
            r = subprocess.run(base + args, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode == 0:
                return
            if "429" not in r.stderr:
                break
            print(f"    YouTube rate-limited captions, retrying in {wait or 20}s", file=sys.stderr)
        if "429" in r.stderr:
            raise RuntimeError("YouTube is rate-limiting caption downloads from this IP (HTTP 429). "
                               "Wait a while, or run the server with --cookies-from-browser firefox/edge.")
        if "not a bot" in r.stderr:
            raise RuntimeError("YouTube's bot check is asking this connection to sign in. Turn off your VPN "
                               "if one is on; otherwise it usually clears within hours.")
        raise RuntimeError(f"yt-dlp failed: {r.stderr.strip()[-500:]}")

    # info and captions are cached separately; an interrupted run can leave either one
    if not info_path.exists():
        ytdlp(["--skip-download", "--write-info-json", "-o", str(d / "%(id)s.%(ext)s"),
               f"https://www.youtube.com/watch?v={video_id}"])
    if not list(d.glob(f"{video_id}.*.json3")):
        meta = json.loads(info_path.read_text(encoding="utf-8"))
        auto_langs = list(meta.get("automatic_captions") or {})
        manual_langs = list(meta.get("subtitles") or {})
        # exactly one track, always auto-generated when there is one (it has per-word timings):
        # the "-orig" ASR track, else a lone/spoken-language auto track (some videos name it
        # plain "en"), and hand-made captions only as a last resort
        spoken = (meta.get("language") or "en").split("-")[0]
        orig = sorted((l for l in auto_langs if l.endswith("-orig")), key=lambda l: not l.startswith(spoken))
        # a plain auto track counts only if it's served as json3 like real speech-recognition
        # tracks; some videos list the uploader's own captions as an HLS-only VTT "auto" track
        auto_json3 = [l for l in auto_langs
                      if any(f.get("ext") == "json3" for f in meta["automatic_captions"][l])]
        plain_auto = auto_json3 if len(auto_langs) == 1 else [l for l in auto_json3 if l.split("-")[0] == spoken]
        if orig or plain_auto:
            flag, lang, tmpl = "--write-auto-subs", (orig or plain_auto)[0], "%(id)s.auto.%(ext)s"
        elif any(l.startswith("en") for l in manual_langs):
            flag, lang, tmpl = "--write-subs", next(l for l in manual_langs if l.startswith("en")), "%(id)s.%(ext)s"
        elif manual_langs:
            flag, lang, tmpl = "--write-subs", manual_langs[0], "%(id)s.%(ext)s"
        else:
            raise RuntimeError("no captions available for this video")
        ytdlp(["--skip-download", "--load-info-json", str(info_path), flag, "--sub-langs", lang,
               "--sub-format", "json3", "-o", str(d / tmpl)])
    info = json.loads(info_path.read_text(encoding="utf-8"))
    duration = float(info.get("duration") or 0)
    title = info.get("title", video_id)

    tracks = sorted(d.glob(f"{video_id}.*.json3"))
    spoken = (info.get("language") or "en").split("-")[0]
    auto = sorted((p for p in tracks if p.name.endswith("-orig.json3") or ".auto." in p.name),
                  key=lambda p: not p.name.replace(".auto", "").startswith(f"{video_id}.{spoken}"))
    manual = [p for p in tracks if p not in auto]
    if not auto and not manual:
        raise RuntimeError("no captions available for this video")
    path = auto[0] if auto else manual[0]
    events = json.loads(path.read_text(encoding="utf-8")).get("events", [])

    words: list[Word] = []
    music: list[float] = []  # times of [Music] tags in auto captions
    word_timed = False
    for ev in events:
        if ev.get("aAppend") or "segs" not in ev:
            continue
        base = ev.get("tStartMs", 0)
        segs = ev["segs"]
        if any("tOffsetMs" in s for s in segs):
            word_timed = True
        if len(segs) == 1 and "tOffsetMs" not in segs[0]:
            # manual track: one caption per event, spread its words across the event
            toks = segs[0]["utf8"].replace("\n", " ").split()
            dur = ev.get("dDurationMs", 0)
            if any(t.lower() == "[music]" for t in toks):
                music.append(base / 1000)
            toks = [t for t in toks if not (t.startswith("[") and t.endswith("]"))]
            for i, tok in enumerate(toks):
                words.append(Word(" " + tok, (base + dur * i / max(1, len(toks))) / 1000))
            continue
        for s in segs:
            txt = s.get("utf8", "")
            if txt.strip().lower() == "[music]":
                music.append((base + s.get("tOffsetMs", 0)) / 1000)
                continue
            if not txt.strip() or txt.strip().lower() in ("[applause]", "[laughter]"):
                continue
            if not txt.startswith(" "):
                txt = " " + txt
            words.append(Word(txt, (base + s.get("tOffsetMs", 0)) / 1000))
    words.sort(key=lambda w: w.t)
    return words, duration, title, word_timed, music


def group_lines(words: list[Word], duration: float, max_words=14, max_secs=6.0, gap=1.2) -> list[Line]:
    lines: list[Line] = []
    cur: list[Word] = []
    for w in words:
        if cur and (len(cur) >= max_words or w.t - cur[0].t >= max_secs or w.t - cur[-1].t >= gap
                    or (len(cur) >= 6 and cur[-1].text.rstrip().endswith((".", "?", "!")))):
            lines.append(Line(cur, cur[0].t))
            cur = []
        cur.append(w)
    if cur:
        lines.append(Line(cur, cur[0].t))
    for a, b in zip(lines, lines[1:]):
        a.end = b.start
    if lines:
        lines[-1].end = max(duration, lines[-1].words[-1].t + 1.0)
    return lines


# ---------------------------------------------------------------- jev

def env(name: str) -> str | None:
    """os.environ, then the user registry (so values saved with `setx` work without opening a new
    terminal), then the "keys" section of config.json."""
    if os.environ.get(name):
        return os.environ[name]
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v = str(winreg.QueryValueEx(k, name)[0]).strip()
                if v:
                    return v
        except OSError:
            pass
    return (str(CONFIG.get("keys", {}).get(name.lower()) or "").strip()) or None


class Jev:
    """Talks to Jev directly (TYPESAFE_API_KEY) or through Cloudflare Workers AI
    (CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN), which serves the same model."""

    def __init__(self, model: str):
        self.model = model
        self.tokens = 0
        self.calls = 0
        self.cost = 0.0  # dollars actually billed this process (cached answers are free)
        self.lock = __import__("threading").Lock()
        (CACHE / "jev").mkdir(parents=True, exist_ok=True)
        if env("TYPESAFE_API_KEY"):
            self.backend, self.key = "typesafe", env("TYPESAFE_API_KEY")
        elif env("AI_GATEWAY_API_KEY"):
            self.backend, self.key = "vercel", env("AI_GATEWAY_API_KEY")
        elif env("CLOUDFLARE_ACCOUNT_ID") and env("CLOUDFLARE_API_TOKEN"):
            self.backend, self.key = "cloudflare", env("CLOUDFLARE_API_TOKEN")
            self.cf_url = (f"https://api.cloudflare.com/client/v4/accounts/"
                           f"{env('CLOUDFLARE_ACCOUNT_ID')}/ai/run")
        else:
            raise SystemExit("No Jev API key. Put one in the \"keys\" section of config.json "
                             "(ai_gateway_api_key for Vercel, typesafe_api_key, or the two cloudflare_ keys), "
                             "or set the matching environment variable.")

    def account(self) -> dict | None:
        """Account-wide spend and balance, where the backend exposes them (Vercel does)."""
        if self.backend != "vercel":
            return None
        try:
            r = requests.get("https://ai-gateway.vercel.sh/v1/credits", timeout=10,
                             headers={"Authorization": f"Bearer {self.key}"})
            d = r.json()
            return {"spent": float(d["total_used"]), "balance": float(d["balance"])}
        except Exception:
            return None

    def ask(self, state, questions: dict, retries: int = 6) -> dict:
        body = {"model": self.model, "state": state, "questions": questions}
        raw = json.dumps(body, sort_keys=True, ensure_ascii=False)
        cpath = CACHE / "jev" / (hashlib.sha256(raw.encode()).hexdigest()[:24] + ".json")
        if cpath.exists():
            return json.loads(cpath.read_text(encoding="utf-8"))["answers"]
        if os.environ.get("SBJEV_CACHE_ONLY") == "1":  # free replays: never send anything to the API
            raise CacheMiss("request not in cache (SBJEV_CACHE_ONLY=1)")
        if self.backend == "cloudflare":
            url = self.cf_url
            send = json.dumps({"model": "typesafe/jev", "input": {"state": state, "questions": questions}},
                              ensure_ascii=False)
        elif self.backend == "vercel":
            # Vercel's gateway speaks TypeSafe's wire format, under its own model id
            url = "https://ai-gateway.vercel.sh/typesafe/v1/systemone"
            # pinned versions pass through ("jev-1.14.0" -> "typesafe-ai/jev-1.14.0"); the cache is keyed
            # by this name, so pinning also keeps answers from different versions apart
            vmodel = "typesafe-ai/jev" if self.model in ("jev-latest", "jev") else f"typesafe-ai/{self.model}"
            send = json.dumps({**body, "model": vmodel}, ensure_ascii=False)
        else:
            url, send = API_URL, raw
        for attempt in range(retries):
            r = requests.post(url, data=send.encode(), timeout=180, headers={
                "Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
            if r.status_code in (429, 529) or r.status_code >= 500:
                wait = float(r.headers.get("retry-after") or 2 ** attempt)
                time.sleep(min(wait, 60))
                continue
            if r.status_code != 200:
                if "RestrictedModelsError" in r.text or "Free tier users" in r.text:
                    raise RuntimeError("Vercel won't run Jev on free credit any more. Buy paid AI Gateway "
                                       "credits at vercel.com → AI Gateway → Credits, then try again.")
                if r.status_code == 402 or "Insufficient balance" in r.text:
                    raise RuntimeError(f"Out of credit on {self.backend}; top up and try again.")
                raise RuntimeError(f"Jev HTTP {r.status_code}: {r.text[:500]}")
            out = r.json()
            if "answers" not in out and isinstance(out.get("result"), dict):
                out = out["result"]  # Cloudflare envelope
            if "answers" not in out:
                raise RuntimeError(f"Jev: unexpected response: {r.text[:500]}")
            # Vercel reports what each request actually cost; otherwise estimate from list price
            billed = ((out.get("provider_metadata") or {}).get("gateway") or {}).get("cost")
            with self.lock:  # requests run in parallel threads
                self.calls += 1
                self.tokens += out.get("usage", {}).get("input_tokens", 0)
                self.cost += float(billed) if billed is not None else \
                    out.get("usage", {}).get("input_tokens", 0) / 1e6 * PRICE_PER_MTOK
            cpath.write_text(json.dumps(out), encoding="utf-8")
            return out["answers"]
        raise Overloaded("Jev: gave up after repeated 429/5xx")


class Overloaded(RuntimeError):
    pass


class CacheMiss(RuntimeError):
    pass


def lid(i: int) -> str:
    return f"L{i:04d}"


SHORT = {
    "content": "regular video content",
    "sponsor": "paid sponsor ad read",
    "selfpromo": "unpaid plug for the creator's own stuff",
    "interaction": "like/subscribe/comment reminder",
    "intro": "contentless intro",
    "outro": "end cards / sign-off",
    "preview": "teaser of later in the video, or recap",
    "filler": "off-topic tangent or skit",
}


SCAN_Q = {
    "sponsor": "Does this excerpt contain any part of a paid sponsor read or ad (the creator promoting a "
               "product or service they were paid by, including a segue into it or a discount code)?",
    "selfpromo": "Does this excerpt contain unpaid self-promotion (the creator's own merch, Patreon, "
                 "memberships, courses, other channels, or a shoutout)?",
    "interaction": "Does this excerpt contain a request to like, subscribe, comment, share, or follow?",
    "preview": "Does this excerpt contain a teaser of what's coming later in the video, or a recap of a "
               "previous episode?",
    "filler": "Does this excerpt contain an off-topic tangent, joke, or skit unrelated to the video's topic?",
}


SCAN_CHUNK, SCAN_MARGIN, SCAN_THRESHOLD = 30, 8, 0.1
# bare per-line questions: half the cost, same recall on 15 held-out videos, ~1 extra false
# segment per 15 videos (eval/e2e_eval.py); SBJEV_LEAN=0 restores the labelled questions
LEAN_QUESTIONS = os.environ.get("SBJEV_LEAN", "1") == "1"
# sentence-based edges for sponsor/selfpromo: on 30 held-out videos, median start error vs the
# crowd 3.70 s -> 0.56 s (within 0.5 s: 7% -> 47%), end 1.33 s -> 0.96 s (eval/e2e_eval.py)
SENTENCE_EDGES = os.environ.get("SBJEV_SENTENCES", "1") == "1"


def scan_chunk(jev: Jev, lines: list[Line], s: int, e: int, cats: list[str]) -> dict[str, float]:
    """Cheap first pass: one yes/no per category for a whole chunk of transcript.
    Asking ~100 tokens of question per LINE is what makes line classification
    expensive; most chunks contain none of these categories and can skip it."""
    state = "YouTube transcript excerpt:\n" + "\n".join(lines[i].text for i in range(s, e))
    ans = jev.ask(state, {c: {"type": "noul", "instructions": SCAN_Q[c]} for c in cats})
    return {c: ans[c]["noul"] for c in cats}


def classify_lines(jev: Jev, lines: list[Line], cats: list[str], batch=24, context=20,
                   ranges: list[tuple[int, int]] | None = None) -> list[dict[str, float]]:
    """Per-line probability for each category (plus 'content'). Lines are asked
    about in small batches, each with `context` lines either side as state.
    Hosted Jev 503s on large requests under load, so a batch that keeps
    failing is split in half and retried. With `ranges`, only those line
    ranges are asked about; every other line counts as plain content."""
    keys = ["content", *cats]
    guide = "\n".join(f"- {k}: {CONTENT if k == 'content' else CATEGORIES[k]}" for k in keys)
    # each question costs ~99 tokens with its own labels vs ~50 bare; the categories are
    # already described once in the state
    criteria = {k: None for k in keys} if LEAN_QUESTIONS else {k: SHORT[k] for k in keys}
    out: list[dict[str, float] | None] = [{k: float(k == "content") for k in keys} for _ in lines]
    ranges = ranges if ranges is not None else [(0, len(lines))]
    todo = [(s, min(e, s + batch)) for a, e in ranges for s in range(a, e, batch)]

    def do_batch(s: int, e: int) -> None:
        lo, hi = max(0, s - context), min(len(lines), e + context)
        state = ("Auto-generated YouTube transcript excerpt, one line per id.\n\nCategories:\n" + guide
                 + "\n\nTranscript:\n"
                 + "\n".join(f"{lid(i)} [{fmt(lines[i].start)}] {lines[i].text}" for i in range(lo, hi)))
        questions = {
            lid(i): {
                "type": "choice",
                "instructions": f"Category of line {lid(i)}?" if LEAN_QUESTIONS else
                                f"Using nearby lines as context, which category does line {lid(i)} belong to?",
                "criteria": criteria,
            }
            for i in range(s, e)
        }
        try:
            answers = jev.ask(state, questions, retries=3 if e - s > 4 else 8)
        except Overloaded:
            if e - s <= 4:
                raise
            mid = (s + e) // 2
            print(f"    Jev busy, retrying {e - s} lines as two smaller requests", file=sys.stderr)
            do_batch(s, mid)
            do_batch(mid, e)
            return
        for i in range(s, e):
            probs = answers[lid(i)]["probabilities"]
            out[i] = {k: probs.get(k, 0.0) for k in keys}

    # batches are independent, so they go out together (identical requests, so caching is unchanged)
    parallel(lambda se: do_batch(*se), todo)
    print(f"    classified {sum(e - s for s, e in todo)} lines in {len(todo)} batches", file=sys.stderr)
    return out


JEV_PARALLEL = int(os.environ.get("SBJEV_PARALLEL") or CONFIG.get("parallel_requests", 6))  # concurrent Jev requests; the hosted service 503s under heavy bursts


def parallel(fn, items: list) -> list:
    """Run fn over items with up to JEV_PARALLEL threads; results in input order.
    The first exception is re-raised (after the others finish)."""
    from concurrent.futures import ThreadPoolExecutor
    if len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=min(JEV_PARALLEL, len(items))) as ex:
        return list(ex.map(fn, items))


@dataclass
class Segment:
    cat: str
    first: int  # line index
    last: int
    start: float = 0.0
    end: float = 0.0
    conf: float = 0.0
    notes: list[str] = field(default_factory=list)


def find_runs(probs: list[dict[str, float]], cats: list[str], threshold: float) -> list[Segment]:
    labels = []
    for p in probs:
        best = max(p, key=p.get)
        labels.append(best if best in cats and p[best] >= threshold else None)
    # bridge single-line holes where the category is still plausible
    for i in range(1, len(labels) - 1):
        if labels[i] is None and labels[i - 1] and labels[i - 1] == labels[i + 1] \
                and probs[i][labels[i - 1]] >= threshold / 2:
            labels[i] = labels[i - 1]
    runs: list[Segment] = []
    for i, lab in enumerate(labels):
        if lab and runs and runs[-1].cat == lab and runs[-1].last == i - 1:
            runs[-1].last = i
        elif lab:
            runs.append(Segment(lab, i, i))
    for r in runs:
        r.conf = sum(probs[i][r.cat] for i in range(r.first, r.last + 1)) / (r.last - r.first + 1)
    return runs


def extend_leadin(jev: Jev, lines: list[Line], seg: Segment, back=4) -> None:
    """Line classification misses segues that don't name the sponsor yet
    ("...and if you can't do X, the next best thing is"), which SponsorBlock
    counts as part of the read. Ask about the few lines before the run and
    walk back while they are judged to be lead-in."""
    cands = list(range(max(0, seg.first - back), seg.first))
    if seg.cat != "sponsor" or not cands:
        return
    lo, hi = max(0, cands[0] - 6), min(len(lines), seg.first + 4)
    state = "YouTube transcript excerpt:\n" + "\n".join(f"{lid(i)} {lines[i].text}" for i in range(lo, hi))
    ans = jev.ask(state, {lid(k): {
        "type": "noul",
        "instructions": f"A paid sponsor read begins at {lid(seg.first)}. Is line {lid(k)} already part of "
                        f"the segue that exists to lead into that sponsor, rather than the video's own content?",
        "criteria": {"true": "a transition whose purpose is setting up the sponsor mention",
                     "false": "regular video content that would make sense without the sponsor"},
    } for k in cands})
    for k in reversed(cands):
        if ans[lid(k)]["noul"] < 0.5:
            break
        seg.first = k
        seg.notes.append("lead-in extended") if "lead-in extended" not in seg.notes else None


def detect_outro(jev: Jev, lines: list[Line], words: list[Word], duration: float, tail=30) -> Segment | None:
    """Outro = the spoken sign-off after the content is over ("come back Friday",
    "thanks for watching", end-screen talk) plus any wordless tail after it.
    Jev picks the first sign-off line among the last `tail` lines; the silent
    tail alone needs no model."""
    if not lines:
        return None
    first = max(0, len(lines) - tail)
    state = ("The last lines of a YouTube video transcript. The video ends right after the final line.\n"
             + "\n".join(f"{lid(i)} [{fmt(lines[i].start)}] {lines[i].text}" for i in range(first, len(lines))))
    ans = jev.ask(state, {
        "has_outro": {
            "type": "noul",
            "instructions": "Does the transcript end with an outro: a sign-off after the video's actual content "
                            "is finished, such as thanks for watching, see you next time, come back on Friday, "
                            "check out this other video, or talk over the end screen?",
            "criteria": {"true": "there is a closing sign-off that carries no new information on the topic",
                         "false": "the video's content (information, story, argument) runs right to the end"},
        },
        "outro_start": {
            "type": "choice",
            "instructions": "Which line is the FIRST line of that closing sign-off? Pick the line where the "
                            "video's content ends and the goodbye / come-back / end-screen talk begins.",
            "criteria": {lid(i): None for i in range(first, len(lines))},
        },
    })
    spoken = ans["has_outro"]["noul"]
    silent_from = words[-1].t + 0.6 if words else 0.0
    # jokey sign-offs ("come back Wednesday for a chance to win...") score only ~0.45-0.6
    # on has_outro while Jev still points at the right line confidently
    if spoken >= 0.6 or (spoken >= 0.4 and ans["outro_start"]["confidence"] >= 0.6):
        i = int(ans["outro_start"]["choice"][1:])
        seg = Segment("outro", i, len(lines) - 1, conf=spoken)
        seg.start = lines[i].start
        if ans["outro_start"]["confidence"] < 0.5:
            seg.notes.append("start uncertain")
        return seg
    if duration - silent_from >= 3.0:
        seg = Segment("outro", len(lines) - 1, len(lines) - 1, conf=0.9)
        seg.start = silent_from
        seg.notes.append("no speech after this point (end screen?)")
        return seg
    return None


def detect_highlight(jev: Jev, lines: list[Line], title: str, chunk=25) -> Segment | None:
    """SponsorBlock highlight = one timestamp: the moment the video's title
    promises (the reveal, the result, the main event). Each chunk of the
    transcript is asked whether it contains that moment and, if so, where;
    only a clear winner is reported."""
    if not lines:
        return None
    def ask_chunk(s: int) -> tuple[float, int, float]:  # (noul, line index, pick confidence)
        e = min(len(lines), s + chunk)
        state = (f"Video title: {title}\n"
                 f"Transcript excerpt ({fmt(lines[s].start)}-{fmt(lines[e - 1].end)} of {fmt(lines[-1].end)}):\n"
                 + "\n".join(f"{lid(i)} {lines[i].text}" for i in range(s, e)))
        ans = jev.ask(state, {
            "here": {
                "type": "noul",
                "instructions": "Does this excerpt contain the highlight of the video: the moment the title "
                                "promises, that most viewers would skip ahead to (the reveal, the result, the "
                                "main event, the answer)?",
                "criteria": {"true": "the payoff moment happens in this excerpt",
                             "false": "setup, background, side content, or ads; the payoff is elsewhere"},
            },
            "where": {
                "type": "choice",
                "instructions": "If the highlight is in this excerpt, which line does it begin on?",
                "criteria": {lid(i): None for i in range(s, e)},
            },
        })
        return ans["here"]["noul"], int(ans["where"]["choice"][1:]), ans["where"]["confidence"]

    best = parallel(ask_chunk, list(range(0, len(lines), chunk)))
    best.sort(reverse=True)
    top = best[0]
    runner_up = best[1][0] if len(best) > 1 else 0.0
    if top[0] < 0.75 or top[0] - runner_up < 0.2:
        return None
    seg = Segment("poi_highlight", top[1], top[1], conf=top[0])
    seg.start = seg.end = lines[top[1]].start
    if top[2] < 0.5:
        seg.notes.append("exact line uncertain")
    return seg


def detect_intro(words: list[Word], music: list[float]) -> Segment | None:
    """Intro animations show up in auto captions as a stretch with no words,
    usually tagged [Music]. Only the first two minutes are searched."""
    if not words:
        return None
    if words[0].t >= 3.0:
        seg = Segment("intro", 0, 0, start=0.0, end=words[0].t - 0.1, conf=0.7)
        seg.notes.append("no speech before this; check it's an intro, not silent footage")
        return seg
    for a, b in zip(words, words[1:]):
        if a.t > 120:
            break
        gap_start = a.t + 0.6
        if b.t - gap_start >= 4.0 and any(gap_start - 1 <= m <= b.t for m in music):
            seg = Segment("intro", 0, 0, start=gap_start, end=b.t - 0.1, conf=0.7)
            seg.notes.append("music with no speech; check it's an intro animation")
            return seg
    return None


# ---------------------------------------------------------------- sentence edges
#
# Edges are chosen among SENTENCES, not the 14-word detection lines: well-rated crowd edges
# sit on a caption sentence boundary 77% (start) / 85% (end) of the time, but on a line
# start only 57-62%, so the right answer often wasn't even an option. Structure follows
# github.com/trungdq88/youtube-sponsor-detection (lead-in -> pitch -> offer, anchor on the
# naming sentence, one trace-back choice), with rules from the eval corpus (the crowd keeps
# "and now back to the video" inside the segment).

@dataclass
class Sentence:
    first: int  # index into words
    last: int
    start: float
    text: str


def split_sentences(words: list[Word], max_words=40) -> list[Sentence]:
    out: list[Sentence] = []
    cur = 0
    for i in range(len(words)):
        end = i == len(words) - 1
        stop = words[i].text.rstrip().endswith((".", "!", "?"))
        pause = not end and words[i + 1].t - words[i].t > 1.0
        if end or stop or pause or i - cur + 1 >= max_words:
            out.append(Sentence(cur, i, words[cur].t, "".join(w.text for w in words[cur:i + 1]).strip()))
            cur = i + 1
    return out


AD_SHAPE = {
    "sponsor": ("a paid sponsor segment",
                "It promotes a third party that paid for placement. It has up to three parts and begins "
                "with the first: 1) a lead-in, where the creator leaves the subject of the video and starts "
                "a segue, joke, story, problem or \"quick break\" whose purpose is to arrive at the sponsor; "
                "2) the pitch, where the sponsor is named and described; 3) the offer: link, code, free "
                "trial, \"link in the description\", thanks to the sponsor, and a hand-back such as \"and "
                "now, back to the video\"."),
    "selfpromo": ("a self-promotion segment",
                  "The creator promotes their own merch, company, product, course, membership, Patreon or "
                  "other channel. It begins with any segue whose purpose is to arrive at that plug and "
                  "ends with the plug's link/offer and any hand-back such as \"and now, back to the video\"."),
}


_SEGUE_RULE = ("A segue, joke or pun that exists to transition into the product belongs to the segment, "
               "even if it mentions the video's topic (\"...just like X, our sponsor...\").")
_NAMED_RULE = "If the segment opens by naming the product, choose `sponsor_named_at`."
START_RULES_VARIANTS = {
    "orig": [_SEGUE_RULE,
             "Sentences that still inform about the video's subject, or conclude it, are NOT part "
             "of the segment, even the one right before it.",
             _NAMED_RULE],
    "A": [_SEGUE_RULE, _NAMED_RULE],
    "B": [_SEGUE_RULE,
          "The lead-in often begins where the creator steps out of the video's own analysis or story: a "
          "personal aside (\"I just want to say how great it is to cover this show\"), talking to the viewers "
          "(\"a lot of you told me...\"), or a theme that sets up what the product is for (online privacy before "
          "a VPN, phones before a mobile game). It belongs to the segment from its first sentence.",
          "The video's own conclusion or summary of its subject is NOT part of the segment.",
          _NAMED_RULE],
}
START_RULES = START_RULES_VARIANTS[os.environ.get("SBJEV_START_RULES", "B")]


def sentence_edges(jev: Jev, words: list[Word], seg: Segment, title: str) -> None:
    """Re-place a detected sponsor/selfpromo segment's start and end on sentence
    boundaries: anchor on the sentence that first names the product, trace back
    to the first sentence of its lead-in, then pick the last sentence of the offer."""
    what, shape = AD_SHAPE[seg.cat]
    sents = split_sentences(words)
    if len(sents) < 3:
        return
    sid = lambda i: f"S{i:04d}"  # noqa: E731
    first = next((i for i, s in enumerate(sents) if s.start >= seg.start - 0.5), len(sents) - 1)
    last = max((i for i, s in enumerate(sents) if s.start < seg.end - 0.5), default=first)
    render = lambda a, b: "\n".join(f"{sid(i)} {sents[i].text}" for i in range(a, b))  # noqa: E731

    # 1) the sentence that first names what is being promoted, near the detected start
    a, b = max(0, first - 6), min(len(sents), first + 10, last + 1)
    ans = jev.ask({"video_title": title, "transcript": render(a, b)}, {"named": {
        "type": "choice",
        "instructions": {"question": f"This transcript excerpt contains {what}. Which sentence is the FIRST one "
                                     f"that names the product, brand or service being promoted?",
                         "segment_definition": shape} if "named" not in FIX else {
            "question": f"This transcript excerpt contains {what}. Which sentence BEGINS the dedicated pitch, "
                        f"where the creator turns to promoting the product?",
            "segment_definition": shape,
            "rules": ["A brief passing mention inside the video's own content (\"this was shot on X, our "
                      "sponsor\", \"X, our sponsor, also does this\") is not the pitch; choose where the "
                      "creator stops the video's content to promote it."]},
        "criteria": {sid(i): None for i in range(a, b)},
    }})
    named = int(ans["named"]["choice"][1:])

    # 2) read backwards from the naming sentence for the first sentence of the lead-in
    a = max(0, named - 15)
    ans = jev.ask({"video_title": title, "transcript": render(a, named + 1), "sponsor_named_at": sid(named)},
                  {"start": {
                      "type": "choice",
                      "instructions": {
                          "question": f"The product is first named at `sponsor_named_at`. Which sentence is the FIRST "
                                      f"sentence of {what}: where the creator leaves the subject of the video "
                                      f"(`video_title`) and begins the lead-in that arrives at the product?",
                          "segment_definition": shape,
                          "rules": START_RULES},
                      "criteria": {sid(i): None for i in range(a, named + 1)},
                  }})
    start_i = int(ans["start"]["choice"][1:])
    start_conf = ans["start"]["confidence"]

    if "walk" in FIX and start_i > 0:
        # starts were late 74% of the time: long segues ("before we get to the interview, I want to
        # talk about how I research these videos...") get left out. Ask about each of the few
        # sentences before the pick on its own and walk back while they read as lead-in.
        cands = list(range(max(0, start_i - 6), start_i))
        ans = jev.ask({"video_title": title, "transcript": render(max(0, cands[0] - 3), named + 1),
                       "segment_starts_at": sid(start_i), "sponsor_named_at": sid(named)},
                      {sid(k): {"type": "noul",
                                "instructions": f"Is sentence {sid(k)} already part of the lead-in to {what} that "
                                                f"follows: has the creator left the video's subject (`video_title`) "
                                                f"to set up the product?",
                                "criteria": {"true": "a segue, story or question whose purpose is arriving at the "
                                                     "product",
                                             "false": "still the video's own content, or its conclusion"}}
                       for k in cands})
        for k in reversed(cands):
            if ans[sid(k)]["noul"] < 0.6:
                break
            start_i = k
            seg.notes.append("lead-in walked back") if "lead-in walked back" not in seg.notes else None

    # 3) the last sentence of the offer / hand-back
    # the line-based run already ends near the read's end; a wide window let the pick drift 15-50 s
    # into the next topic (eval), so "end" narrows it
    a, b = max(named, last - 8), min(len(sents), last + (4 if "end" in FIX else 10))
    ans = jev.ask({"video_title": title, "transcript": render(a, b)}, {"end": {
        "type": "choice",
        "instructions": {
            "question": f"This excerpt contains the end of {what}. Which sentence is the LAST sentence of it, "
                        f"right before the video's own content resumes?",
            "segment_definition": shape,
            "rules": ["The offer (link, code, trial, thanking the sponsor) belongs to the segment.",
                      "A hand-back such as \"and now, back to the video\" or \"let's get back to it\" belongs "
                      "to the segment.",
                      "The first sentence that is about the video's subject again is NOT part of it."]},
        "criteria": {sid(i): None for i in range(a, b)},
    }})
    end_i = int(ans["end"]["choice"][1:])
    end_conf = ans["end"]["confidence"]
    if end_i < start_i:
        return  # inconsistent picks: keep the line-based edges

    # the end pick often swallows the sign-off ("thanks so much for watching, see you in the next one")
    # or a next-episode teaser ("Next time, or right now on Nebula, ..."); the crowd ends the ad before
    # them (eval set C: ~13 of 30 late ends)
    while end_i > start_i and end_i > named and SIGNOFF.search(sents[end_i].text):
        end_i -= 1
        if "sign-off left out of the ad" not in seg.notes:
            seg.notes.append("sign-off left out of the ad")

    # "We'll talk about that after the break." / "...right after a quick word from our sponsor": the
    # crowd starts the segment on this cliffhanger, Jev counts it as content (eval set C: ~10 of 28 late
    # starts). Same idea as include_return_line at the other end.
    for _ in range(2):
        if start_i > 0 and BREAK_TEASER.search(sents[start_i - 1].text) \
                and sents[start_i].start - sents[start_i - 1].start < 12:
            start_i -= 1
            seg.notes.append("includes the 'after the break' line")
        else:
            break
    seg.start = sents[start_i].start
    nxt = sents[end_i + 1] if end_i + 1 < len(sents) else None
    seg.end = nxt.start if nxt else words[sents[end_i].last].t + 0.6
    if start_conf < 0.4:
        seg.notes.append("start uncertain")
    if end_conf < 0.4:
        seg.notes.append("end uncertain")


# experimental fixes, tested on Jev 1.14 (eval/e2e_eval.py): "own" (sponsor<->selfpromo both ways)
# held up on fresh videos and is on by default; "named", "end" and "walk" didn't and stay off. Also
# tried and dropped (no gain on a 97-video held-out set): a per-sentence yes/no "profile" for edges,
# a wider trace-back window, close-call tie-breaks toward the earlier sentence, an opening-word prior,
# a coarse paragraph-level search for long segues (worse at 0.5 confidence, neutral at 0.8).
FIX = set(filter(None, os.environ.get("SBJEV_FIX", "own").split(",")))


def own_product(jev: Jev, lines: list[Line], seg: Segment, channel: str) -> float:
    """Probability that the promoted product is the creator's OWN (SponsorBlock files that as
    selfpromo, not sponsor; Mark Rober's CrunchLabs was the first case found in eval/e2e_eval.py).
    One small question per ad segment."""
    body = "\n".join(lines[i].text for i in range(seg.first, min(seg.last + 1, seg.first + 25)))
    question = {
        "type": "noul",
        "instructions": "Is the product or service being promoted owned or made by this channel's creator "
                        "(their own company, product line, merch, course, subscription box, app, or other "
                        "channel), rather than by a separate company paying for the promotion?",
        "criteria": {"true": "the creator's own business or product",
                     "false": "a third-party brand sponsoring the video"},
    }
    if "own" in FIX:
        # the crowd files collaborations and creator-run platforms as sponsor (eval: dbrand-made
        # "my" skins, Lumafield, Nebula were all misfiled as selfpromo)
        question["criteria"] = {
            "true": "the creator's own business or product, sold by the creator",
            "false": "a separate company's product: a sponsor, a brand the creator collaborated with "
                     "(even a co-branded or 'my' product that company makes and sells), or a platform "
                     "such as Nebula or CuriosityStream",
        }
    ans = jev.ask(f"YouTube channel: {channel}\nAd read from one of its videos:\n{body}", {"own": question})
    return ans["own"]["noul"]


def nebula_tags(words: list[Word]) -> list[Segment]:
    """Tom Scott-style "Next time, or right now on Nebula, ..." teasers: the crowd files just the
    "or right now on Nebula" phrase as its own ~2 s sponsor segment (eval: 7 of 7 such phrases on
    his channel). Only this exact form: other "...right now on Nebula" wording isn't marked."""
    out = []
    norm = [re.sub(r"[^a-z]", "", w.text.lower()) for w in words]
    for i in range(len(words) - 5):
        if norm[i] == "next" and norm[i + 1] == "time" and norm[i + 2] == "or" and norm[i + 3] == "right" \
                and norm[i + 4] == "now":
            j = next((k for k in range(i + 5, min(len(words), i + 9)) if norm[k].startswith("nebul")), None)
            if j is None:
                continue
            end = words[j + 1].t if j + 1 < len(words) else words[j].t + 0.6
            seg = Segment("sponsor", 0, 0, start=words[i + 2].t, end=end, conf=0.9)
            seg.notes.append("\"or right now on Nebula\" plug")
            out.append(seg)
    return out


SELFPROMO_RECHECK = float(os.environ.get("SBJEV_SELFPROMO_RECHECK", "0.6"))


def is_self_plug(jev: Jev, lines: list[Line], seg: Segment, title: str) -> bool:
    """Second look at a borderline selfpromo run: is it really a plug, or a passing mention?"""
    lo, hi = max(0, seg.first - 2), min(len(lines), seg.last + 3)
    ans = jev.ask({"video_title": title, "transcript": "\n".join(lines[i].text for i in range(lo, hi))}, {"plug": {
        "type": "noul",
        "instructions": "In this excerpt, does the creator promote something of their own or a friend's: their "
                        "Patreon or membership, merch, a book or course, another channel or show, or another "
                        "creator's channel, asking viewers to go check it out or support it?",
        "criteria": {"true": "a plug that asks viewers to buy, support, subscribe to or visit it",
                     "false": "a passing mention, a question to viewers, credits, or the video's own content"},
    }})
    return ans["plug"]["noul"] >= 0.6


PLATFORMS = re.compile(r"\b(nebula|curiosity ?stream)\b", re.I)


def platform_plug(lines: list[Line], seg: Segment) -> bool:
    """SponsorBlock's crowd files Nebula / CuriosityStream plugs as sponsor even when it's the
    creator's own show there (the platform pays per sign-up). Text match on the segment."""
    return bool(PLATFORMS.search(" ".join(lines[i].text for i in range(seg.first, seg.last + 1))))


def should_join(jev: Jev, lines: list[Line], a: Segment, b: Segment, channel: str) -> bool:
    """a comes first, b second; one is sponsor, the other selfpromo, and they touch or overlap.
    Join (as one sponsor read) when:
      - Nebula/CuriosityStream is plugged anywhere in the pair (the crowd files those as sponsor,
        even for the creator's own show there), or
      - the selfpromo comes FIRST, leading into the ad ("I made a new episode..."), and isn't
        clearly the creator's own product.
    A selfpromo AFTER the sponsor (Patreon thanks, merch sign-off) stays its own segment."""
    pair = Segment(a.cat, min(a.first, b.first), max(a.last, b.last))
    if platform_plug(lines, pair):
        return True
    return a.cat == "selfpromo" and own_product(jev, lines, a, channel) < 0.6


BREAK_TEASER = re.compile(
    r"\b(after|before)\b[^.?!]{0,30}\b(the|this|a|our|today's|these)\s+(short\s+|quick\s+|brief\s+|little\s+)?"
    r"(break|interval|intermission|sponsors?|ads?|word from|message from)\b(?![\w-]|\s+(of|on|in)\b)"
    r"|\b(right after|after) (we|i) (thank|tell you about|hear from)\b"
    r"|\bquick word from\b|\bword from (our|today's|this video's) sponsor\b", re.I)


SIGNOFF = re.compile(
    r"\bthanks?(\s+you)?(\s+(so|very)\s+much)?\s+(for|4)\s+watching\b"
    r"|\bsee\s+(you|ya)(\s+(all|guys))?\s+(in|on)\s+(the|that|my)\s+next\b"
    r"|\blet\s+me\s+know\s+what\s+you\s+think\b"
    r"|^\W*(>>\s*)?(well,?\s+)?next\s+(time|week)\b", re.I)


RETURN_PHRASE = re.compile(
    r"^(?:(?:and|so|okay|ok|alright|all right|anyway|anyways|well)\W+)*(?:now\W+)?"
    r"(?:(?:let'?s|let us)\W+)?(?:(?:get|go|head|jump)\W+)?back\W+to\b", re.I)


def include_return_line(words: list[Word], seg: Segment) -> None:
    """Well-rated sponsor segments end AFTER "and now, back to the video" (11/11 in the
    eval corpus, never before it), but Jev stops where the read's content ends. If the
    next sentence is such a return line, extend the segment to its end."""
    k = next((i for i, w in enumerate(words) if w.t >= seg.end - 0.3), None)
    if k is None or words[k].t - seg.end > 2.0:
        return
    nxt = words[k:k + 16]
    if not RETURN_PHRASE.match(" ".join(w.text.strip() for w in nxt)):
        return
    for j, w in enumerate(nxt):
        if w.text.rstrip().endswith((".", "!", "?")) or (j + 1 < len(nxt) and nxt[j + 1].t - w.t > 1.0):
            after = words[k + j + 1] if k + j + 1 < len(words) else None
            seg.end = after.t if after else w.t + 0.6
            seg.notes.append("includes the 'back to the video' line")
            return


def refine(jev: Jev, lines: list[Line], seg: Segment) -> None:
    """Snap segment edges to word boundaries: Choice over word ids spanning the
    line before/after the edge and the edge line itself."""
    what = DESCRIPTIONS[seg.cat]

    def pick(idxs: list[int], question: str) -> tuple[list[Word], str, float]:
        words = [w for i in idxs for w in lines[i].words]
        ctx_lo, ctx_hi = max(0, idxs[0] - 4), min(len(lines), idxs[-1] + 5)
        tagged = " ".join(f"[W{k:03d}]{w.text.strip()}" for k, w in enumerate(words))
        state = {
            "context_before": " ".join(lines[i].text for i in range(ctx_lo, idxs[0])),
            "candidate_words": tagged,
            "context_after": " ".join(lines[i].text for i in range(idxs[-1] + 1, ctx_hi)),
        }
        ans = jev.ask(state, {"edge": {
            "type": "choice",
            "instructions": question,
            "criteria": {f"W{k:03d}": None for k in range(len(words))},
        }})["edge"]
        return words, ans["choice"], ans["confidence"]

    lo = [i for i in (seg.first - 1, seg.first) if 0 <= i < len(lines)]
    words, c, conf = pick(lo, f"In `candidate_words`, which word is the FIRST word of the {what}? "
                              f"Include its lead-in phrase (e.g. \"thanks to X for sponsoring\").")
    seg.start = words[int(c[1:])].t
    if conf < 0.4:
        seg.notes.append("start uncertain")

    hi = [i for i in (seg.last, seg.last + 1) if 0 <= i < len(lines)]
    words, c, conf = pick(hi, f"In `candidate_words`, which word is the LAST word of the {what}, "
                              f"before regular content resumes?")
    k = int(c[1:])
    # end at the start of the following word (or a beat after the last word)
    all_after = [w for i in range(hi[0], len(lines)) for w in lines[i].words]
    j = all_after.index(words[k])
    seg.end = all_after[j + 1].t if j + 1 < len(all_after) else words[k].t + 0.6
    if conf < 0.4:
        seg.notes.append("end uncertain")


# ---------------------------------------------------------------- sponsorblock

def existing_segments(video_id: str, cats: list[str]) -> list[dict]:
    # the API only returns skip/mute segments unless asked; highlights are actionType "poi"
    r = requests.get(SB_API, params={"videoID": video_id, "categories": json.dumps(cats),
                                     "actionTypes": json.dumps(["skip", "mute", "poi", "full"])}, timeout=20)
    return r.json() if r.status_code == 200 else []


def overlaps(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    return inter / max(1e-6, min(a[1] - a[0], b[1] - b[0]))


def import_url(video_id: str, segs: list[Segment]) -> str:
    payload = [{"actionType": "poi" if s.cat == "poi_highlight" else "skip", "category": s.cat,
                "segment": [round(s.start, 3), round(s.end, 3)]} for s in segs]
    return (f"https://www.youtube.com/watch?v={video_id}#segments="
            + urllib.parse.quote(json.dumps(payload, separators=(",", ":")), safe=""))


# ---------------------------------------------------------------- main

def video_ids(targets: list[str], limit: int) -> list[str]:
    ids = []
    for t in targets:
        if len(t) == 11 and "/" not in t and "." not in t:
            ids.append(t)
            continue
        r = subprocess.run(ytdlp_cmd() + ["--flat-playlist", "--print", "id", "--playlist-end", str(limit),
                            "--no-warnings", t], capture_output=True, text=True)
        got = [x for x in r.stdout.split() if len(x) == 11]
        if not got:
            raise SystemExit(f"could not resolve {t}: {r.stderr.strip()[-300:]}")
        ids += got
    return list(dict.fromkeys(ids))


_AUDIO_WORKER = None


def audio_worker(args):
    """One shared worker process for audio snapping; it exits after
    --audio-idle minutes without work, which gives back all of its VRAM."""
    global _AUDIO_WORKER
    if _AUDIO_WORKER is None:
        from audio_snap import SnapWorker
        _AUDIO_WORKER = SnapWorker(idle_minutes=args.audio_idle)
    return _AUDIO_WORKER


def process(jev: Jev, vid: str, args) -> dict:
    words, duration, title, word_timed, music = fetch_subs(vid, args.cookies_from_browser)
    if not args.no_audio:
        # fetch the audio (and low-res video for scene cuts) in the background while Jev works;
        # the snapping worker waits for these instead of downloading them again
        import threading
        from audio_snap import AudioSnapper
        threading.Thread(target=AudioSnapper(vid, CACHE, duration, args.cookies_from_browser).prefetch,
                         args=(not getattr(args, "no_scene_cuts", False),), daemon=True).start()
    lines = group_lines(words, duration)
    print(f"\n== {title}  [{vid}]  {fmt(duration)}, {len(lines)} lines", file=sys.stderr)
    line_cats = [c for c in args.categories if c in LINE_CATS]
    # what SponsorBlock already has: 29 of 30 eval videos already had their sponsor read, and
    # re-detecting it only to drop it as "already on SponsorBlock" was most of the spend
    known = [] if getattr(args, "ignore_existing", False) else existing_segments(vid, args.categories)
    known_spans = [tuple(e["segment"]) for e in known if e["category"] in LINE_CATS]
    covered = lambda i: any(a + 1 <= lines[i].start and lines[i].end <= b - 1 for a, b in known_spans)  # noqa: E731
    if getattr(args, "skip_known", False):
        # optional: don't look for categories SponsorBlock already has at all. Much cheaper (most
        # videos already have their sponsor read) but a second, unsubmitted read won't be found
        have = {e["category"] for e in known}
        skipped = [c for c in line_cats if c in have]
        line_cats = [c for c in line_cats if c not in have]
        if skipped:
            print(f"    skipping categories already on SponsorBlock: {', '.join(skipped)}", file=sys.stderr)
    segs = []
    if line_cats:
        ranges = None
        if not args.no_scan:
            # cheap scan first (~21 tokens per line), then the ~100-tokens-per-line classification
            # only where something was flagged, with margin for the edges. Scan recall on the
            # eval corpus at 0.1: 100% of sponsor reads, 98% overall (eval/gate_eval.py)
            chunks = [(s0, min(len(lines), s0 + SCAN_CHUNK)) for s0 in range(0, len(lines), SCAN_CHUNK)]
            chunks = [(s0, e0) for s0, e0 in chunks if not all(covered(i) for i in range(s0, e0))]
            flags = parallel(lambda c: max(scan_chunk(jev, lines, c[0], c[1], line_cats).values()), chunks)
            ranges = []
            for (s0, e0), f in zip(chunks, flags):
                if f >= SCAN_THRESHOLD:
                    a, b = max(0, s0 - SCAN_MARGIN), min(len(lines), e0 + SCAN_MARGIN)
                    if ranges and a <= ranges[-1][1]:
                        ranges[-1] = (ranges[-1][0], b)
                    else:
                        ranges.append((a, b))
            print(f"    scan flagged {sum(b - a for a, b in ranges)}/{len(lines)} lines", file=sys.stderr)
        else:
            ranges = [(0, len(lines))]
        # don't pay to re-classify lines that sit inside a segment SponsorBlock already has
        if known_spans:
            trimmed = []
            for a, b in ranges:
                run = None
                for i in range(a, b):
                    if covered(i):
                        if run is not None:
                            trimmed.append((run, i))
                            run = None
                    elif run is None:
                        run = i
                if run is not None:
                    trimmed.append((run, b))
            ranges = trimmed
        probs = classify_lines(jev, lines, line_cats, ranges=ranges)
        # short categories false-positive on passing mentions ("tell me your thoughts",
        # "we did another video"), so they need one line Jev is sure about
        runs = find_runs(probs, line_cats, args.threshold)
        peak = lambda s: max(probs[i][s.cat] for i in range(s.first, s.last + 1))  # noqa: E731
        segs = [s for s in runs if peak(s) >= PEAK.get(s.cat, 0.0)]
        # borderline selfpromo (peak 0.6-0.85: Patreon thanks, the creator's book, a friend's channel)
        # used to be dropped with the passing mentions the 0.85 bar exists for; ask about those directly
        borderline = [s for s in runs if s.cat == "selfpromo" and SELFPROMO_RECHECK <= peak(s) < PEAK["selfpromo"]]
        if borderline:
            ok = parallel(lambda s: is_self_plug(jev, lines, s, title), borderline)
            for s, yes in zip(borderline, ok):
                if yes:
                    s.notes.append("borderline self-promo, confirmed by a second question")
                    segs.append(s)
            segs.sort(key=lambda s: s.first)
    channel = video_info(vid).get("channel") or ""

    def finish(s: Segment) -> None:  # each segment's follow-up questions are independent of the others
        # 0.4: known third-party reads score <=0.26 (one outlier), creator-owned ones that never say
        # "my company" (CrunchLabs) land 0.42-0.53
        if s.cat == "sponsor" and "selfpromo" in args.categories and own_product(jev, lines, s, channel) >= 0.4:
            s.cat = "selfpromo"
            s.notes.append("the creator's own product, so self-promotion")
        elif s.cat == "selfpromo" and "sponsor" in args.categories and platform_plug(lines, s):
            s.cat = "sponsor"
            s.notes.append("Nebula/CuriosityStream plug: SponsorBlock files these as sponsor")
        elif "own" in FIX and s.cat == "selfpromo" and "sponsor" in args.categories \
                and own_product(jev, lines, s, channel) < 0.3:
            s.cat = "sponsor"
            s.notes.append("another company's product, so sponsor")
        if SENTENCE_EDGES and s.cat in ("sponsor", "selfpromo"):
            s.start, s.end = lines[s.first].start, lines[s.last].end
            sentence_edges(jev, words, s, title)
        else:
            extend_leadin(jev, lines, s)
            s.start, s.end = lines[s.first].start, lines[s.last].end
            if word_timed and not args.no_refine:
                refine(jev, lines, s)
        if s.end <= s.start:
            s.start, s.end = lines[s.first].start, lines[s.last].end
            s.notes.append("refine collapsed; using line bounds")
        if s.cat in ("sponsor", "selfpromo"):
            include_return_line(words, s)

    parallel(finish, segs)
    # sentence edges can stretch a selfpromo run into the sponsor run after it (or vice versa);
    # join them by time (see should_join); done after edges so each half keeps its own traced-back start
    joined: list[Segment] = []
    for s in sorted(segs, key=lambda s: s.start):
        prev = next((p for p in reversed(joined) if p.cat in ("sponsor", "selfpromo")), None)
        if (prev and s.cat in ("sponsor", "selfpromo") and s.cat != prev.cat and s.start <= prev.end + 3.0
                and "sponsor" in args.categories):
            if should_join(jev, lines, prev, s, channel):
                prev.start, prev.end = min(prev.start, s.start), max(prev.end, s.end)
                prev.first, prev.last = min(prev.first, s.first), max(prev.last, s.last)
                prev.cat = "sponsor"
                prev.conf = max(prev.conf, s.conf)
                prev.notes.append("self-promo and sponsor runs joined into one ad read")
                continue
        joined.append(s)
    segs = joined

    o = None
    if "outro" in args.categories and any(e["category"] == "outro" for e in known):
        pass  # SponsorBlock already has the outro
    elif "outro" in args.categories and (o := detect_outro(jev, lines, words, duration)):
        spoken = not any(n.startswith("no speech") for n in o.notes)
        if word_timed and not args.no_refine and spoken:
            line_start = o.start
            refine(jev, lines, o)
            if not line_start - 8 <= o.start <= lines[o.first].end:
                o.start = line_start  # word pick wandered off the chosen line
        o.end = duration
        # a sponsor read that closes the video is not the outro; start after it
        sponsor_spans = [(s.start, s.end) for s in segs if s.cat in ("sponsor", "selfpromo")] + \
                        [tuple(e["segment"]) for e in existing_segments(vid, ["sponsor", "selfpromo"])]
        for a, b in sponsor_spans:
            if a < o.end and b > o.start:
                o.start = max(o.start, b)
                o.notes.append("starts after the sponsor read")
        if duration - o.start < MIN_SECONDS["outro"]:
            o = None
    if "outro" in args.categories and o:
        # anything line-labelled inside the sign-off ("subscribe", "come back Friday") is just the outro
        segs = [s for s in segs if s.cat in ("sponsor", "selfpromo") or s.start < o.start - 1]
        segs.append(o)
    if "intro" in args.categories and (i := detect_intro(words, music)):
        segs.append(i)
    if "sponsor" in args.categories:
        ads = [(s.start, s.end) for s in segs if s.cat == "sponsor"]
        segs += [t for t in nebula_tags(words) if not any(a - 1 <= t.start and t.end <= b + 1 for a, b in ads)]
    if "poi_highlight" in args.categories and not any(e["category"] == "poi_highlight" for e in known) \
            and (hl := detect_highlight(jev, lines, title)):
        segs.append(hl)
    segs.sort(key=lambda s: s.start)
    segs = [s for s in segs if s.end - s.start >= MIN_SECONDS[s.cat] or "Nebula\" plug" in " ".join(s.notes)]

    if segs and not args.no_audio:
        print(f"    snapping {len(segs)} segment(s) to the audio", file=sys.stderr)
        edges = []
        for s in segs:
            silent_start = s.cat == "intro" or any(n.startswith("no speech") for n in s.notes)
            # a segment that opens on speech is cut just before the first word (onset);
            # one that opens on silence/music is cut just after the last word before it
            edges += [(s.start, "offset" if silent_start else "onset", "start"),
                      (s.end, "onset" if s.cat in ("intro", "poi_highlight") else "offset",
                       "start" if s.cat == "poi_highlight" else "end")]
        try:
            snapped = audio_worker(args).snap(vid, CACHE, duration, args.cookies_from_browser, edges, words,
                                              scene_cuts=not getattr(args, "no_scene_cuts", False))
            for n, s in enumerate(segs):
                (s.start, n1), (s.end, n2) = snapped[2 * n], snapped[2 * n + 1]
                if s.cat == "poi_highlight":  # a timestamp: both "edges" are the same onset
                    s.end, n2 = s.start, None
                s.notes += [x for x in (n1, n2) if x]
                if not any(x and x.startswith("audio:") for x in (n1, n2)):
                    s.notes.append("edges snapped to audio")
        except Exception as e:  # audio is a refinement; never lose the Jev result over it
            print(f"    audio snapping failed: {e}", file=sys.stderr)
            for s in segs:
                s.notes.append("audio snapping failed; caption times")

    # lead-in extension and edge snapping can push two runs of the same category into each
    # other (two ad reads back to back); SponsorBlock wants one segment for that
    merged: list[Segment] = []
    for s in sorted(segs, key=lambda s: s.start):
        prev = next((m for m in reversed(merged) if m.cat == s.cat), None)
        if prev and s.cat != "poi_highlight" and s.start <= prev.end + 1.0:
            prev.end = max(prev.end, s.end)
            prev.conf = max(prev.conf, s.conf)
            prev.notes += [n for n in s.notes if n not in prev.notes] + ["merged with an overlapping run"]
        else:
            merged.append(s)
    segs = merged

    # yt-dlp's duration is rounded to whole seconds, so a segment "to the end" can stop a
    # fraction short; SponsorBlock clamps anything past the end, so overshoot by a second
    for s in segs:
        if s.cat != "poi_highlight" and s.end >= duration - 1.5:
            s.end = duration + 1.0

    existing = known if not getattr(args, "ignore_existing", False) else existing_segments(vid, args.categories)
    fresh = []
    for s in segs:
        if s.cat == "poi_highlight":  # a video has one highlight; any existing one wins
            hit = next((e for e in existing if e["category"] == "poi_highlight"), None)
        else:
            hit = next((e for e in existing if e["category"] == s.cat
                        and overlaps((s.start, s.end), tuple(e["segment"])) > 0.5), None)
        if hit:
            s.notes.append(f"already on SponsorBlock ({fmt(hit['segment'][0])}-{fmt(hit['segment'][1])}, "
                           f"{hit['votes']} votes)")
        else:
            fresh.append(s)

    for s in segs:
        mark = " " if s in fresh else "="
        low = " LOW-CONF" if s.conf < args.review_below else ""
        print(f"  {mark} {s.cat:<11} {fmt(s.start)}-{fmt(s.end)} ({s.end - s.start:5.1f}s) "
              f"conf {s.conf:.2f}{low}  {'; '.join(dict.fromkeys(s.notes))}")
        print(f"      \"{lines[s.first].text[:90]}\"")
    if not segs:
        print("  (nothing found)")
    url = import_url(vid, fresh) if fresh else None
    if url:
        print(f"  import link: {url}")
        if args.open:
            webbrowser.open(url)
    elif segs:
        print("  everything found is already on SponsorBlock")
    return {"id": vid, "title": title, "url": url,
            "segments": [{"category": s.cat, "segment": [s.start, s.end], "confidence": s.conf,
                          "new": s in fresh, "notes": list(dict.fromkeys(s.notes)),
                          "text": "(no speech)" if s.cat == "intro" or any(n.startswith("no speech") for n in s.notes)
                          else lines[s.first].text}
                         for s in segs]}


def serve(jev: Jev, args, port: int) -> None:
    """Local endpoint for the userscript: GET /run?v=VIDEO_ID -> process() JSON.
    Bound to 127.0.0.1 only; runs one video at a time."""
    import re
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    lock = threading.Lock()
    state = {"busy": None, "since": 0.0}  # the video being processed, for /ping

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "https://www.youtube.com")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass  # the tab went away mid-request; the result is cached for next time

        def do_GET(self):
            try:
                self._route()
            except Exception as e:  # never let one bad request take the server down
                print(f"request failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                self._send(500, {"error": f"server error: {e}", "kind": "server"})

        def _route(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path == "/ping":
                return self._send(200, {"ok": True, "backend": jev.backend, "account": jev.account(),
                                        "busy": state["busy"],
                                        "busy_for": round(time.time() - state["since"]) if state["busy"] else 0,
                                        "categories": list(CATEGORIES), "defaults": args.categories})
            vid = q.get("v", [""])[0]
            if u.path != "/run" or not re.fullmatch(r"[\w-]{11}", vid):
                return self._send(400, {"error": "use /run?v=<11-char video id>", "kind": "bad_request"})
            run_args = argparse.Namespace(**vars(args))
            cats = q.get("c", [""])[0]
            if cats:
                run_args.categories = [c for c in cats.split(",") if c in CATEGORIES] or args.categories
            run_args.skip_known = q.get("skip_known", ["0"])[0] == "1" or getattr(args, "skip_known", False)
            with lock:
                state["busy"], state["since"] = vid, time.time()
                before = jev.cost
                try:
                    result = process(jev, vid, run_args)
                except Exception as e:
                    print(f"{vid}: {e}", file=sys.stderr, flush=True)
                    return self._send(500, {"error": str(e), "kind": error_kind(str(e)),
                                            "account": jev.account()})
                finally:
                    state["busy"] = None
            result["cost"] = jev.cost - before
            result["account"] = jev.account()
            self._send(200, result)

        def log_message(self, *a):
            pass

    class Server(ThreadingHTTPServer):
        # on Windows SO_REUSEADDR lets a second server bind the same port and silently steal
        # requests; refuse instead so the background launcher can't double-start
        allow_reuse_address = False
        daemon_threads = True

    try:
        httpd = Server(("127.0.0.1", port), Handler)
    except OSError:
        print(f"port {port} is already in use (another sbjev server running?)", file=sys.stderr)
        sys.exit(3)
    print(f"sbjev serving on http://127.0.0.1:{port} (backend: {jev.backend})", file=sys.stderr, flush=True)
    httpd.serve_forever()


def error_kind(msg: str) -> str:
    """A short code the userscript turns into advice."""
    m = msg.lower()
    for kind, needles in (("bot_check", ("bot check", "not a bot", "sign in")),
                          ("rate_limited", ("429", "rate-limit")),
                          ("no_captions", ("no captions",)),
                          ("credit", ("free credit", "out of credit", "insufficient balance", "402")),
                          ("jev_busy", ("gave up after repeated 429/5xx",)),
                          ("unavailable", ("private video", "unavailable", "members-only", "age"))):
        if any(n in m for n in needles):
            return kind
    return "other"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="*", help="video URL/ID, playlist, or channel URL")
    ap.add_argument("--serve", type=int, nargs="?", const=int(CONFIG.get("port", 8765)), metavar="PORT",
                    help="run the local endpoint for the userscript (port from config.json, default 8765)")
    ap.add_argument("-c", "--categories", default=",".join(CONFIG.get("categories") or DEFAULT_CATS),
                    help=f"comma list from {','.join(CATEGORIES)} (default: %(default)s)")
    ap.add_argument("-t", "--threshold", type=float, default=0.5, help="min line probability (default 0.5)")
    ap.add_argument("--review-below", type=float, default=0.75, help="flag segments under this mean confidence")
    ap.add_argument("--limit", type=int, default=20, help="max videos per playlist/channel")
    ap.add_argument("--no-refine", action="store_true", help="skip word-level edge snapping")
    ap.add_argument("--skip-known", action="store_true", default=bool(CONFIG.get("skip_known_categories")),
                    help="don't look for categories SponsorBlock already has (cheaper; misses second reads)")
    ap.add_argument("--ignore-existing", action="store_true",
                    help="analyse parts SponsorBlock already covers too (for evaluating against it)")
    ap.add_argument("--no-scan", action="store_true",
                    help="classify every line instead of scanning first (about 2x the cost)")
    ap.add_argument("--no-audio", action="store_true", default=not CONFIG.get("audio_snapping", True),
                    help="skip snapping edges to the audio with local Whisper + VAD")
    ap.add_argument("--no-scene-cuts", action="store_true", default=not CONFIG.get("scene_cuts", True),
                    help="don't line edges up with visual cuts (skips the low-res video download)")
    ap.add_argument("--audio-idle", type=float, default=float(CONFIG.get("audio_idle_minutes", 30)), metavar="MIN",
                    help="stop the audio worker (freeing all its VRAM) after this many idle minutes (default 30)")
    ap.add_argument("--open", action="store_true", help="open each import link in the default browser")
    ap.add_argument("--cookies-from-browser", default=CONFIG.get("cookies_from_browser") or None,
                    help="passed to yt-dlp if YouTube wants sign-in (e.g. firefox)")
    ap.add_argument("--model", default=CONFIG.get("model") or "jev-1.14.0",
                    help="Jev version (pinned so cached answers never mix versions)")
    ap.add_argument("--json", help="write results to this JSON file")
    args = ap.parse_args()
    args.categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    bad = set(args.categories) - set(CATEGORIES)
    if bad:
        ap.error(f"unknown categories: {bad}")

    sys.stdout.reconfigure(encoding="utf-8")
    jev = Jev(args.model)
    if args.serve:
        return serve(jev, args, args.serve)
    if not args.targets:
        ap.error("give a video/playlist/channel, or --serve")
    print(f"Jev backend: {jev.backend}", file=sys.stderr)
    results = []
    for vid in video_ids(args.targets, args.limit):
        try:
            results.append(process(jev, vid, args))
        except Exception as e:  # keep going through a playlist
            print(f"\n== {vid}: {e}", file=sys.stderr)
    print(f"\nJev: {jev.calls} calls, {jev.tokens:,} input tokens, ${jev.cost:.4f} this run", file=sys.stderr)
    if acct := jev.account():
        print(f"Account: ${acct['spent']:.4f} spent so far, ${acct['balance']:.2f} balance", file=sys.stderr)
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
    if _AUDIO_WORKER:
        _AUDIO_WORKER.stop()


if __name__ == "__main__":
    main()
