# dsh-videokit — long-video understanding for DeepSeek Harness

A plugin for [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (dsh) that
**reads a long video end to end**, instead of sampling a few frames and guessing.

The usual approach (one frame every N seconds + ASR only + chained refine summaries) fails
systematically on long videos for two reasons: the budget is split evenly over time, so the
ending gets diluted; and chained summaries decay whatever comes last. This plugin changes
three things:

1. **Full-length, lossless audio coverage.** The whole track is cut into **5-minute** chunks and
   sent to your own ASR endpoint, keeping each chunk's start/end — that is the skeleton of the
   timeline, not the raw material for a summary. After each chunk it computes
   *characters per second of audio*; when that is out of range it **splits the audio in half and
   re-transcribes** (see "Two traps you have to defend against" below).
2. **Adaptive visual density.** ffmpeg scene detection gives representative frames, `density`
   adds uniform ones, the **last `tailRatio` of the video is sampled at 2×**, and a hard
   `maxFrames` cap keeps the cost bounded.
3. **Structured timeline + map-reduce.** The artifact is `timeline.json` — an event array with
   `t0/t1/kind/source`. Summarization extracts facts per block, and the reduce step
   **only dedupes and sorts; it never re-compresses**. Frame descriptions are truncated to their
   first 80 characters there, otherwise a few hundred art-style descriptions crowd the
   narrator's actual argument out of the block budget.

All three model endpoints (ASR / vision / text) are configured by you: base URL, API key,
model name and auth style. No vendor is hardcoded.

## Install

```bash
git clone https://github.com/Jablem/dsh-videokit.git
dsh plugin --profile <your-profile> add ./dsh-videokit
```

Tools are live as soon as the plugin is installed; **refresh the settings page once** to get the
"Video Understanding" section. The only external dependencies are `ffmpeg` / `ffprobe`
(required) and `yt-dlp` (only for YouTube and similar sites).

## Configure

Settings → **Video Understanding**. Four cards:

| Card | What to fill in | Default placeholder |
|---|---|---|
| Speech recognition | OpenAI-compatible **audio chat** endpoint (`input_audio` block) | `https://api.xiaomimimo.com/v1` / `mimo-v2.5-asr` / auth `api-key` |
| Vision | Vision model endpoint (`image_url` block) | `https://open.bigmodel.cn/api/paas/v4` / `glm-4.1v-thinking-flash` / auth `Bearer` |
| Text summary | Text model endpoint | `https://api.deepseek.com` / `deepseek-chat` / auth `Bearer` |
| Global | python / ffmpeg / ffprobe paths, working directory | auto-detected when empty |

Every card has a "Test connection" button that really fires one minimal request (0.5 s of silence
as a WAV for ASR, a 16×16 grey PNG for vision, `hi` for text) and reports
`✓ 812 ms · mimo-v2.5-asr` or the error verbatim.

Configuration lives in `~/.dsh/dsh-videokit.json`; keys are stored in plain text locally (same
posture as dsh's own credentials file) and are always masked as `sk-****abcd` when read back.
Changes take effect without restarting dsh — every tool call re-reads the file.

Resolution order for binaries: settings → `VIDEOKIT_PYTHON` / `VIDEOKIT_FFMPEG` /
`VIDEOKIT_FFPROBE` / `VIDEOKIT_CONFIG` → `PATH` → common install locations.

## Nine tools

| Tool | Purpose |
|---|---|
| `video_probe` | duration / resolution / fps / audio / chapters → `probe.json`. Local paths and http(s) links |
| `video_download` | Bilibili via playurl + ffmpeg mux; YouTube etc. via `yt-dlp`. Multi-part videos: one part by default, all parts with `allPages=true` |
| `video_index` | The heavy one: chunked transcription + scene/tail-biased frame election + VLM descriptions → `timeline.json` / `chapters.json` |
| `video_index_series` | Index a multi-part series part by part (use this for a 30-hour course, not `video_index`) |
| `video_timeline` | Compact read of the timeline, one `[mm:ss] text` per line; `tail=40` jumps straight to the ending |
| `video_frames` | Dense frame extraction in a time range (no model calls); returns absolute paths so the main model can look at them |
| `video_summarize` | `mapreduce` / `tail-first` |
| `video_report` | Single-video markdown report |
| `video_series_report` | Aggregate a series: overview + course outline + per-part table |

Typical flow:

```
video_probe → video_index → video_timeline tail=40 → video_frames → video_summarize → video_report
```

### Multi-part / 30-hour courses

Do **not** feed 30 hours into a single index: the timeline becomes unreadable, `maxFrames`
spread over 30 hours means one frame every 9 minutes, and any failure restarts everything.
Index per part and aggregate at the series level:

```
video_download      url=... allPages=true out=~/videokit/series
video_index_series  dir=~/videokit/series resume=true maxFrames=60
video_series_report dir=~/videokit/series summarize=true
```

`resume=true` skips parts that already have an `index.json` and lists the failed ones at the end.
Cost ballpark for 30 hours / 61 parts: 2–7 GB download, ~400 ASR calls, 3660 vision calls at 60
frames per part (**vision dominates**; halve `maxFrames` to halve that), roughly 4–8 hours.

## Two traps you have to defend against

Neither is "wrong API usage". The vendor silently returns wrong data and there is no error code.

### 1. ASR silently fails in both directions on long audio

MiMo `mimo-v2.5-asr`, same 16 kHz mono speech audio, all sliced from 0:

| Chunk | Characters | chars/s | |
|---|---|---|---|
| 300 s | 1529 | 5.10 | fine |
| 450 s | 2255 | 5.01 | fine |
| 572 s | 1229 | 2.15 | mildly abridged |
| 600 s | 146 | 0.24 | collapsed |
| 750 s | 1238 | 1.65 | collapsed |
| 900 s | 371 | 0.41 | collapsed, jumps straight to the closing line |

The 900 s tail is byte-identical to the tail of a standalone 600–900 s transcription — it skipped
the middle and read the ending. `finish_reason` is still `stop` and `audio_tokens` shows the audio
was fully uploaded.

It can also inflate: one 300 s maths lecture chunk returned 3195 chars (10.6 chars/s) while two
150 s halves summed to **1764** — that one was repetition/expansion.

**The only detectable signal is characters per second of audio.** Normal Mandarin speech runs
4–6 chars/s; the plugin accepts `[3.0, 8.0]` and bisects the audio (recursively, depth ≤ 2,
floor 150 s) when a chunk falls outside. Effect: a 24.5-minute video went from 4740 to 7588
characters, and a 34.7-minute lecture tripped the guard once in each direction before every chunk
landed at 4.9–5.9 chars/s.

### 2. Bilibili multi-part: `data.cid` is P1's cid

`x/web-interface/view` returns `data.cid`, which is **part 1**. Download with it and a 61-part
video quietly gives you part 1 with no error at all. You have to read `pages`
(this plugin implements `--p N` / `--all-pages` around it).

## Tests

Most of the suite is **network-free**: real endpoints are replaced by a local mock.

| Command | What it checks | Needs |
|---|---|---|
| `python test/selftest.py` | chunk planning, frame election, time parsing, config fallbacks | — |
| `python test/make_fixture.py` | generates a 90 s synthetic video with ffmpeg | ffmpeg |
| `python test/e2e_mock_test.py` | full pipeline: probe→index→timeline→frames→summarize→report | ffmpeg |
| `python test/needle_test.py` | a conclusion at minute 40 must survive both reduce modes | — |
| `node verify/apply_check.mjs` | host assembly: 9 tools, `execute → render` round trip, config masking | — |
| `node dev/render_check.mjs` | settings panel rendering/interaction, offline (fake window + SSR) | a matched react/react-dom pair |
| `python verify/real_llm_check.py` | **real** DeepSeek: the minute-40 conclusion in both modes | network + `DEEPSEEK_API_KEY` |

If ffmpeg is not on `PATH`:

```bash
VIDEOKIT_FFMPEG=/usr/bin/ffmpeg VIDEOKIT_FFPROBE=/usr/bin/ffprobe python test/e2e_mock_test.py
```

`SPEC.md` is the three-way interface contract (host / client / python). It is the most useful
document in the repo — three halves of the plugin were built in parallel against it.

## License

MIT
