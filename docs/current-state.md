# Current state

Status (2026-10-05): LIVE INFRASTRUCTURE, NO EPISODES YET. Repo + Pages are
up (https://bel9777.github.io/ai-applied-daily-audio/feed.xml, empty feed
pushed by a real unattended `--run`). Task `AI Applied Podcast daily` runs
8:00 daily (battery-safe flags cloned from Foundations). The first episode
is generated after the Mon 2026-10-12 course email (Day 1).

- Verified: offline feed build; one real smoke render (gemini-3.8-flash-tts,
  4:29, -16.5 LUFS, LRA 3.5, 0 dips, 0 silence); real --run + push.
- NOT verified: a real course email through the pipeline; weekly.py
  compile/publish (first week completes Sun 10-18); regen_episode.py.
- Watchdog rows (fleet-watchdog 2bcc0a0): build heartbeat, live feed
  (arms 10-13), enclosures, weekly collections (arms 10-19), Pages landed.
- Gemini key: shared until Brian creates ~/.ai-keys/gemini-applied-api-key.txt
  (heartbeat `key:shared` -> `key:applied`).
- Course hub + decisions: ~/ai-applied-kindle/docs/current-state.md.
