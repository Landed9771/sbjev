"""End-to-end: what does the pipeline find vs well-rated crowd segments, and what does it cost?

Runs process() (no audio snapping, captions from cache) on corpus videos in a
given mode and reports recall of crowd segments, extra segments, and Jev spend
per video minute. Costs Jev credit.

usage: python eval/e2e_eval.py scan|noscan [n_videos] [skip]"""

import argparse
import os
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sbjev  # noqa: E402

HERE = Path(__file__).parent
CORPUS = json.loads((HERE / "corpus.json").read_text())
CATS = ["sponsor", "selfpromo", "interaction", "outro"]


def overlap(a, b):
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    return inter / max(1e-6, min(a[1] - a[0], b[1] - b[0]))


def main():
    mode = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    skip = int(sys.argv[3]) if len(sys.argv) > 3 else 40  # gate_eval used the first 40
    args = argparse.Namespace(categories=CATS, threshold=0.5, review_below=0.75, no_refine=False,
                              no_audio=os.environ.get("SBJEV_AUDIO") != "1",
                              no_scene_cuts=os.environ.get("SBJEV_SCENES") != "1",
                              no_scan=(mode == "noscan"), open=False, cookies_from_browser=None, audio_idle=30,
                              ignore_existing=True)  # the crowd segments ARE the answer key
    jev = sbjev.Jev(os.environ.get("SBJEV_MODEL", "jev-latest"))
    found = total = extra = 0
    minutes = 0.0
    rows = []
    starts, ends = [], []
    for vid, info in list(CORPUS.items())[skip:skip + n]:
        crowd = [s for s in info["segments"] if s["category"] in CATS]
        before = jev.cost
        try:
            r = sbjev.process(jev, vid, args)
        except Exception as e:
            print(vid, "ERR", e, file=sys.stderr)
            continue
        ours = [(s["category"], s["segment"]) for s in r["segments"]]
        hit = sum(any(c == s["category"] and overlap(seg, s["segment"]) > 0.5 for c, seg in ours) for s in crowd)
        for s in crowd:
            m = [seg for c, seg in ours if c == s["category"] and overlap(seg, s["segment"]) > 0.5]
            if m and s["category"] in ("sponsor", "selfpromo"):
                starts.append(m[0][0] - s["segment"][0])
                if s["segment"][1] < info["duration"] - 2:
                    ends.append(m[0][1] - s["segment"][1])
        ext = sum(not any(c == s["category"] and overlap(seg, s["segment"]) > 0.5 for s in crowd) for c, seg in ours)
        found, total, extra = found + hit, total + len(crowd), extra + ext
        minutes += info["duration"] / 60
        rows.append({"vid": vid, "crowd": len(crowd), "found": hit, "extra": ext, "cost": jev.cost - before,
                     "extras": [(c, [round(x) for x in seg]) for c, seg in ours
                                if not any(c == s["category"] and overlap(seg, s["segment"]) > 0.5 for s in crowd)]})
    (HERE / f"e2e_{mode}.json").write_text(json.dumps(rows, indent=1))
    print(f"{mode}: {len(rows)} videos, {minutes:.0f} min of video | crowd segments found {found}/{total} "
          f"({found / max(1, total):.0%}) | extra segments {extra} | spend ${jev.cost:.4f} = "
          f"${jev.cost / max(1, minutes) * 10:.5f} per 10 min of video")
    import numpy as np
    for name, xs in (("start", starts), ("end", ends)):
        a = np.abs(xs)
        if len(a):
            print(f"   sponsor/selfpromo {name} error n={len(a)}: median {np.median(a):.2f}s  <=0.5s {np.mean(a <= .5):.0%}  "
                  f"<=1s {np.mean(a <= 1):.0%}  <=3s {np.mean(a <= 3):.0%}  (late/early {np.mean(np.array(xs) > 0):.0%} late)  "
                  f"| <=0.1s {np.mean(a <= .1):.0%}  <=0.25s {np.mean(a <= .25):.0%}")


if __name__ == "__main__":
    main()
