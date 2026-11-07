"""Collect a ground-truth corpus: SponsorBlock segments that are locked or
well voted, on videos from channels the SponsorBlock crowd covers heavily.
Writes eval/corpus.json. No Jev calls."""

import json
import subprocess
import sys
import time
from pathlib import Path

import requests

CHANNELS = [
    "@LinusTechTips", "@veritasium", "@mkbhd", "@kurzgesagt", "@MarkRober", "@TechLinked",
    "@Mrwhosetheboss", "@JerryRigEverything", "@RealEngineering", "@wendoverproductions",
    "@SmarterEveryDay", "@CGPGrey", "@HalfAsInteresting", "@ShortCircuit", "@Vox",
]
CATS = ["sponsor", "selfpromo", "interaction", "intro", "outro"]
MIN_VOTES = 3
PER_CHANNEL = 12
OUT = Path(__file__).with_name("corpus.json")


def main():
    # optional: python eval/build_corpus.py @Channel1,@Channel2 [per_channel] [first_item]
    channels = sys.argv[1].split(",") if len(sys.argv) > 1 else CHANNELS
    per = int(sys.argv[2]) if len(sys.argv) > 2 else PER_CHANNEL
    first = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    corpus = json.loads(OUT.read_text()) if OUT.exists() else {}
    for ch in channels:
        r = subprocess.run(["yt-dlp", "--flat-playlist", "--print", "id", "--playlist-items", f"{first}:{first - 1 + per}",
                            f"https://www.youtube.com/{ch}/videos"], capture_output=True, text=True)
        ids = r.stdout.split()
        kept = 0
        for vid in ids:
            if vid in corpus:
                continue
            resp = requests.get("https://sponsor.ajay.app/api/skipSegments",
                                params={"videoID": vid, "categories": json.dumps(CATS)}, timeout=20)
            time.sleep(0.3)
            if resp.status_code != 200:
                continue
            good = [s for s in resp.json() if s["locked"] or s["votes"] >= MIN_VOTES]
            if good:
                corpus[vid] = {"channel": ch, "duration": good[0]["videoDuration"],
                               "segments": [{"category": s["category"], "segment": s["segment"],
                                             "votes": s["votes"], "locked": s["locked"]} for s in good]}
                kept += 1
        print(f"{ch}: {kept} videos with good segments", file=sys.stderr)
        OUT.write_text(json.dumps(corpus, indent=1))
    segs = [s for v in corpus.values() for s in v["segments"]]
    print(f"{len(corpus)} videos, {len(segs)} segments:",
          {c: sum(s["category"] == c for s in segs) for c in CATS})


if __name__ == "__main__":
    main()
