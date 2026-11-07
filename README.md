# sbjev: SponsorBlock segments drafted by Jev

A **Jev SB** button on YouTube that finds the sponsor reads, self-promotion, subscribe reminders,
intros and outros in the video you're watching, then hands them to the
[SponsorBlock](https://sponsor.ajay.app) extension as **unsubmitted drafts**. You check them in
SponsorBlock's normal editor and press submit yourself. Nothing is ever submitted automatically.

It reads the video's captions with [Jev](https://docs.typesafe.ai) (TypeSafe's fast, cheap
decision model), then snaps each edge to the actual audio with a local speech model so cuts land
in the pause between sentences, where careful SponsorBlock submitters put them.

| | |
|---|---|
| **Cost** | about **$0.0007 per 10 minutes** of video at Jev's $0.042 per million tokens (less if SponsorBlock already covers part of it) |
| **Speed** | about 3–4 s per video for the Jev part, plus a few seconds for audio |
| **Finds** | 83–88% of the well-voted SponsorBlock segments on held-out test videos |
| **Edges** | ~0.1 s from well-voted segments when the right sentence is picked; about 1 in 3 needs a one-sentence nudge (see [Accuracy](#accuracy)) |

## Requirements

- **Windows** for the one-step installer and background server. The Python code itself runs
  anywhere; see [Other systems](#other-systems).
- **Python 3.10+**
- A browser with **[Tampermonkey](https://www.tampermonkey.net/)** or
  **[Violentmonkey](https://violentmonkey.github.io/)**, and the **SponsorBlock** extension.
- **Access to Jev**, one of:
  - [Vercel AI Gateway](https://vercel.com/ai-gateway) key (paid credit required), or
  - [TypeSafe](https://console.typesafe.ai/keys) key, or
  - Cloudflare Workers AI account ID + API token.
- Optional, for audio edge snapping: **ffmpeg**, and an NVIDIA GPU (it also runs on CPU, slower).
- Recommended: **[Deno](https://deno.com)**, which yt-dlp uses for YouTube's JavaScript
  challenges. Without it you hit "confirm you're not a bot" far more often.

## Install (Windows)

```powershell
git clone https://github.com/Landed9771/sbjev sbjev
cd sbjev
powershell -ExecutionPolicy Bypass -File install.ps1
```

The installer:
1. installs the Python packages (`requests`, `numpy`, `yt-dlp`, and `faster-whisper` unless you
   pass `-NoAudio`);
2. offers to install ffmpeg and Deno with winget if they're missing;
3. creates `config.json` and asks for your Jev key;
4. sets the server to start hidden at login and to restart itself if it ever crashes;
5. registers a `sbjev://` link so the userscript's **Start server** button can launch it;
6. starts the server.

Then **install the userscript**: open
[`sbjev.user.js`](https://raw.githubusercontent.com/Landed9771/sbjev/main/sbjev.user.js) and click
Install in Tampermonkey or Violentmonkey. Allow it to connect to `127.0.0.1` if asked.

To remove it: `powershell -ExecutionPolicy Bypass -File uninstall.ps1`, then delete the folder and
the userscript.

## Use

1. Open a YouTube video and click **Jev SB** (next to the search box).
2. The panel lists what it found. Segments already on SponsorBlock are greyed out; uncertain
   edges are marked.
3. **Load into SponsorBlock** reloads the page at the same timestamp with the drafts in
   SponsorBlock's submit menu. Preview them, adjust if needed, and submit.

The panel also has:
- **Category checkboxes:** sponsor, selfpromo, interaction, intro and outro are on by default.
  preview, filler and highlight are available but less reliable.
- **Skip categories already on SponsorBlock:** much cheaper on popular videos, but a second
  sponsor read nobody submitted won't be found.
- **Cost line:** this video's cost, plus total spent and balance (Vercel only).

## Configuration

Everything lives in `config.json` (start from `config.example.json`). The server re-reads it on
restart. End the `python` process and the background launcher restarts it within seconds.

| setting | default | what it does |
|---|---|---|
| `keys.*` | empty | your Jev credentials; environment variables with the same names in upper case override them |
| `port` | `8765` | local server port (if you change it, also change the userscript's server address from the Tampermonkey menu) |
| `model` | `jev-1.14.0` | Jev version; pinned so cached answers never mix versions |
| `categories` | sponsor, selfpromo, interaction, intro, outro | default categories when the panel doesn't send any |
| `audio_snapping` | `true` | snap edges to the audio with faster-whisper + Silero VAD |
| `audio_idle_minutes` | `30` | the speech model is unloaded (all GPU memory freed) after this long unused |
| `scene_cuts` | `true` | put an edge on the editor's visual cut when there's one inside the pause (downloads the video at lowest quality, ~8 MB for 25 min) |
| `skip_known_categories` | `false` | server-side default for "skip categories already on SponsorBlock" |
| `cookies_from_browser` | `null` | e.g. `"firefox"`: let yt-dlp use a browser's YouTube login if the bot check blocks you |
| `parallel_requests` | `6` | concurrent Jev requests |

## Command line

The server isn't required: `sbjev.py` also works directly.

```
python sbjev.py https://www.youtube.com/watch?v=VIDEO_ID
python sbjev.py <playlist-or-channel-url> --limit 10 --open
python sbjev.py --serve                    # what the background launcher runs
python sbjev.py --help
```

It prints each segment and an import link
(`https://www.youtube.com/watch?v=ID#segments=[...]`). Opening that link with SponsorBlock
installed loads the drafts.

## How it works

1. **Captions:** yt-dlp fetches one caption track, preferring YouTube's automatic ones for their
   per-word timings.
2. **Scan:** the transcript is read in ~2-minute chunks, with one yes/no question per category
   per chunk. Cheap, and on the eval set it caught 100% of sponsor reads.
3. **Classify:** only flagged chunks get a per-line category question. Lines inside segments
   SponsorBlock already has are skipped.
4. **Edges:** for sponsor and self-promo reads, Jev finds the sentence that names the product,
   traces back to where the lead-in starts, and picks the sentence that ends the read. A small
   extra question tells apart the creator's own products (self-promotion) from other companies'
   (sponsor; this includes collaborations and Nebula, as the crowd files them). When one read
   comes out as self-promotion that turns into a sponsor partway ("a new episode of my show...
   only on Nebula"), the two halves are joined into one sponsor segment. A creator's own plug
   *after* a sponsor (Patreon thanks, merch) stays separate.
5. **Outro / intro:** a sign-off question over the last ~2 minutes, plus music-only gaps from
   the captions.
6. **Audio:** each edge is cut in the pause it belongs to, using faster-whisper (to find the
   word) and Silero VAD (to find the pause). Starts go ~0.3 s before the first word, ends just
   before speech resumes. Those offsets were measured from locked SponsorBlock segments. If
   the editor made a visual cut inside that pause, the edge goes on that frame instead
   (`scene_cuts`; it needs the whole video at the lowest quality, a few MB, downloaded in the
   background while Jev works).
7. **Import link:** new segments are put in the `#segments=` link SponsorBlock reads; segments
   already on SponsorBlock are left out.

Jev answers are cached in `cache/`, so re-running a video is free.

## Accuracy

Measured against locked or well-voted (3+) SponsorBlock segments: 212 videos from 28 channels
(`eval/`). Held-out videos were not used for tuning; the largest test set is 97 videos from 14
channels that nothing was tuned on.

- **Finding segments:** 207 of 253 well-voted segments across the three held-out sets (82%;
  79% on the 97-video set). About a third of "extra" segments are real ones with few votes
  (end-of-video outros, "let me know in the comments"), another quarter are filed by the crowd
  under a neighbouring category.
- **Picking the right sentence (97-video set, before audio snapping):** 66% of starts and 62% of
  ends within 1 s of the crowd, 73% / 75% within 3 s.
- **Edges, when the right sentence is picked:** the audio step puts cuts a median **~0.1 s** from
  well-voted segments (0.13 s on starts, 0.11 s on ends, 170 crowd edges, held-out half). That is
  about as tight as the crowd agrees with itself: equally reasonable cut rules differ by 0.1–0.2 s.
- **What still goes wrong:** about a quarter of reads start or end a sentence or more off, which
  means seconds, not fractions. Mostly long themed segues, where the video's topic slides into
  the sponsor's ("...protesters needed VPNs..." before a VPN ad), and passing mentions of the
  sponsor before the actual read. Check those in SponsorBlock's preview before submitting.
  Handled by fixed rules: "after the break" cliffhangers count as the start; "and now back to
  the video" counts as the end; "thanks for watching" sign-offs and "next time on..." teasers
  are left out of the ad.
- **Highlight** is conservative: it only answers when clearly sure. That was 2 of 8 MrBeast
  videos, both within 5 s, and no wrong answers.
- **Filler** doesn't work well from transcripts; the crowd's filler is mostly visual.

## Privacy

- The **video's captions and title** go to your Jev provider (TypeSafe, Vercel or Cloudflare).
- The **video ID** goes to the public SponsorBlock API to check existing segments.
- **Audio is processed locally** and never uploaded.
- The server only listens on `127.0.0.1`. Your keys stay in `config.json` (which git ignores) or
  your environment.

## Troubleshooting

| message | fix |
|---|---|
| "YouTube's bot check..." | turn off your VPN if one is on; install Deno; or set `cookies_from_browser` to a browser where you're signed in to YouTube |
| "The sbjev server isn't running" | press **Start server** in the panel; if your browser asks about opening sbjev links, allow it. Or run `install.ps1` again |
| "Out of usable Jev credit" | Vercel needs *paid* credit for Jev (free starter credit doesn't count) |
| "no captions" | the video has no captions, so there's nothing to read |
| anything else | `server.log` in the sbjev folder |

## Other systems

The launcher, autostart and `sbjev://` link are Windows-only. On macOS or Linux, install the
packages (`pip install -r requirements.txt faster-whisper`, plus ffmpeg and Deno), copy
`config.example.json` to `config.json`, and run `python sbjev.py --serve` yourself, or from a
systemd user service or launchd agent.

## Evaluation scripts

`eval/` has the scripts used for the numbers above: building the corpus from SponsorBlock,
measuring where crowd edges sit relative to speech, end-to-end scoring (`e2e_eval.py`) and more.
Set `SBJEV_CACHE_ONLY=1` to replay from cache without spending anything.

## Credits

- [SponsorBlock](https://github.com/ajayyy/SponsorBlock), whose crowd data is the ground truth
  and whose `#segments=` import makes this work.
- [Jev / TypeSafe](https://docs.typesafe.ai).
- [trungdq88/youtube-sponsor-detection](https://github.com/trungdq88/youtube-sponsor-detection),
  for the lead-in → pitch → offer structure.
- [yt-dlp](https://github.com/yt-dlp/yt-dlp), [faster-whisper](https://github.com/SYSTRAN/faster-whisper),
  [Silero VAD](https://github.com/snakers4/silero-vad).

## License

MIT
