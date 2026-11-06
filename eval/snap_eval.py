"""How close does audio snapping land to well-rated crowd edges?

Simulates what the Jev pipeline hands the snapper: the caption time of the
boundary word (start edge = first caption word at/after the crowd start;
end edge = first caption word at/after the crowd end, which is what
seg.end is set to). Runs the real snapper in-process and reports signed
errors (ours - crowd). No Jev calls.

usage: python eval/snap_eval.py [limit]"""

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sbjev  # noqa: E402
from audio_snap import AudioSnapper  # noqa: E402

CORPUS = json.loads(Path(__file__).with_name("corpus.json").read_text())
EDGES = json.loads(Path(__file__).with_name("edges.json").read_text())
FIRST = int(sys.argv[1]) if len(sys.argv) > 1 else 60  # videos before this were used for fitting
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 999


def main():
    errs = {"start": [], "end": []}
    notes = {}
    rows = []
    order = list(json.loads(Path(__file__).with_name("corpus.json").read_text()))
    for vid, info in EDGES.items():
        if not FIRST <= order.index(vid) < LIMIT:
            continue
        if "edges" not in info:
            continue
        words, duration, *_ = sbjev.fetch_subs(vid, None)
        snapper = AudioSnapper(vid, sbjev.CACHE, duration)
        for e in info["edges"]:
            if e["category"] in ("intro",) or e["mid_speech"]:
                continue  # intros are music/silence edges; mid-speech crowd cuts are a different problem
            t = e["t"]
            nxt = next((w for w in words if w.t >= t - 0.15), None)
            if nxt is None:
                continue
            kind = "onset" if e["side"] == "start" else "offset"
            silent_start = e["side"] == "start" and e["category"] == "outro" and (e["next_onset"] or 9) > 2
            if silent_start:
                continue
            got, note = snapper.snap(nxt.t, kind, words, e["side"])
            err = got - t
            errs[e["side"]].append(err)
            notes[note or "snapped"] = notes.get(note or "snapped", 0) + 1
            rows.append((vid, e["category"], e["side"], round(t, 2), round(got, 2), round(err, 2), note or ""))
    Path(__file__).with_name("snap_eval.json").write_text(json.dumps(rows, indent=0))
    for side, xs in errs.items():
        a = np.abs(xs)
        if not len(a):
            continue
        print(f"{side:5} n={len(a):3}  median |err| {np.median(a):.2f}s  mean signed {np.mean(xs):+.2f}s  "
              f"<=0.1s {np.mean(a <= 0.1):.0%}  <=0.25s {np.mean(a <= 0.25):.0%}  <=0.5s {np.mean(a <= 0.5):.0%}  "
              f"<=1s {np.mean(a <= 1):.0%}")
    print("outcomes:", notes)


if __name__ == "__main__":
    main()
