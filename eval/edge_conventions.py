"""Where do well-rated SponsorBlock edges sit relative to the speech?

For every crowd edge in eval/corpus.json (skipping edges at 0 / video end):
  - Silero VAD speech probabilities in a +-4 s window
  - the nearest speech onset after the edge and speech offset before it
  - the caption words on either side
Writes eval/edges.json for analysis. No Jev calls."""

import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sbjev  # noqa: E402
from audio_snap import AudioSnapper, FRAME  # noqa: E402

CORPUS = json.loads(Path(__file__).with_name("corpus.json").read_text())
OUT = Path(__file__).with_name("edges.json")
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 60


def speech_runs(p, thr_on=0.6):
    """Contiguous speech runs in VAD frames as (start_idx, end_idx_exclusive)."""
    runs, cur = [], None
    for i, x in enumerate(p):
        if x >= thr_on and cur is None:
            cur = i
        elif x < thr_on and cur is not None:
            runs.append((cur, i))
            cur = None
    if cur is not None:
        runs.append((cur, len(p)))
    return runs


def main():
    from faster_whisper.vad import get_vad_model
    vad = get_vad_model()
    done = json.loads(OUT.read_text()) if OUT.exists() else {}
    for n, (vid, info) in enumerate(list(CORPUS.items())[:LIMIT]):
        if vid in done:
            continue
        try:
            words, duration, title, timed, _ = sbjev.fetch_subs(vid, None)
        except Exception as e:
            print(vid, "captions:", e, file=sys.stderr)
            done[vid] = {"error": str(e)}
            continue
        snapper = AudioSnapper(vid, sbjev.CACHE, duration)
        rows = []
        try:
            for s in info["segments"]:
                for side, t in (("start", s["segment"][0]), ("end", s["segment"][1])):
                    if t < 1 or t > duration - 1.5:
                        continue
                    a, b = max(0.0, t - 4), min(duration, t + 4)
                    audio = snapper._clip(a, b)
                    p = np.asarray(vad(np.pad(audio, (0, (-len(audio)) % 512)))).reshape(-1)
                    runs = [(a + i * FRAME, a + j * FRAME) for i, j in speech_runs(p)]
                    after = [r for r in runs if r[0] >= t - 0.25]
                    before = [r for r in runs if r[1] <= t + 0.25]
                    inside = [r for r in runs if r[0] < t - 0.25 and r[1] > t + 0.25]
                    rows.append({
                        "category": s["category"], "side": side, "t": t, "votes": s["votes"], "locked": s["locked"],
                        "next_onset": after[0][0] - t if after else None,
                        "prev_offset": t - before[-1][1] if before else None,
                        "mid_speech": bool(inside),
                        "words_before": " ".join(w.text.strip() for w in words if t - 6 <= w.t < t)[-80:],
                        "words_after": " ".join(w.text.strip() for w in words if t <= w.t < t + 6)[:80],
                        "timed": timed,
                    })
        except Exception as e:
            print(vid, "audio:", e, file=sys.stderr)
            done[vid] = {"error": str(e)}
            continue
        done[vid] = {"title": title, "edges": rows}
        OUT.write_text(json.dumps(done, indent=1))
        print(f"[{n + 1}] {vid} {len(rows)} edges", file=sys.stderr)
        time.sleep(2)  # be gentle with YouTube


if __name__ == "__main__":
    main()
