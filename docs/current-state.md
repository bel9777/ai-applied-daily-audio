# Current state

Status (2026-10-05 evening): LIVE INFRASTRUCTURE, NO EPISODES YET. Repo + Pages
are up (https://bel9777.github.io/ai-applied-daily-audio/feed.xml, empty feed
pushed by a real unattended `--run`). Task `AI Applied Podcast daily` runs 8:00
daily (battery-safe flags cloned from Foundations). The first episode is
expected Mon 2026-10-12 ~8:00, after the 6:30 course email (Day 1).

- Verified: offline feed build; one real smoke render (gemini-3.8-flash-tts,
  4:29, -16.5 LUFS, LRA 3.5, 0 dips, 0 silence); real --run + push.
- Codex audit fixes (2026-10-05): weekly.py wrote to the wrong host folder
  and would have 404ed. FIXED to `docs/weekly`, proven with a live probe
  file (served at /weekly/). Publish now pulls before touching files, commits
  only on change, size-checks EVERY new file, and raises instead of
  recording a broken week, so the next run retries.
- NOT verified: a real course email through the pipeline; a real weekly
  compile (first week completes Sun 10-18); regen_episode.py.
- Watchdog rows: build heartbeat, live feed (arms 10-13), enclosures, weekly
  collections (arms 10-19), Pages landed, and "AI Applied podcast on its own
  Gemini key" (arms 10-07; WARNs if the heartbeat says key:shared).
- Gemini: own key since 2026-10-05 evening (project gen-lang-client-0225226165,
  $10 cap seen in AI Studio). Status shows `key:applied`; the 14:57 heartbeat
  line predates the key.
- Course hub + decisions: ~/ai-applied-kindle/docs/current-state.md.
