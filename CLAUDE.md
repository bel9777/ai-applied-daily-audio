# CLAUDE.md - ai-applied-daily-audio

Canonical agent rules. Read `docs/current-state.md` next.

## What this is

The AI Applied podcast: GitHub Pages site + RSS feed at
`https://bel9777.github.io/ai-applied-daily-audio/feed.xml`. Episodes are
generated Claude-side by `podcast.py` (two-host dialogue via Gemini text +
multi-speaker TTS, Alex=Puck curious host, Jordan=Sulafat expert). A daily
Task Scheduler job (8:00) runs `py podcast.py --run`. This repo is a fork of
the sibling course's pipeline with its quality gates intact and its legacy /
cutover logic removed.

## Hard rules

1. **Feed titles are load-bearing**: items are `Day N: <title>`
   (fleet-watchdog parses `Day (\d+)`). Weekly bonus items are
   `Full Week N ...` and must NEVER contain "Day N:".
2. **NEVER use GitHub Release assets for audio.** Apps refuse them
   (octet-stream + nosniff + expiring redirect = "can't be played").
   Daily audio is on this repo's Pages; weekly audio is on the separate
   `bel9777/ai-applied-weekly-audio` Pages repo.
3. **Pages 1 GB cap.** This repo grows ~3 MB/day (~10 months of headroom
   from 2026-10-12, so roughly Aug 2027). Prune plan: before ~800 MB,
   delete the oldest `docs/audio` + `docs/transcripts` files and drop their
   ledger entries (or move the oldest months to a second Pages repo like
   the weekly host). Restore from git rather than regenerating if files
   vanish: filenames embed byte size, so restored blobs are byte-identical.
4. **Gemini key file**: `~\.ai-keys\gemini-applied-api-key.txt` if it
   exists, else the shared `~\.ai-keys\gemini-api-key.txt`. The heartbeat
   records `key:applied` or `key:shared`. Never commit, print, or log it.
   429 (quota) pauses the backfill by design; the next run resumes. Fall-
   through on 429 and 5xx across TEXT_MODELS and TTS_MODELS. Re-discover
   models via the models endpoint if any 404.
5. **Episode spec is Brian-approved**: a format copy of the sibling course
   at 600-750 words of dialogue (~4-5 min). `episode_shape()` switches on
   title prefix: `Lesson N:` tool deep dive (hook, what it is / what
   changed, how to use it, gotcha, 2-question check, try-it homework beat -
   never drop it, tomorrow tease), `Applied:` design walkthrough, `Build:`
   build walkthrough, `Release Radar` news show with adopt/try/watch/ignore
   verdicts, `Weekly Review` quiz show. Spoken audio never reads URLs, code
   or HTML aloud. Change voices/format only on Brian's say-so.
6. **Hard dependency on `~\ai-applied-kindle\build.py`**
   (`gmail_token`, `pull_copies`, `select_days`). Importing it also forces
   IPv4 process-wide (this laptop's IPv6 is blackholed). The import is
   FATAL on failure - never make it fail-soft.
7. **Never read `data/episodes.json` directly** to judge progress: it can
   list episodes whose mp3s no longer exist. Resolve through
   `load_state()` (filters by file existence). `ondisk:N/ledger:M` in the
   heartbeat surfaces divergence.
8. **Quality gates stay**: LRA/integrated-loudness/dip gate, silence
   ratio, words-per-second, duration sanity, script QA. They exist because
   bad renders shipped without them. `feed_enclosures_on_disk()` is the
   hard pre-push gate; `git_sync()` runs before anything is generated.
9. **Foundations repos belong to another thread** (`ai-foundations-*`):
   never edit them from here.
10. **Weekly collections** (`weekly.py`): each Mon-Sun week as one
    chaptered mp3 in `docs/weekly.xml` (this repo), audio pushed to the
    weekly host repo clone `~\ai-applied-weekly-audio` (`docs/weekly/`).
    Scratch build dir is outside the repo. Failures show as `weekly-FAILED`
    in the heartbeat and never block the daily episode.

## Ops

- Heartbeat `_RUN-LOG.md`:
  `<time> OK|WARN|FAIL made:N ondisk:N ledger:N missing:N key:applied|shared PUSHED|no-push|PUSH-FAILED[:..] [weekly:+N] [failed:..] [stopped:..]`.
- Unattended push relies on Windows Credential Manager's stored GitHub
  credentials.
- Cover art: `make_cover.py` regenerates `docs/cover.png` (Pillow).
