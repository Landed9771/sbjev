"""Would a cheap chunk-level scan miss crowd segments?

Splits each corpus video's transcript into chunks and asks one Noul per
category per chunk. Reports, per threshold, how many well-rated crowd
segments fall in a flagged chunk (recall) and what fraction of chunks get
flagged (≈ the share of line-level classification still needed).
Costs a little Jev credit (~0.03¢ per video).

usage: python eval/gate_eval.py [n_videos] [chunk_lines]"""

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sbjev  # noqa: E402

HERE = Path(__file__).parent
CORPUS = json.loads((HERE / "corpus.json").read_text())
CATS = ["sponsor", "selfpromo", "interaction"]


def main():
    n_videos = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    chunk = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    jev = sbjev.Jev("jev-latest")
    hits = []  # (category, max noul over chunks the segment overlaps)
    flags = []  # per chunk: {cat: noul}
    done = 0
    for vid, info in CORPUS.items():
        if done >= n_videos:
            break
        segs = [s for s in info["segments"] if s["category"] in CATS]
        if not segs:
            continue
        try:
            words, duration, title, *_ = sbjev.fetch_subs(vid, None)
        except Exception as e:
            print(vid, e, file=sys.stderr)
            continue
        lines = sbjev.group_lines(words, duration)
        spans = []
        for s0 in range(0, len(lines), chunk):
            e0 = min(len(lines), s0 + chunk)
            nouls = sbjev.scan_chunk(jev, lines, s0, e0, CATS)
            spans.append((lines[s0].start, lines[e0 - 1].end, nouls))
            flags.append(nouls)
        for s in segs:
            a, b = s["segment"]
            over = [n for (x, y, n) in spans if x < b and y > a]
            hits.append((s["category"], max((n[s["category"]] for n in over), default=0.0),
                         max((max(n.values()) for n in over), default=0.0)))
        done += 1
        print(f"[{done}] {vid} {len(spans)} chunks", file=sys.stderr)
    print(f"{done} videos, {len(flags)} chunks, {len(hits)} crowd segments; Jev spend ${jev.cost:.4f}")
    for thr in (0.1, 0.2, 0.3, 0.5):
        own = np.mean([h[1] >= thr for h in hits])
        anyc = np.mean([h[2] >= thr for h in hits])
        flagged = np.mean([max(f.values()) >= thr for f in flags])
        by = {c: f"{np.mean([h[1] >= thr for h in hits if h[0] == c]):.0%}" for c in CATS}
        print(f"thr {thr}: recall (own category) {own:.0%} {by}  (any category) {anyc:.0%}  chunks flagged {flagged:.0%}")
    for h, (cat, own, anyc) in zip(range(len(hits)), hits):
        pass


if __name__ == "__main__":
    main()
