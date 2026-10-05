"""AI Applied podcast - two-host episode pipeline (Claude-side).

For every course day (adjudicated by the ai-applied-kindle repo - HARD
dependency, fails loudly): generate a two-host dialogue script (Gemini text
model), render it with Gemini multi-speaker TTS (Alex=Puck, Jordan=Sulafat),
encode mp3, and publish to GitHub Pages (docs/ on main).

Forked from the sibling course pipeline with all of its hard-won quality
gates intact. Idempotent: done days are skipped (an episode counts as done
only if its mp3 exists on disk); a TTS quota 429 pauses the backfill and the
next run continues. The main feed is rebuilt from ALL episodes on disk on
every run.

Usage:
    py podcast.py            # status only (safe)
    py podcast.py --run      # generate missing, publish, push
    py podcast.py --run --limit 5
"""

import base64
import io
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from html import escape
from pathlib import Path

HOME = Path.home()
REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(HOME / "ai-applied-kindle"))
try:
    # importing build.py also forces IPv4 process-wide (IPv6 is blackholed
    # on this laptop) - essential, so a failure here is FATAL, never soft
    from build import gmail_token, pull_copies, select_days  # noqa: E402
except ImportError as e:  # hard dependency - never fail soft (f4 lesson)
    sys.exit(f"FATAL: cannot import ai-applied-kindle build.py: {e}")


def _load_key():
    """Dedicated key if present, else the shared one. Never printed."""
    for name, label in (("gemini-applied-api-key.txt", "applied"),
                        ("gemini-api-key.txt", "shared")):
        f = HOME / ".ai-keys" / name
        if f.exists():
            return f.read_text().strip(), label
    sys.exit("FATAL: no Gemini key file in ~/.ai-keys")


KEY, KEY_LABEL = _load_key()
GBASE = "https://generativelanguage.googleapis.com/v1beta"
# fall through on 429/5xx so one model's 503 streak cannot block every episode
TEXT_MODELS = ["gemini-3.8-flash", "gemini-flash-latest", "gemini-3.7-flash",
               "gemini-3.5-flash", "gemini-3.1-flash-lite"]
# Quota is PER MODEL, so the fallback chain roughly doubles free-tier
# throughput. Same voices, same script; the plausibility gate polices quality.
# gemini-3.8-flash-tts (GA) is PRIMARY: the two previews 503'd for weeks and
# 3.1's long renders wobble in loudness. 3.8 wants each line as its own part
# tagged speechMetadata.speaker.
TTS_MODELS = ["gemini-3.8-flash-tts", "gemini-2.5-flash-preview-tts",
              "gemini-3.1-flash-tts-preview"]
PER_PART_SPEAKER_MODELS = {"gemini-3.8-flash-tts"}
# fall through to the next model on quota OR server overload
FALLTHROUGH_CODES = (429, 500, 502, 503, 504)
# every render is normalized to podcast loudness at encode time
AUDIO_FILTERS = "dynaudnorm=f=300:g=31:p=0.95,loudnorm=I=-16:TP=-1.5:LRA=7"
MAX_LRA, MIN_I, MAX_I = 8.5, -19.5, -13.5
DIP_LU, MAX_DIP_SECONDS = 6, 3.0
CHUNK_PARTS = 4  # last-resort chunked render when a whole script 429s
FFMPEG = (HOME / r"AppData\Local\Microsoft\WinGet\Packages"
               r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
               r"\ffmpeg-8.1.2-full_build\bin\ffmpeg.exe")

# split timeouts: the text call is quick; the TTS call must synthesize
# minutes of 24 kHz audio and return it base64-inlined in one response
TEXT_TIMEOUT, TTS_TIMEOUT = 120, 600
RUN_CAP_SECONDS = 3 * 3600  # a bad run must not grind all day
# a good episode is ~2.4-3.4 spoken words/sec with only natural pauses; a
# half-silent render passes a duration-only gate (silence is bytes too)
MAX_SILENCE_RATIO, MIN_WORDS_PER_SEC = 0.10, 2.0
# episode spec: 600-750 words (~4-5 min). QA floor leaves slack under 600;
# the duration sanity range is wide enough for slow/fast delivery.
MIN_SCRIPT_WORDS = 420
MIN_SECONDS, MAX_SECONDS = 120, 600

HOST, EXPERT = "Alex", "Jordan"
VOICE_HOST, VOICE_EXPERT = "Puck", "Sulafat"
SITE = "https://bel9777.github.io/ai-applied-daily-audio"
# Audio is served from GitHub Pages. Pages has a 1 GB site limit; this repo
# grows ~3 MB/day (~10 months of headroom) - see CLAUDE.md for the prune plan.
AUDIO_BASE = SITE
STATE = REPO / "data" / "episodes.json"
AUDIO_DIR = REPO / "docs" / "audio"
TRANS_DIR = REPO / "docs" / "transcripts"
RUN_LOG = REPO / "_RUN-LOG.md"


def gapi(path, payload, timeout=300, attempts=3):
    """POST to Gemini, retrying transient 5xx.

    A single HTTP 503 once killed a whole run and left episodes unmade.
    Server errors are transient and must not end the backfill; 429 (quota)
    must still propagate so the caller can stop.
    """
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            f"{GBASE}/{path}", data=json.dumps(payload).encode(),
            headers={"x-goog-api-key": KEY, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (500, 502, 503, 504) and attempt < attempts:
                wait = 20 * attempt
                print(f"    HTTP {e.code} - retry {attempt}/{attempts - 1} "
                      f"in {wait}s")
                time.sleep(wait)
                continue
            raise


def episode_shape(day, title, lesson):
    """Episode structure by title prefix: Mon-Wed tool lesson, Thu Applied
    design, Fri Build, Sat Release Radar, Sun Weekly Review."""
    m = re.match(r"Lesson (\d+):", title)
    intro = (f'then "This is AI Applied, lesson {m.group(1)}."' if m
             else 'then "This is AI Applied."')
    if title.startswith("Release Radar"):
        return f"""Rules:
- Open with {HOST} teasing the single biggest release of the week, {intro}
- Run it as a news show. For every item in the briefing: what shipped, then
  the verdict - adopt, try, watch, or ignore - and why, for Brian's stack.
  {EXPERT} names the source for each claim ("according to the release
  notes", "the announcement says") - never invent details beyond the
  briefing.
- End with "what to watch" for next week in one beat."""
    if title.startswith("Weekly Review"):
        return f"""Rules:
- Open with {HOST} saying this is review day, {intro}
- Run it as a quiz show of about six questions drawn from the review:
  {EXPERT} asks, {HOST} answers out loud first, then after a beat {EXPERT}
  gives the answer and one sentence on why it matters. Keep the energy up.
- Close with the review's single most important takeaway."""
    if title.startswith("Applied:"):
        return f"""Rules:
- Open with {HOST} naming the real problem in Brian's work that today
  designs a solution for, {intro}
- Walk through the design step by step: what the pieces are, how data and
  decisions flow between them, and why each choice was made.
- Then cover what could go wrong and how the design guards against it.
- Close with the one first step to take this week."""
    if title.startswith("Build:"):
        return f"""Rules:
- Open with {HOST} saying what you'll have working by the end of today's
  build, {intro}
- Narrate the build steps at a high level, in order: what to do and what
  you should see. Never read code, commands, or file contents aloud.
- Call out the checkpoints that tell you it's working so far.
- Cover the likely failures and how to recognize them.
- Close with the stretch goal for anyone who finishes early."""
    return f"""Rules:
- {HOST} opens with a one-sentence hook about a concrete problem Brian
  has that today's tool addresses, {intro} Then dive in.
- Explain what the tool is and what changed recently.
- Then {EXPERT} walks through how Brian should use it: the one specific
  workflow from the lesson, in plain spoken steps.
- Then the gotcha: when NOT to use it, or the thing that bites.
- Then {EXPERT} gives a quick check - two questions from the lesson;
  {HOST} answers in their own words, {EXPERT} confirms or sharpens.
- Then {EXPERT} assigns the lesson's try-it exercise as homework in one
  tight beat: exactly what to do and what to notice while doing it. Do
  not skip this beat.
- Close with a one-line tease of tomorrow's topic if the lesson names one."""


def dialogue_prompt(day, title, lesson):
    return f"""You write scripts for a two-host educational podcast called
"AI Applied Daily". Turn today's lesson into a natural conversation.

Hosts:
- {HOST}: the curious co-host. Sharp, asks the questions a smart listener
  would ask, occasionally pushes back or summarizes in plain words.
- {EXPERT}: the expert. Explains clearly with everyday analogies, keeps it
  grounded, never lectures for long without {HOST} jumping in.

{episode_shape(day, title, lesson)}
- Sound like two real people: contractions, short sentences, occasional
  quick banter. No corporate speak, no "delve", no "great question", no
  filler praise between hosts.
- Plain spoken text only: no headings, no bullet lists, no stage
  directions, nothing in brackets or asterisks. Never read URLs, code,
  commands, or HTML aloud - describe them in words instead.
- Target 600-750 words total (about four to five minutes of audio).
- FORMAT: every line starts with "{HOST}:" or "{EXPERT}:" followed by the
  words they say. Nothing else.

Today's lesson (Day {day}: {title}):
{lesson}"""


def gen_script(day, title, lesson):
    for i, model in enumerate(TEXT_MODELS):
        try:
            resp = gapi(f"models/{model}:generateContent", {
                "contents": [{"parts": [{"text":
                    dialogue_prompt(day, title, lesson)}]}],
                "generationConfig": {"temperature": 0.8}},
                timeout=TEXT_TIMEOUT)
            break
        except urllib.error.HTTPError as e:
            if (e.code not in (404, 429, 500, 502, 503, 504)
                    or i == len(TEXT_MODELS) - 1):
                raise
            print(f"    {model}: HTTP {e.code}, trying next text model")
    script = resp["candidates"][0]["content"]["parts"][0]["text"].strip()
    script = re.sub(r"^```.*$", "", script, flags=re.M).strip()
    lines = [ln for ln in script.splitlines() if ln.strip()]
    good = [ln for ln in lines if re.match(rf"^({HOST}|{EXPERT}):", ln.strip())]
    words = sum(len(ln.split()) for ln in good)
    if len(good) < len(lines) - 2 or words < MIN_SCRIPT_WORDS:
        raise ValueError(f"script failed QA: {len(good)}/{len(lines)} dialogue "
                         f"lines, {words} words")
    return "\n".join(good)


def gen_audio(script):
    """Render the dialogue, falling through TTS_MODELS on quota errors.

    Returns (pcm, rate, model). Raises the LAST 429 only if every model
    is exhausted, so the caller's quota-stop logic still works.
    """
    last_429 = None
    for model in TTS_MODELS:
        try:
            return (*_gen_audio_with(script, model), model)
        except urllib.error.HTTPError as e:
            if e.code not in FALLTHROUGH_CODES:
                raise
            last_429 = e
            print(f"    {model}: HTTP {e.code}, trying next model")

    # LAST RESORT: render in chunks. The remaining free-tier allowance is
    # TOKEN-based, so several small requests can succeed where one large
    # one 429s. Raw PCM concatenates cleanly - same rate, mono, 16-bit - and
    # each chunk carries the same voice config, so speakers stay consistent.
    for model in TTS_MODELS:
        try:
            parts, rate = [], None
            chunks = _split_script(script, CHUNK_PARTS)
            for i, chunk in enumerate(chunks, 1):
                pcm, rate = _gen_audio_with(chunk, model)
                parts.append(pcm)
                print(f"    {model}: chunk {i}/{len(chunks)} rendered")
                time.sleep(8)
            return b"".join(parts), rate, f"{model}+chunked"
        except urllib.error.HTTPError as e:
            if e.code not in FALLTHROUGH_CODES:
                raise
            last_429 = e
            print(f"    {model}: chunked render also blocked (HTTP {e.code})")
    raise last_429


def _split_script(script, parts):
    """Split on speaker-line boundaries so no utterance is cut in half."""
    lines = [ln for ln in script.splitlines() if ln.strip()]
    size = -(-len(lines) // parts)  # ceil
    return ["\n".join(lines[i:i + size]) for i in range(0, len(lines), size)]


def _tts_parts(script, model):
    if model not in PER_PART_SPEAKER_MODELS:
        return [{"text":
            f"TTS the following podcast conversation between {HOST} and "
            f"{EXPERT}. {HOST} sounds curious and engaged; {EXPERT} sounds "
            "warm and clear. Natural conversational pacing.\n\n" + script}]
    parts = []
    for ln in script.splitlines():
        m = re.match(rf"^({HOST}|{EXPERT}):\s*(.+)", ln.strip())
        if m:
            parts.append({"text": m.group(2),
                          "speechMetadata": {"speaker": m.group(1)}})
    return parts


def _gen_audio_with(script, TTS_MODEL):
    resp = gapi(f"models/{TTS_MODEL}:generateContent", {
        "contents": [{"role": "user", "parts": _tts_parts(script, TTS_MODEL)}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"multiSpeakerVoiceConfig": {"speakerVoiceConfigs": [
                {"speaker": HOST, "voiceConfig":
                    {"prebuiltVoiceConfig": {"voiceName": VOICE_HOST}}},
                {"speaker": EXPERT, "voiceConfig":
                    {"prebuiltVoiceConfig": {"voiceName": VOICE_EXPERT}}}]}}}},
        timeout=TTS_TIMEOUT)
    pcm, rate = b"", 24000
    for part in resp["candidates"][0]["content"]["parts"]:
        mime = part["inlineData"]["mimeType"]
        data = base64.b64decode(part["inlineData"]["data"])
        if "wav" in mime:  # 3.8 returns a WAV container, previews raw PCM
            with wave.open(io.BytesIO(data)) as w:
                rate, data = w.getframerate(), w.readframes(w.getnframes())
        elif "rate=" in mime:
            rate = int(re.search(r"rate=(\d+)", mime).group(1))
        pcm += data
    return pcm, rate


def encode_mp3(pcm, rate, out_path):
    tmp = out_path.with_suffix(".tmp.wav")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    subprocess.run([str(FFMPEG), "-y", "-loglevel", "error", "-i", str(tmp),
                    "-af", AUDIO_FILTERS,
                    "-codec:a", "libmp3lame", "-b:a", "96k", str(out_path)],
                   check=True)
    tmp.unlink()
    return len(pcm) / (rate * 2)


def loudness(path):
    """EBU R128: (integrated LUFS, loudness range LU, dip seconds).

    dip seconds = time the 3s short-term loudness sits more than DIP_LU
    below the episode's speech median. LRA alone passed episodes that still
    "faded in and out"; every clean render measures 0-3s of dips.
    """
    r = subprocess.run([str(FFMPEG), "-hide_banner", "-v", "verbose",
                        "-i", str(path), "-af", "ebur128=framelog=verbose",
                        "-f", "null", "-"], capture_output=True, text=True,
                       errors="replace")
    tail = r.stderr[r.stderr.rfind("Summary:"):]
    shortterm = [float(v) for v in
                 re.findall(r"\sS:\s*(-?[\d.]+)", r.stderr)][30:]
    speech = sorted(v for v in shortterm if v > -40)
    median = speech[len(speech) // 2] if speech else 0
    dips = sum(1 for v in speech if v < median - DIP_LU) / 10  # 100ms frames
    return (float(re.search(r"I:\s*(-?[\d.]+) LUFS", tail).group(1)),
            float(re.search(r"LRA:\s*([\d.]+) LU", tail).group(1)), dips)


def silence_ratio(path):
    """Fraction of the rendered file that is TRUE digital silence."""
    r = subprocess.run(
        [str(FFMPEG), "-i", str(path), "-af",
         "silencedetect=noise=-50dB:d=2", "-f", "null", "-"],
        capture_output=True, text=True)
    quiet = sum(float(m) for m in
                re.findall(r"silence_duration: ([\d.]+)", r.stderr))
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    if not m:
        return 0.0
    secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return quiet / secs if secs else 0.0


def slugify(title):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", title.lower())).strip("-")


def load_state():
    """Ledger entries whose mp3 still exists on disk (never trust the
    ledger alone)."""
    if STATE.exists():
        eps = json.loads(STATE.read_text(encoding="utf-8"))
        return {e["day"]: e for e in eps
                if (REPO / "docs" / e["audioPath"].lstrip("/")).exists()}
    return {}


def save_state(eps):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(
        sorted(eps.values(), key=lambda e: -e["day"]), indent=1,
        ensure_ascii=False), encoding="utf-8")


def build_episode(day, info, eps):
    lesson = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", info["html"])).strip()
    # name the failing stage so the heartbeat says which one (HTTPError must
    # pass through untouched - the 429 quota-stop upstream depends on it)
    try:
        script = gen_script(day, info["title"], lesson)
    except urllib.error.HTTPError:
        raise
    except Exception as e:
        raise RuntimeError(f"script-{type(e).__name__}") from e
    try:
        pcm, rate, tts_model = gen_audio(script)
    except urllib.error.HTTPError:
        raise
    except Exception as e:
        raise RuntimeError(f"tts-{type(e).__name__}") from e
    slug = slugify(info["title"])
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    TRANS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_mp3 = AUDIO_DIR / f"day-{day:03d}-{slug}.new.mp3"
    secs = encode_mp3(pcm, rate, tmp_mp3)
    # plausibility, not just reconciliation
    if not MIN_SECONDS <= secs <= MAX_SECONDS:
        tmp_mp3.unlink()
        raise ValueError(f"audio duration {secs:.0f}s outside sanity range")
    sil = silence_ratio(tmp_mp3)
    wps = len(script.split()) / secs if secs else 0
    if sil > MAX_SILENCE_RATIO or wps < MIN_WORDS_PER_SEC:
        tmp_mp3.unlink()
        raise ValueError(f"audio failed plausibility: {sil:.0%} silence, "
                         f"{wps:.2f} words/sec ({len(script.split())} words "
                         f"in {secs:.0f}s) - TTS dropped content")
    # loudness gate: a render whose volume wanders must not reach headphones
    li, lra, dips = loudness(tmp_mp3)
    if lra > MAX_LRA or not MIN_I <= li <= MAX_I or dips > MAX_DIP_SECONDS:
        tmp_mp3.unlink()
        raise ValueError(f"audio failed loudness gate: I={li:+.1f} LUFS, "
                         f"LRA={lra:.1f} LU, dips={dips:.1f}s - unstable render")
    size = tmp_mp3.stat().st_size
    final = AUDIO_DIR / f"day-{day:03d}-{slug}-{size}.mp3"
    tmp_mp3.rename(final)
    (TRANS_DIR / f"day-{day:03d}-{size}.txt").write_text(script, encoding="utf-8")
    date = info["date"] or datetime.now()
    eps[day] = {
        "day": day, "title": info["title"], "format": "two-host",
        "guid": f"applied-day-{day:03d}",
        "publishedAt": date.strftime("%Y-%m-%dT%H:%M:%S"),
        "audioPath": f"/audio/{final.name}",
        "transcriptPath": f"/transcripts/day-{day:03d}-{size}.txt",
        "audioBytes": size, "durationSeconds": int(secs),
        "ttsModel": tts_model,
        "metrics": {"words": len(script.split()), "lra": lra, "lufs": li,
                    "dipSeconds": dips, "silenceRatio": round(sil, 4)},
    }
    return secs


def rfc822(iso):
    return format_datetime(
        datetime.fromisoformat(iso).replace(tzinfo=timezone.utc))


def _weekly_items():
    """Week-long collections, also shown IN the daily feed. Titled
    "Full Week N ... Days A-B" - never "Day N:", which the watchdog's
    newest-episode check parses. Dated one minute after the week's last
    episode so each lands just above it."""
    f = REPO / "data" / "weekly.json"
    out = []
    for w in json.loads(f.read_text(encoding="utf-8")) if f.exists() else []:
        when = (datetime.fromisoformat(w["publishedAt"])
                + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
        hrs, rem = divmod(w["durationSeconds"], 3600)
        chap = "".join(
            f"{c['start'] // 60}:{c['start'] % 60:02d} {escape(c['title'])}&lt;br/&gt;"
            for c in w["chapters"])
        out.append((when, f"""    <item>
      <title>Full {escape(w['title'])}</title>
      <description>The whole week as one listen, a chapter per day:&lt;br/&gt;{chap}</description>
      <guid isPermaLink="false">weekly-{w['weekStart']}-{w['key']}</guid>
      <pubDate>{rfc822(when)}</pubDate>
      <itunes:episodeType>bonus</itunes:episodeType>
      <enclosure url="{w['url']}" length="{w['bytes']}" type="audio/mpeg"/>
      <itunes:duration>{hrs}:{rem // 60:02d}:{rem % 60:02d}</itunes:duration>
    </item>"""))
    return out


def build_feed(eps):
    items = []
    for e in sorted(eps.values(), key=lambda x: -x["day"]):
        mins, secs = divmod(e["durationSeconds"], 60)
        items.append((e["publishedAt"], f"""    <item>
      <title>Day {e['day']}: {escape(e['title'])}</title>
      <description>{escape(e['title'])} - AI Applied day {e['day']}, as a conversation between Alex and Jordan.</description>
      <guid isPermaLink="false">{escape(e['guid'])}</guid>
      <pubDate>{rfc822(e['publishedAt'])}</pubDate>
      <itunes:episode>{e['day']}</itunes:episode>
      <enclosure url="{AUDIO_BASE}{e['audioPath']}" length="{e['audioBytes']}" type="audio/mpeg"/>
      <itunes:duration>{mins}:{secs:02d}</itunes:duration>
    </item>"""))
    items = [xml for _, xml in sorted(items + _weekly_items(),
                                      key=lambda t: t[0], reverse=True)]
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>AI Applied Daily</title>
    <link>{SITE}/</link>
    <language>en-us</language>
    <description>The AI Applied course as a daily two-host conversation: the tools, models and agents worth using now, and what to build with them. Companion to the daily email and Kindle edition.</description>
    <itunes:author>AI Applied</itunes:author>
    <itunes:type>episodic</itunes:type>
    <itunes:image href="{SITE}/cover.png"/>
    <atom:link href="{SITE}/feed.xml" rel="self" type="application/rss+xml"/>
{chr(10).join(items)}
  </channel>
</rss>
"""
    (REPO / "docs").mkdir(exist_ok=True)
    (REPO / "docs" / "feed.xml").write_text(feed, encoding="utf-8")


def build_index(eps):
    rows = []
    for e in sorted(eps.values(), key=lambda x: -x["day"]):
        mins, secs = divmod(e["durationSeconds"], 60)
        rows.append(
            f'<li><strong>Day {e["day"]}: {escape(e["title"])}</strong> '
            f'({mins}:{secs:02d}) <audio controls preload="none" '
            f'src="{e["audioPath"].lstrip("/")}"></audio> '
            f'<a href="{e["transcriptPath"].lstrip("/")}">transcript</a></li>')
    html = ("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"/>"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>"
            "<title>AI Applied Daily</title>"
            "<style>body{font-family:Georgia,serif;max-width:44rem;margin:2rem auto;"
            "padding:0 1rem;line-height:1.5}li{margin:.8em 0}audio{width:100%;"
            "max-width:24rem;display:block;margin:.3em 0}</style></head><body>"
            "<h1>AI Applied Daily</h1>"
            "<p>The AI Applied course as a daily two-host conversation. "
            f"Subscribe by URL: <code>{SITE}/feed.xml</code></p>"
            "<p>Each week as one continuous listen (chapter per day): "
            f"<code>{SITE}/weekly.xml</code></p><ul>"
            + "".join(rows) + "</ul></body></html>")
    (REPO / "docs" / "index.html").write_text(html, encoding="utf-8")


def git_sync():
    """Pull BEFORE generating anything, so load_state() sees what is really
    on disk (a vanished mp3 must be restored from git, not advertised)."""
    # --autostash: the previous run's heartbeat line is still uncommitted at
    # this point (written after publish(), committed by the NEXT run), and a
    # plain pull refuses on a dirty tree.
    r = subprocess.run(["git", "-C", str(REPO), "pull", "--rebase",
                        "--autostash"], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"WARNING: pre-run git pull failed: {r.stderr.strip()[:200]}")
    return r.returncode == 0


def feed_enclosures_on_disk():
    """Every enclosure OUR feed advertises must exist on disk. Only
    validates enclosures served from SITE (weekly items live on their own
    Pages site and are not gated here)."""
    missing = []
    f = REPO / "docs" / "feed.xml"
    if not f.exists():
        return missing
    try:
        root = ET.fromstring(f.read_text(encoding="utf-8"))
    except ET.ParseError as e:
        return [f"feed.xml: UNPARSEABLE ({e})"]
    for enc in root.findall(".//enclosure"):
        url = enc.get("url") or ""
        if not url.startswith(SITE):
            continue
        rel = url[len(SITE):].lstrip("/")
        if not (REPO / "docs" / rel).exists():
            missing.append(f"feed.xml->{rel}")
    return missing


def publish(msg):
    # commit FIRST, then rebase, then push - pulling before staging always
    # refuses (this run's outputs are unstaged at that point)
    subprocess.run(["git", "-C", str(REPO), "add", "-A"], check=True)
    r = subprocess.run(["git", "-C", str(REPO), "diff", "--cached", "--quiet"])
    committed = r.returncode != 0
    if committed:
        subprocess.run(["git", "-C", str(REPO), "commit", "-m", msg, "--quiet"],
                       check=True)
    ahead = subprocess.run(
        ["git", "-C", str(REPO), "rev-list", "--count", "@{u}..HEAD"],
        capture_output=True, text=True)
    if not committed and ahead.stdout.strip() == "0":
        return False
    subprocess.run(["git", "-C", str(REPO), "pull", "--rebase", "--quiet"],
                   check=True)
    # LAST GATE: never push a feed that advertises files we do not have.
    missing = feed_enclosures_on_disk()
    if missing:
        raise RuntimeError(
            f"refusing to push: {len(missing)} advertised enclosure(s) "
            f"missing after rebase, e.g. {missing[:3]}")
    subprocess.run(["git", "-C", str(REPO), "push", "--quiet"], check=True)
    return True


def main():
    run = "--run" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    started = datetime.now()

    if run:
        git_sync()  # sync BEFORE deciding anything
    tok = gmail_token()
    days = select_days(tok, pull_copies(tok))
    eps = load_state()
    missing = sorted(d for d in days if d not in eps)
    print(f"course days: {'1-' + str(max(days)) if days else 'none yet'} | "
          f"episodes done: {len(eps)} | missing: {missing or 'none'} | "
          f"key:{KEY_LABEL}")
    if not run:
        return

    # newest missing day first (today's episode ships same-morning even
    # mid-backfill), then oldest-first backfill
    queue = [missing[-1]] + missing[:-1] if missing else []
    made, fails, stopped = [], [], ""
    for day in queue[:limit] if limit else queue:
        if (datetime.now() - started).total_seconds() > RUN_CAP_SECONDS:
            stopped = f"run-cap {RUN_CAP_SECONDS}s reached at day {day}"
            print(f"  {stopped}")
            break
        try:
            secs = build_episode(day, days[day], eps)
            save_state(eps)
            made.append(day)
            print(f"  day {day}: OK ({secs:.0f}s)")
            time.sleep(15)  # stay under free-tier RPM
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429:
                stopped = f"quota-429 at day {day}, resumes next run"
                print(f"  day {day}: {stopped}")
                break
            # only auth/permission errors are worth abandoning the run for;
            # anything else is this day's problem, not the fleet's
            print(f"  day {day}: HTTP {e.code} {body}")
            if e.code in (401, 403):
                stopped = f"HTTP-{e.code} at day {day}"
                break
            fails.append(f"{day}:HTTP{e.code}")
        except Exception as e:
            # keep the stage label (script-/tts-) rather than the bare class
            fails.append(f"{day}:{e.args[0] if isinstance(e, RuntimeError) and e.args else type(e).__name__}")
            print(f"  day {day}: FAILED {e!r} - continuing")

    # the main feed is rebuilt from ALL episodes on disk, every run
    build_feed(eps)
    build_index(eps)
    # weekly collections: a failure here must never cost the daily episode,
    # so it is caught and surfaced in the heartbeat instead
    weekly_note = ""
    if eps:
        try:
            import weekly
            n = weekly.update(eps)
            weekly_note = f" weekly:+{n}" if n else ""
            if n:
                build_feed(eps)  # the new week also belongs in the daily feed
        except Exception as e:
            weekly_note = f" weekly-FAILED:{type(e).__name__}"
            fails.append(f"weekly:{type(e).__name__}")
            print(f"weekly collections failed: {e!r}")
    label = f"day(s) {', '.join(map(str, made))}" if made else "feed refresh"
    try:
        pushed = "PUSHED" if publish(f"Episodes: {label}") else "no-push"
    except Exception as e:  # push can fail right after laptop wake (no
        pushed = f"PUSH-FAILED:{type(e).__name__}"  # network) - commit is
        print(f"publish failed: {e!r}")             # local, next run retries
    # HONEST STATUS TOKEN. This line is the watchdog's only build-layer
    # signal, so it must never read OK for a run that lost episodes or
    # failed to publish. quota-429 is deliberate pacing, NOT a failure.
    ondisk = len(load_state())
    if pushed.startswith("PUSH-FAILED") or ondisk != len(eps):
        status = "FAIL"
    elif fails or (made and pushed == "no-push"):
        status = "WARN"
    else:
        status = "OK"
    line = (f"{datetime.now():%Y-%m-%d %H:%M} {status} made:{len(made)} "
            f"ondisk:{ondisk} ledger:{len(eps)} "
            f"missing:{len([d for d in days if d not in eps])} "
            f"key:{KEY_LABEL} {pushed}{weekly_note}"
            + (f" failed:{','.join(fails)}" if fails else "")
            + (f" stopped:{stopped}" if stopped else ""))
    with open(RUN_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


if __name__ == "__main__":
    main()
