"""Fit and score the audio snapper's cut placement against well-rated crowd edges.

For each crowd edge (eval/edges.json), hand the snapper what the Jev pipeline
would (the caption time of the boundary word), record the pause it finds, and
where the crowd put the cut inside that pause. Fits START_AFTER / END_BEFORE on
even-numbered videos, scores on odd-numbered ones (and reports the fit on all).
No Jev calls.

usage: python eval/fit_conventions.py"""

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sbjev  # noqa: E402
from audio_snap import AudioSnapper  # noqa: E402

HERE = Path(__file__).parent
EDGES = json.loads((HERE / "edges.json").read_text())
CACHE_FILE = HERE / "gaps.json"


def collect():
    rows = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else []
    seen = {(r["vid"], r["t"]) for r in rows}
    for n, (vid, info) in enumerate(EDGES.items()):
        if "edges" not in info:
            continue
        todo = [e for e in info["edges"] if (vid, e["t"]) not in seen
                and e["category"] != "intro" and not e["mid_speech"]]
        if not todo:
            continue
        words, duration, *_ = sbjev.fetch_subs(vid, None)
        snapper = AudioSnapper(vid, sbjev.CACHE, duration)
        for e in todo:
            t, side = e["t"], e["side"]
            # what the pipeline hands over: the first caption word of the segment (start) /
            # the first caption word after it (end, = seg.end before snapping)
            nxt = next((w for w in words if w.t >= t - 0.15), None)
            if nxt is None:
                continue
            if side == "start" and e["category"] == "outro" and (e["next_onset"] or 9) > 2:
                continue  # silent end screen: a different kind of edge
            kind = "onset" if side == "start" else "offset"
            got, note = snapper.snap(nxt.t, kind, words, side)
            rows.append({"vid": vid, "cat": e["category"], "side": side, "t": t, "got": got, "note": note,
                         "gap": snapper.last_gap, "votes": e["votes"], "locked": e["locked"], "vid_n": n})
        CACHE_FILE.write_text(json.dumps(rows, indent=0))
        print(f"[{n}] {vid}: {len(todo)} edges", file=sys.stderr)
    return rows


def main():
    rows = collect()
    ok = [r for r in rows if r["gap"]]
    print(f"{len(rows)} edges, {len(ok)} with a pause found; outcomes:",
          {k: sum((r['note'] or 'snapped') == k for r in rows) for k in {(r['note'] or 'snapped') for r in rows}})
    for side, param in (("start", "START_AFTER"), ("end", "END_BEFORE")):
        pos = lambda r: (r["t"] - r["gap"][0]) if side == "start" else (r["gap"][1] - r["t"])  # noqa: E731
        inside = [r for r in ok if r["side"] == side and r["gap"][0] - 0.3 <= r["t"] <= r["gap"][1] + 0.3]
        train = [pos(r) for r in inside if r["vid_n"] % 2 == 0]
        test = [r for r in inside if r["vid_n"] % 2 == 1]
        fit = float(np.median(train))
        errs = []
        for r in test:
            g0, g1 = r["gap"]
            new = min(g0 + fit, g1) if side == "start" else max(g1 - fit, g0)
            errs.append(new - r["t"])
        a = np.abs(errs)
        print(f"{side}: crowd cut inside the found pause for {len(inside)}/{sum(r['side'] == side for r in ok)}; "
              f"{param} fit on half = {fit:.3f}s (all: {np.median([pos(r) for r in inside]):.3f}s)")
        print(f"   held-out n={len(a)}: median |err| {np.median(a):.3f}s  <=0.1s {np.mean(a <= .1):.0%}  "
              f"<=0.2s {np.mean(a <= .2):.0%}  <=0.5s {np.mean(a <= .5):.0%}  <=1s {np.mean(a <= 1):.0%}")
        cur = [r["got"] - r["t"] for r in ok if r["side"] == side]
        print(f"   current code on all found pauses: median |err| {np.median(np.abs(cur)):.3f}s  "
              f"<=0.2s {np.mean(np.abs(cur) <= .2):.0%}  <=1s {np.mean(np.abs(cur) <= 1):.0%}")
        far = [r for r in ok if r["side"] == side and not (r["gap"][0] - 0.3 <= r["t"] <= r["gap"][1] + 0.3)]
        for r in far[:6]:
            print(f"   crowd outside pause: {r['vid']} {r['cat']} crowd {r['t']:.2f} pause {r['gap'][0]:.2f}-{r['gap'][1]:.2f}")


if __name__ == "__main__":
    main()
