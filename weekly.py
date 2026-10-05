"""Weekly collections: each calendar week's episodes as ONE audiobook-style
file with chapter markers, in their own feed (2026-10-05, Brian: "a
grouping every week... listen to a group almost like an audio book").

- Week = Monday-Sunday by episode publish date, so v2 weeks run lesson,
  lesson, lesson, lesson, lab, briefing, review. A week compiles once its
  Sunday episode exists (or the week is over), and RE-compiles whenever
  its episode set changes (a re-render gives a new content key).
- Audio is hosted on its OWN Pages site, repo bel9777/ai-applied-weekly-audio
  (local clone ~/ai-applied-weekly-audio), so it has a separate 1 GB
  cap from this repo (audio here grows ~3 MB/day). GitHub Release assets were tried
  first and podcast apps refused them ("can't be played on this device"):
  they are served as application/octet-stream + nosniff behind an
  expiring redirect. Pages serves audio/mp3.
- The weeks appear as "Full Week N" items in the daily feed and in their
  own feed, docs/weekly.xml. Ledger: data/weekly.json.

Usage: py weekly.py   (podcast.py --run also calls update() every morning)
"""

import hashlib
import json
import subprocess
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

import podcast

REPO = podcast.REPO
LEDGER = REPO / "data" / "weekly.json"
# scratch OUTSIDE the repo: publish() runs `git add -A`
BUILD = Path.home() / "AppData" / "Local" / "ai-applied-weekly"
FFPROBE = podcast.FFMPEG.with_name("ffprobe.exe")
HOST = Path.home() / "ai-applied-weekly-audio"
ASSET_BASE = "https://bel9777.github.io/ai-applied-weekly-audio/weekly"
COURSE_WEEK1 = date(2026, 10, 12)  # Monday of Day 1 (2026-10-12)
GAP_SECONDS = 1.5


def _mesc(text):
    r"""FFMETADATA escaping: = ; # \ take a backslash; newlines dropped."""
    for ch in "\\=;#":
        text = text.replace(ch, "\\" + ch)
    return text.replace("\n", " ")


def _week_start(iso):
    d = datetime.fromisoformat(iso).date()
    return d - timedelta(days=d.weekday())


def _duration(path):
    r = subprocess.run([str(FFPROBE), "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def _git(*args):
    r = subprocess.run(["git", "-C", str(HOST), *args], capture_output=True,
                       text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {args[0]} in weekly host failed: {r.stderr[:200]}")
    return r


def _publish_host(names):
    """Commit + push new week files, then wait (max ~5 min) until Pages
    serves the last one, so the feed never advertises a 404."""
    _git("pull", "--rebase", "--quiet")
    _git("add", "weekly")
    _git("commit", "--quiet", "-m", f"Weekly audio: {', '.join(names)}")
    _git("push", "--quiet")
    import time
    import urllib.request
    for _ in range(30):
        try:
            req = urllib.request.Request(f"{ASSET_BASE}/{names[-1]}", method="HEAD")
            if urllib.request.urlopen(req, timeout=20).status == 200:
                return
        except Exception:
            pass
        time.sleep(10)
    print("  weekly: Pages not serving yet - feed will catch up next run")


def _compile(week, eps, out):
    BUILD.mkdir(parents=True, exist_ok=True)
    gap = BUILD / "gap.mp3"
    if not gap.exists():
        subprocess.run([str(podcast.FFMPEG), "-y", "-loglevel", "error", "-f",
                        "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t",
                        str(GAP_SECONDS), "-b:a", "64k", str(gap)], check=True)
    gap_len = _duration(gap)
    lines, meta, chapters, t = [], [";FFMETADATA1",
                                    f"title={_mesc(week['title'])}",
                                    "artist=AI Applied",
                                    "album=AI Applied Weekly Collections"], [], 0.0
    for i, e in enumerate(eps):
        src = REPO / "docs" / e["audioPath"].lstrip("/")
        if i:
            lines.append(f"file '{gap.as_posix()}'")
            t += gap_len
        lines.append(f"file '{src.as_posix()}'")
        dur = _duration(src)
        title = f"Day {e['day']}: {e['title']}"
        meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(t * 1000)}",
                 f"END={int((t + dur) * 1000)}", f"title={_mesc(title)}"]
        chapters.append({"start": round(t), "title": title})
        t += dur
    lst = BUILD / "list.txt"
    lst.write_text("\n".join(lines), encoding="utf-8")
    meta_f = BUILD / "meta.txt"
    meta_f.write_text("\n".join(meta) + "\n", encoding="utf-8")
    subprocess.run([str(podcast.FFMPEG), "-y", "-loglevel", "error",
                    "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-i", str(meta_f), "-map", "0:a", "-map_metadata", "1",
                    "-map_chapters", "1", "-ac", "1", "-ar", "24000",
                    "-b:a", "64k", "-id3v2_version", "3", str(out)],
                   check=True)
    return chapters, int(t)


def update(eps=None, today=None):
    """Compile/upload every complete week whose content changed. Returns
    the number of weeks (re)built. Never touches the daily feed."""
    eps = eps if eps is not None else podcast.load_state()
    today = today or date.today()
    ledger = {w["weekStart"]: w for w in
              (json.loads(LEDGER.read_text(encoding="utf-8"))
               if LEDGER.exists() else [])}
    weeks = {}
    for e in eps.values():
        weeks.setdefault(_week_start(e["publishedAt"]), []).append(e)
    built, new_files = 0, []
    for start, items in sorted(weeks.items()):
        items.sort(key=lambda e: e["day"])
        end = start + timedelta(days=6)
        has_sunday = any(datetime.fromisoformat(e["publishedAt"]).date() == end
                         for e in items)
        if not (end < today or has_sunday):
            continue  # week still in progress
        key = hashlib.sha1("|".join(e["audioPath"] for e in items)
                           .encode()).hexdigest()[:8]
        if ledger.get(start.isoformat(), {}).get("key") == key:
            continue
        n = (start - COURSE_WEEK1).days // 7 + 1
        span = (f"{start:%b} {start.day}–{end.day}" if start.month == end.month
                else f"{start:%b} {start.day}–{end:%b} {end.day}")
        week = {"weekStart": start.isoformat(), "week": n, "key": key,
                "title": f"Week {n} ({span}): Days {items[0]['day']}–{items[-1]['day']}",
                "days": [e["day"] for e in items]}
        name = f"week-{n:02d}-{start.isoformat()}-{key}.mp3"
        out = HOST / "weekly" / name
        out.parent.mkdir(parents=True, exist_ok=True)
        week["chapters"], week["durationSeconds"] = _compile(week, items, out)
        old = ledger.get(start.isoformat(), {}).get("url", "")
        stale = HOST / "weekly" / old.rsplit("/", 1)[-1] if old else None
        if stale and stale.exists() and stale != out:
            stale.unlink()  # superseded re-compile; git history keeps it
        week["url"] = f"{ASSET_BASE}/{name}"
        week["bytes"] = out.stat().st_size
        # publish the collection on the morning its last episode came out
        week["publishedAt"] = max(e["publishedAt"] for e in items)
        ledger[start.isoformat()] = week
        new_files.append(name)
        built += 1
        print(f"  weekly: {week['title']} ({week['durationSeconds'] // 60} min)")
    if new_files:
        _publish_host(new_files)
    LEDGER.write_text(json.dumps(sorted(ledger.values(), key=lambda w: w["weekStart"],
                                        reverse=True), indent=1,
                                 ensure_ascii=False), encoding="utf-8")
    build_feed(list(ledger.values()))
    return built


def _ts(s):
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def build_feed(weeks):
    items = []
    for w in sorted(weeks, key=lambda w: w["weekStart"], reverse=True):
        chap = "".join(f"{_ts(c['start'])} {escape(c['title'])}&lt;br/&gt;"
                       for c in w["chapters"])
        items.append(f"""    <item>
      <title>{escape(w['title'])}</title>
      <description>{chap}</description>
      <guid isPermaLink="false">weekly-{w['weekStart']}-{w['key']}</guid>
      <pubDate>{podcast.rfc822(w['publishedAt'])}</pubDate>
      <enclosure url="{w['url']}" length="{w['bytes']}" type="audio/mpeg"/>
      <itunes:duration>{_ts(w['durationSeconds'])}</itunes:duration>
    </item>""")
    site = podcast.SITE
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>AI Applied — Weekly Collections</title>
    <link>{site}/</link>
    <language>en-us</language>
    <description>Each week of AI Applied Daily as one continuous listen, with a chapter per day. Companion to the daily feed.</description>
    <itunes:author>AI Applied</itunes:author>
    <itunes:type>episodic</itunes:type>
    <itunes:image href="{site}/cover.png"/>
    <atom:link href="{site}/weekly.xml" rel="self" type="application/rss+xml"/>
{chr(10).join(items)}
  </channel>
</rss>
"""
    (REPO / "docs" / "weekly.xml").write_text(feed, encoding="utf-8")


if __name__ == "__main__":
    print(f"weeks built: {update()}")
