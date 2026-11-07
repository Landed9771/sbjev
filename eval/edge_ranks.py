"""Where does the crowd's boundary sentence rank in Jev's start/end choice distributions?

Replays the pipeline from cache (SBJEV_CACHE_ONLY=1 recommended), records every sentence_edges
start/end Choice (probabilities over sentence ids) and, for segments matched to a crowd segment,
the rank of the sentence the crowd's edge falls on. Free with a warm cache.

usage: SBJEV_CACHE_ONLY=1 python eval/edge_ranks.py [first] [count]"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sbjev  # noqa: E402

HERE = Path(__file__).parent
CORPUS = json.loads((HERE / "corpus.json").read_text())


def ov(a, b):
    return max(0, min(a[1], b[1]) - max(a[0], b[0])) / max(1e-6, min(a[1] - a[0], b[1] - b[0]))


def main():
    first = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    args = argparse.Namespace(categories=["sponsor", "selfpromo", "interaction", "outro"], threshold=0.5,
                              review_below=0.75, no_refine=False, no_audio=True, no_scan=False, open=False,
                              cookies_from_browser=None, audio_idle=30, ignore_existing=True, no_scene_cuts=True)
    jev = sbjev.Jev("jev-1.14.0")
    calls = []  # every start/end choice made during the current video
    orig = sbjev.Jev.ask

    def spy(self, state, questions, retries=6):
        a = orig(self, state, questions, retries)
        for key in ("start", "end"):
            if key in questions and key in a and "probabilities" in a[key]:
                calls.append({"kind": key, "probs": a[key]["probabilities"]})
        return a
    sbjev.Jev.ask = spy

    rows = []
    for vid in list(CORPUS)[first:first + count]:
        calls.clear()
        try:
            r = sbjev.process(jev, vid, args)
        except Exception as e:
            print(vid, "skip:", e, file=sys.stderr)
            continue
        words, duration, *_ = sbjev.fetch_subs(vid, None)
        sents = sbjev.split_sentences(words)
        sid = lambda i: f"S{i:04d}"  # noqa: E731
        info = CORPUS[vid]
        for s in info["segments"]:
            if s["category"] not in ("sponsor", "selfpromo"):
                continue
            m = [x for x in r["segments"] if x["category"] == s["category"] and ov(x["segment"], s["segment"]) > 0.5]
            if not m:
                continue
            for kind, crowd_t, ours_t in (("start", s["segment"][0], m[0]["segment"][0]),
                                          ("end", s["segment"][1], m[0]["segment"][1])):
                if kind == "end" and crowd_t > info["duration"] - 2:
                    continue
                # the sentence the crowd edge belongs to: for a start, the first sentence at/after it;
                # for an end, the last sentence before it
                if kind == "start":
                    ci = next((i for i, x in enumerate(sents) if x.start >= crowd_t - 0.6), None)
                else:
                    ci = max((i for i, x in enumerate(sents) if x.start < crowd_t - 0.3), default=None)
                # the choice call whose options contain both our pick and the crowd sentence
                cand = [c for c in calls if c["kind"] == kind and ci is not None and sid(ci) in c["probs"]]
                rank, p = None, None
                if cand:
                    probs = cand[-1]["probs"]
                    order = sorted(probs, key=probs.get, reverse=True)
                    rank = order.index(sid(ci)) + 1
                    p = probs[sid(ci)]
                rows.append({"vid": vid, "kind": kind, "err": round(ours_t - crowd_t, 2), "crowd_rank": rank,
                             "crowd_p": p, "in_window": bool(cand)})
    (HERE / f"edge_ranks_{first}.json").write_text(json.dumps(rows, indent=1))
    for kind in ("start", "end"):
        xs = [r for r in rows if r["kind"] == kind]
        bad = [r for r in xs if abs(r["err"]) > 1.0]
        print(f"{kind}: {len(xs)} edges, {len(bad)} off by >1 s")
        print(f"   of those: crowd sentence not among the options {sum(not r['in_window'] for r in bad)}, "
              f"ranked 2nd {sum(r['crowd_rank'] == 2 for r in bad)}, 3rd {sum(r['crowd_rank'] == 3 for r in bad)}, "
              f"4th+ {sum((r['crowd_rank'] or 0) >= 4 for r in bad)}")
        for r in bad:
            print(f"   {r['vid']} err {r['err']:+7.2f}s  crowd rank {r['crowd_rank']}  p={r['crowd_p']}")
    print("spend", jev.cost)


if __name__ == "__main__":
    main()
