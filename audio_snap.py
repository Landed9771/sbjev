"""Snap segment edges to the actual audio.

Caption word times are ~0.1-0.3 s off and have no word end times. For each
edge this cuts ~10 s of the video's audio around it, runs faster-whisper
(word timestamps) to find the boundary word in the audio, then uses Silero VAD
speech probabilities (32 ms frames; robust to background music, unlike raw
loudness) to put the cut exactly where speech starts (onset) or stops (offset).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np

SR = 16000
FRAME = 512 / SR  # Silero VAD frame, 32 ms
_MODELS: list = []  # [whisper, vad]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


class AudioSnapper:
    def __init__(self, video_id: str, cache: Path, duration: float, cookies_from: str | None = None,
                 scene_cuts: bool = False):
        self.video_id, self.duration = video_id, duration
        self.dir = cache / "audio"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cookies_from = cookies_from
        self.scene_cuts = scene_cuts
        self._src: Path | None = None
        self._cuts_src = None  # low-res video for scene cuts; False once a download failed

    # ---------------------------------------------------------------- resources

    def prefetch(self, video: bool) -> None:
        """Download the audio (and the low-res video for scene cuts) now, while Jev works.
        A <id>.downloading marker tells the worker to wait rather than download again."""
        jobs = [(self.dir, self._download_audio)]
        if video:
            jobs.append((self.dir.parent / "video", self._download_video))
        for folder, download in jobs:
            folder.mkdir(parents=True, exist_ok=True)
            if self._found(folder):
                continue
            marker = folder / f"{self.video_id}.downloading"
            try:
                marker.touch()
                download()
            except Exception:
                pass  # the worker tries again later and reports its own error
            finally:
                marker.unlink(missing_ok=True)

    @staticmethod
    def _wait_for_prefetch(folder: Path, video_id: str, limit: float = 180.0) -> None:
        """The main process starts these downloads early (prefetch); if one is still running,
        wait for it instead of starting a second copy of the same download."""
        import time
        deadline = time.time() + limit
        while any(folder.glob(f"{video_id}.*.part")) or (folder / f"{video_id}.downloading").exists():
            if time.time() > deadline:
                break
            time.sleep(0.5)

    def _found(self, folder: Path) -> list[Path]:
        return [p for p in folder.glob(f"{self.video_id}.*") if p.suffix not in (".part", ".downloading", ".ytdl")]

    def _audio_file(self) -> Path:
        if self._src is None:
            self._wait_for_prefetch(self.dir, self.video_id)
            if not self._found(self.dir):
                self._download_audio()
            self._src = self._found(self.dir)[0]
        return self._src

    def _download_audio(self) -> None:
        # videos with auto-dubbed tracks list several languages; the original
        # one is tagged "original" and is the only one that matches the captions
        ytdlp = ["yt-dlp"] if shutil.which("yt-dlp") else [sys.executable, "-m", "yt_dlp"]
        cmd = ytdlp + ["-q", "--no-warnings", "-f", "ba[format_note*=original]/ba",
                       "--retries", "10", "--fragment-retries", "10", "-o",
                       str(self.dir / "%(id)s.%(ext)s"), f"https://www.youtube.com/watch?v={self.video_id}"]
        if not shutil.which("deno") and shutil.which("node"):  # JS runtime; yt-dlp defaults to deno
            cmd[len(ytdlp):len(ytdlp)] = ["--js-runtimes", "node"]
        if self.cookies_from:
            cmd[len(ytdlp):len(ytdlp)] = ["--cookies-from-browser", self.cookies_from]
        r = None
        for _ in range(3):  # googlevideo sometimes 403s mid-download; a fresh URL usually works
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode == 0 and self._found(self.dir):
                return
            for p in self.dir.glob(f"{self.video_id}.*.part"):
                p.unlink(missing_ok=True)
        raise RuntimeError(f"audio download failed: {r.stderr.strip()[-300:]}")

    def _clip(self, a: float, b: float) -> np.ndarray:
        pcm = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}",
                              "-i", str(self._audio_file()), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                             capture_output=True).stdout
        return np.frombuffer(pcm, np.float32)

    @staticmethod
    def _models():
        # loaded once per process; the worker process is what gets thrown away to free VRAM
        if not _MODELS:
            from faster_whisper import WhisperModel
            from faster_whisper.vad import get_vad_model
            try:
                _MODELS.append(WhisperModel("large-v3-turbo", device="cuda", compute_type="float16"))
            except Exception:  # no usable GPU: slower, same answers
                _MODELS.append(WhisperModel("large-v3-turbo", device="cpu", compute_type="int8"))
            _MODELS.append(get_vad_model())
        return _MODELS[0], _MODELS[1]

    # ---------------------------------------------------------------- snapping

    @staticmethod
    def _align(cap_words: list, k: int, a: float, b: float, ww: list) -> int | None:
        """Index of caption word k in whisper's words. Both transcripts are
        aligned at the character level, so "Boot.dev" vs "Boot dev" or
        "gonna" vs "going to" differences don't break the match."""
        import difflib
        cap = [(i, _norm(w.text)) for i, w in enumerate(cap_words) if a <= w.t <= b and _norm(w.text)]
        if not cap or not ww:
            return None
        cap_s, cap_owner = "", []
        for i, tok in cap:
            cap_s += tok
            cap_owner += [i] * len(tok)
        ww_s, ww_owner = "", []
        for j, (_, _, tok) in enumerate(ww):
            ww_s += tok
            ww_owner += [j] * len(tok)
        sm = difflib.SequenceMatcher(None, cap_s, ww_s, autojunk=False)
        if sm.ratio() < 0.6:
            return None
        # first character of caption word k, mapped through the matching blocks
        pos = next((p for p, o in enumerate(cap_owner) if o == k), None)
        if pos is None:
            return None
        for blk in sm.get_matching_blocks():
            if blk.a <= pos < blk.a + blk.size and blk.size >= 3:
                return ww_owner[blk.b + (pos - blk.a)]
        return None

    def snap(self, t: float, kind: str, cap_words: list, role: str | None = None) -> tuple[float, str | None]:
        """Return (new_time, note). kind is "onset" (the cut sits in the pause
        before a word) or "offset" (in the pause after one); cap_words identify
        which spoken word that is. role says whether this is the segment's
        "start" or "end", which decides where in the pause the cut goes."""
        role = role or ("start" if kind == "onset" else "end")
        self.last_gap = None
        if not cap_words or t <= 0.3 or t >= self.duration - 0.3:
            return t, None
        if kind == "onset":  # the first word inside the cut
            k = min(range(len(cap_words)), key=lambda i: abs(cap_words[i].t - t))
        else:  # the last word before the cut
            k = max((i for i, w in enumerate(cap_words) if w.t < t - 0.05), default=0)
        whisper, vad = self._models()
        # Whisper sometimes stops transcribing partway through a clip (end-of-text after one
        # sentence), so if the edge word isn't covered, retry on tighter windows around it
        target = None
        for before, after in ((5.0, 5.0), (2.5, 2.5), (1.5, 4.0), (4.0, 1.5)):
            a = max(0.0, cap_words[k].t - before)
            b = min(self.duration, cap_words[k].t + after)
            audio = self._clip(a, b)
            if len(audio) < SR:
                return t, None
            segs, _ = whisper.transcribe(audio, word_timestamps=True, vad_filter=False, beam_size=1,
                                         condition_on_previous_text=False)
            ww = [(w.start + a, w.end + a, _norm(w.word)) for s in segs for w in (s.words or [])]
            ww = [w for w in ww if w[2]]
            target = self._align(cap_words, k, a, b, ww)
            if target is not None:
                break
        if target is None:
            return t, "audio: couldn't match the words, kept caption time"
        # the boundary is the pause between two spoken words: before the target word for an
        # onset edge, after it for an offset edge
        if kind == "onset":
            left_end = ww[target - 1][1] if target > 0 else ww[target][0] - 1.0
            right_start = ww[target][0]
        else:
            left_end = ww[target][1]
            right_start = ww[target + 1][0] if target + 1 < len(ww) else ww[target][1] + 1.0

        p = np.asarray(vad(np.pad(audio, (0, (-len(audio)) % 512)))).reshape(-1)
        gap = self._gap(p, a, left_end, right_start)
        self.last_gap = gap
        if gap is None:
            # words run together: no silence to hide the cut in, so split between them
            new = (left_end + right_start) / 2 if right_start > left_end else right_start
            return (new, "audio: no pause at the boundary, cut between the words") if abs(new - t) <= 1.5 \
                else (t, "audio: snap too far, kept caption time")
        g0, g1 = gap
        # the words were matched by text, so trust the pause unless it's nowhere near the
        # caption time (captions lag speech by up to a second or two around music)
        if not g0 - 2.5 <= t <= g1 + 2.5:
            return t, "audio: snap too far, kept caption time"
        new = self._place(g0, g1, role)
        if self.scene_cuts:
            # careful submitters line edges up with the editor's visual cut when there is one inside
            # the pause (median error 0.097 -> 0.068 s on 60 crowd edges; a usable cut on ~1 in 4)
            try:
                cuts = [x for x, score in self._scene_cuts(g0, g1) if score >= 20 and g0 <= x <= g1]
            except Exception:
                cuts = []
            if cuts:
                return min(cuts, key=lambda x: abs(x - new)), "on a scene cut"
        return new, None

    def _video_file(self) -> Path | None:
        """The whole video at the lowest quality YouTube has (a few MB, a few seconds to fetch).
        Downloading only the seconds around an edge looks cheaper but those section downloads
        stall on googlevideo, sometimes for minutes."""
        vdir = self.dir.parent / "video"
        vdir.mkdir(exist_ok=True)
        self._wait_for_prefetch(vdir, self.video_id)
        if not self._found(vdir):
            self._download_video()
        got = self._found(vdir)
        return got[0] if got else None

    def _download_video(self) -> None:
        vdir = self.dir.parent / "video"
        vdir.mkdir(exist_ok=True)
        ytdlp = ["yt-dlp"] if shutil.which("yt-dlp") else [sys.executable, "-m", "yt_dlp"]
        cmd = ytdlp + ["-q", "--no-warnings", "-f", "wv[ext=mp4]/wv", "--retries", "5",
                       "-o", str(vdir / "%(id)s.%(ext)s"), f"https://www.youtube.com/watch?v={self.video_id}"]
        if self.cookies_from:
            cmd[len(ytdlp):len(ytdlp)] = ["--cookies-from-browser", self.cookies_from]
        subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    def _scene_cuts(self, a: float, b: float) -> list[tuple[float, float]]:
        """[(time, score)] of visual scene changes between a and b (ffmpeg scdet, local)."""
        if self._cuts_src is None:
            self._cuts_src = self._video_file() or False
        if not self._cuts_src:
            return []
        err = subprocess.run(["ffmpeg", "-hide_banner", "-ss", f"{max(0.0, a - 1):.3f}", "-t", f"{b - a + 2:.3f}",
                              "-copyts", "-i", str(self._cuts_src), "-vf", "scdet=threshold=6:sc_pass=0,metadata=print",
                              "-an", "-f", "null", "-"], capture_output=True, text=True, timeout=120).stderr
        out = []
        for block in err.split("frame:")[1:]:
            m_t = re.search(r"pts_time:([\d.]+)", block)
            m_s = re.search(r"lavfi\.scd\.score=([\d.]+)", block)
            if m_t and m_s and a <= float(m_t.group(1)) <= b:
                out.append((float(m_t.group(1)), float(m_s.group(1))))
        return out

    SPEECH = 0.85

    @staticmethod
    def _place(g0: float, g1: float, role: str) -> float:
        """Where in the pause the cut goes. Fitted on locked / well-voted SponsorBlock
        edges (eval/fit_conventions.py): starts sit early in the pause, ends late, and
        both drift toward the middle as the pause gets longer."""
        L = g1 - g0
        if role == "start":  # ~0.3 s ahead of the first word, never before the pause opens
            return g0 + min(max(L - 0.30, 0.0), 1.5)
        return g1 - min(max(0.06 + 0.20 * L, 0.0), L, 0.75)

    def _gap(self, p: np.ndarray, a: float, left_end: float, right_start: float) -> tuple[float, float] | None:
        """The silence between two words as (speech stops, speech starts), from VAD
        frame probabilities, or None if there is no dip in speech there."""
        ti = lambda x: int(np.clip((x - a) / FRAME, 0, len(p) - 1))  # noqa: E731
        lo, hi = ti(min(left_end, right_start) - 0.3), ti(max(left_end, right_start) + 0.3)
        if hi <= lo or p[lo:hi + 1].min() > 0.6:
            return None
        m = lo + int(np.argmin(p[lo:hi + 1]))
        i = m
        while i > max(0, lo - 10) and not (p[i] >= self.SPEECH and p[max(0, i - 2):i + 1].mean() >= self.SPEECH):
            i -= 1
        j = m
        while j < min(len(p) - 1, hi + 10) and not (p[j] >= self.SPEECH and p[j:j + 3].mean() >= self.SPEECH):
            j += 1
        return a + (i + 1) * FRAME, a + j * FRAME


# ---------------------------------------------------------------- worker process
#
# CUDA keeps ~500 MB reserved for as long as the process that touched the GPU is
# alive, so the long-running server never loads Whisper itself. It talks to a
# child worker over stdin/stdout (one JSON request per line) and kills that
# worker after it has been idle for a while, which returns all of the VRAM.

def _worker_main() -> None:
    proto = sys.stdout
    sys.stdout = sys.stderr  # library chatter must not corrupt the protocol stream
    for line in sys.stdin:
        req = json.loads(line)
        try:
            words = [SimpleNamespace(text=w, t=t) for w, t in req["words"]]
            snapper = AudioSnapper(req["video_id"], Path(req["cache"]), req["duration"], req.get("cookies"),
                                   scene_cuts=bool(req.get("scene_cuts")))
            out = {"edges": [list(snapper.snap(t, kind, words, *rest)) for t, kind, *rest in req["edges"]]}
        except Exception as e:
            out = {"error": f"{type(e).__name__}: {e}"}
        proto.write(json.dumps(out) + "\n")
        proto.flush()


class SnapWorker:
    """Client side: start the worker on demand, keep it (and the loaded model)
    while it's in use, stop it after `idle_minutes` without requests."""

    def __init__(self, idle_minutes: float = 30):
        self.idle = idle_minutes * 60
        self.proc: subprocess.Popen | None = None
        self.timer: threading.Timer | None = None
        self.lock = threading.Lock()

    def snap(self, video_id: str, cache: Path, duration: float, cookies: str | None,
             edges: list[tuple[float, str]], words: list, scene_cuts: bool = False) -> list[tuple[float, str | None]]:
        with self.lock:
            if self.timer:
                self.timer.cancel()
            if self.proc is None or self.proc.poll() is not None:
                self.proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker"],
                                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                             encoding="utf-8")
            req = {"video_id": video_id, "cache": str(cache), "duration": duration, "cookies": cookies,
                   "edges": edges, "words": [[w.text, w.t] for w in words], "scene_cuts": scene_cuts}
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
            self.timer = threading.Timer(self.idle, self.stop)
            self.timer.daemon = True
            self.timer.start()
        if not line:
            raise RuntimeError("audio worker exited unexpectedly")
        res = json.loads(line)
        if "error" in res:
            raise RuntimeError(res["error"])
        return [tuple(e) for e in res["edges"]]

    def stop(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.proc.stdin.close()  # worker exits on EOF
                try:
                    self.proc.wait(10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None


if __name__ == "__main__" and "--worker" in sys.argv:
    _worker_main()
