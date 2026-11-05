#!/usr/bin/env python3
"""
NBA Highlights Watcher
----------------------
Follows the NBA schedule for your teams. Once one of their games is final,
it checks your YouTube channels every run (every ~5 minutes) until each
channel has posted the highlights, sends you ONE spoiler-free WhatsApp
message per channel (via CallMeBot), and then goes quiet until the next game.

Spoiler safety:
  * Never reads, stores, logs or sends scores.
  * Never logs or sends video titles — only the team names, channel and link.

Usage:
  python watcher.py                 normal run (what GitHub Actions calls)
  python watcher.py --check         show resolved channels + upcoming games (no scores)
  python watcher.py --test-whatsapp send a test WhatsApp message

Secrets (environment variables):
  CALLMEBOT_PHONE    your WhatsApp number in international format, e.g. +972501234567
  CALLMEBOT_APIKEY   the API key CallMeBot sent you
  DRY_RUN=1          print messages instead of sending them
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("WATCHER_CONFIG", ROOT / "config.yaml"))
STATE_PATH = Path(os.environ.get("WATCHER_STATE", ROOT / "state.json"))

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={date}"
YT_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
USER_AGENT = "Mozilla/5.0 (compatible; nba-highlights-watcher/1.0)"

ET_TZ = ZoneInfo("America/New_York")  # NBA schedule dates are US Eastern
MIN_MINUTES_BEFORE_STATUS_CHECK = 110  # a game can't be over sooner than this
FORGET_GAMES_AFTER_DAYS = 3

# ── Teams: ESPN abbreviation + words a YouTube title might use ────────────
# City names are included only where they are unique to one team.
TEAMS = {
    "Hawks":         ("ATL", ["hawks", "atlanta"]),
    "Celtics":       ("BOS", ["celtics", "boston"]),
    "Nets":          ("BKN", ["nets", "brooklyn"]),
    "Hornets":       ("CHA", ["hornets", "charlotte"]),
    "Bulls":         ("CHI", ["bulls", "chicago"]),
    "Cavaliers":     ("CLE", ["cavaliers", "cavs", "cleveland"]),
    "Mavericks":     ("DAL", ["mavericks", "mavs", "dallas"]),
    "Nuggets":       ("DEN", ["nuggets", "denver"]),
    "Pistons":       ("DET", ["pistons", "detroit"]),
    "Warriors":      ("GS",  ["warriors", "golden state", "dubs"]),
    "Rockets":       ("HOU", ["rockets", "houston"]),
    "Pacers":        ("IND", ["pacers", "indiana"]),
    "Clippers":      ("LAC", ["clippers"]),
    "Lakers":        ("LAL", ["lakers"]),
    "Grizzlies":     ("MEM", ["grizzlies", "memphis"]),
    "Heat":          ("MIA", ["heat", "miami"]),
    "Bucks":         ("MIL", ["bucks", "milwaukee"]),
    "Timberwolves":  ("MIN", ["timberwolves", "wolves", "minnesota"]),
    "Pelicans":      ("NO",  ["pelicans", "new orleans"]),
    "Knicks":        ("NY",  ["knicks", "new york"]),
    "Thunder":       ("OKC", ["thunder", "oklahoma city", "okc"]),
    "Magic":         ("ORL", ["magic", "orlando"]),
    "76ers":         ("PHI", ["76ers", "sixers", "philadelphia"]),
    "Suns":          ("PHX", ["suns", "phoenix"]),
    "Trail Blazers": ("POR", ["trail blazers", "blazers", "portland"]),
    "Kings":         ("SAC", ["kings", "sacramento"]),
    "Spurs":         ("SA",  ["spurs", "san antonio"]),
    "Raptors":       ("TOR", ["raptors", "toronto"]),
    "Jazz":          ("UTAH", ["jazz", "utah"]),
    "Wizards":       ("WSH", ["wizards", "washington"]),
}
ABBR_TO_TEAM = {abbr: name for name, (abbr, _) in TEAMS.items()}
# ESPN occasionally uses alternate abbreviations
ABBR_TO_TEAM.update({"GSW": "Warriors", "NOP": "Pelicans", "NYK": "Knicks",
                     "SAS": "Spurs", "UTA": "Jazz", "WAS": "Wizards", "BRK": "Nets",
                     "PHO": "Suns"})


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


def norm(text: str) -> str:
    """lowercase, keep letters/digits, single spaces, padded for word matching."""
    return " " + re.sub(r"[^a-z0-9]+", " ", text.lower()).strip() + " "


def contains_phrase(normalized_text: str, phrase: str, *, word_start: bool = False) -> bool:
    """Exact word/phrase match; with word_start, 'highlight' also matches 'highlights'."""
    p = norm(phrase)
    return (p[:-1] if word_start else p) in normalized_text


def resolve_team(name: str) -> str:
    key = norm(name)
    for team, (abbr, aliases) in TEAMS.items():
        if key == norm(team) or key == norm(abbr) or any(key == norm(a) for a in aliases):
            return team
    raise SystemExit(f"Unknown team in config.yaml: {name!r}. "
                     f"Use one of: {', '.join(TEAMS)}")


def parse_time(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    # ESPN uses "2026-11-05T00:30Z" (no seconds)
    if re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d\+", s):
        s = s[:16] + ":00" + s[16:]
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── HTTP ──────────────────────────────────────────────────────────────────
def http_get(url: str, *, retries: int = 3, headers: dict | None = None) -> str:
    hdrs = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    hdrs.update(headers or {})
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url.split('?')[0]} ({last_err})")


# ── Config / state ────────────────────────────────────────────────────────
def load_config() -> dict:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    teams = [resolve_team(t) for t in cfg.get("teams") or []]
    if not 1 <= len(teams) <= 5:
        raise SystemExit("config.yaml: list 1–5 teams under 'teams:'")
    channels = cfg.get("channels") or []
    if not channels:
        raise SystemExit("config.yaml: list your YouTube channels under 'channels:'")
    for ch in channels:
        if not ch.get("id") or "CHANNEL_" in str(ch.get("id")):
            raise SystemExit("config.yaml: fill in the real channel handles under 'channels:'")
        ch.setdefault("name", ch["id"])
    return {
        "teams": teams,
        "channels": channels,
        "must": [str(w) for w in cfg.get("title_must_contain") or ["highlight"]],
        "must_not": [str(w) for w in cfg.get("title_must_not_contain") or []],
        "give_up_hours": float(cfg.get("give_up_after_hours", 4)),
        "notify_give_up": bool(cfg.get("notify_when_giving_up", False)),
    }


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("state.json was unreadable — starting fresh")
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ── YouTube ───────────────────────────────────────────────────────────────
def resolve_channel_id(raw: str, state: dict) -> str:
    raw = raw.strip()
    cache = state.setdefault("channel_ids", {})
    if raw in cache:
        return cache[raw]
    m = re.search(r"(UC[\w-]{22})", raw)
    if m and ("/channel/" in raw or raw.startswith("UC")):
        cid = m.group(1)
    else:
        if raw.startswith("http"):
            url = raw
        else:
            url = "https://www.youtube.com/" + (raw if raw.startswith("@") else "@" + raw)
        html = http_get(url, headers={"Cookie": "CONSENT=YES+1; SOCS=CAI"})
        cid = None
        for pat in (r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
                    r'"externalId":"(UC[\w-]{22})"',
                    r'<meta itemprop="identifier" content="(UC[\w-]{22})"',
                    r'"channelId":"(UC[\w-]{22})"'):
            mm = re.search(pat, html)
            if mm:
                cid = mm.group(1)
                break
        if not cid:
            raise RuntimeError(f"Couldn't find the channel ID for {raw!r}. "
                               "Put the channel ID (starts with UC…) in config.yaml instead.")
    cache[raw] = cid
    return cid


def fetch_feed(cid: str) -> list[dict]:
    xml = http_get(YT_FEED.format(cid=cid))
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    root = ET.fromstring(xml)
    videos = []
    for e in root.findall("a:entry", ns):
        vid = e.findtext("yt:videoId", default="", namespaces=ns)
        title = e.findtext("a:title", default="", namespaces=ns)
        pub = e.findtext("a:published", default="", namespaces=ns)
        if vid and pub:
            videos.append({"id": vid, "title": title, "published": parse_time(pub)})
    return videos


def is_highlight_for(title: str, teams: list[str], cfg: dict) -> bool:
    t = norm(title)
    if not any(contains_phrase(t, w, word_start=True) for w in cfg["must"]):
        return False
    if any(contains_phrase(t, w, word_start=True) for w in cfg["must_not"]):
        return False
    return all(any(contains_phrase(t, a) for a in TEAMS[team][1]) for team in teams)


# ── Schedule (ESPN) — only teams, start time and status are read ──────────
def fetch_games(et_date: str, followed: set[str]) -> list[dict]:
    data = json.loads(http_get(ESPN_SCOREBOARD.format(date=et_date.replace("-", ""))))
    games = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        home = away = None
        for c in comp.get("competitors", []):
            team = c.get("team", {})
            name = ABBR_TO_TEAM.get(team.get("abbreviation", "").upper())
            if not name:
                try:
                    name = resolve_team(team.get("name", ""))
                except SystemExit:
                    name = None
            if c.get("homeAway") == "home":
                home = name
            else:
                away = name
        if not home or not away or not ({home, away} & followed):
            continue
        st = (ev.get("status") or comp.get("status") or {}).get("type", {})
        status_name = str(st.get("name", "")).upper()
        if any(x in status_name for x in ("POSTPONED", "CANCELED", "CANCELLED", "SUSPENDED")):
            status = "off"
        elif st.get("completed") and st.get("state") == "post":
            status = "final"
        elif st.get("state") == "in":
            status = "live"
        else:
            status = "scheduled"
        games.append({"id": str(ev["id"]), "start": iso(parse_time(ev["date"])),
                      "home": home, "away": away, "status": status})
    return games


def label(g: dict) -> str:
    return f"{g['away']} @ {g['home']}"


# ── WhatsApp via CallMeBot ────────────────────────────────────────────────
def send_whatsapp(text: str) -> bool:
    if os.environ.get("DRY_RUN") == "1":
        log("DRY_RUN — would send WhatsApp:\n" + text)
        return True
    phone = os.environ.get("CALLMEBOT_PHONE", "").strip()
    key = os.environ.get("CALLMEBOT_APIKEY", "").strip()
    if not phone or not key:
        log("ERROR: CALLMEBOT_PHONE / CALLMEBOT_APIKEY secrets are not set")
        return False
    url = CALLMEBOT_URL + "?" + urllib.parse.urlencode(
        {"phone": phone, "text": text, "apikey": key})
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", errors="replace")
            if r.status == 200 and "error" not in body.lower():
                return True
            log(f"CallMeBot answered but didn't confirm (attempt {attempt + 1})")
        except Exception as e:  # noqa: BLE001
            log(f"CallMeBot request failed (attempt {attempt + 1}): {type(e).__name__}")
        time.sleep(5 * (attempt + 1))
    return False


def highlight_message(g: dict, channel_name: str, video_id: str) -> str:
    return (f"🏀 Highlights are up!\n"
            f"{g['away']} vs {g['home']}\n"
            f"Channel: {channel_name}\n"
            f"https://www.youtube.com/watch?v={video_id}")


# ── Main run ──────────────────────────────────────────────────────────────
def run(now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    cfg = load_config()
    state = load_state()
    followed = set(cfg["teams"])
    games: dict = state.setdefault("games", {})
    give_up = timedelta(hours=cfg["give_up_hours"])
    channel_keys = [str(ch["id"]).strip() for ch in cfg["channels"]]

    # 1) Refresh the schedule once per (ET) day, and whenever a game might be over.
    et_today = now.astimezone(ET_TZ).date()
    need_status = [g for g in games.values()
                   if g["status"] in ("scheduled", "live")
                   and now >= parse_time(g["start"]) + timedelta(minutes=MIN_MINUTES_BEFORE_STATUS_CHECK)]
    if state.get("schedule_day") != et_today.isoformat() or need_status:
        days = {et_today - timedelta(days=1), et_today}
        days |= {parse_time(g["start"]).astimezone(ET_TZ).date() for g in need_status}
        for d in sorted(days):
            try:
                fetched = fetch_games(d.isoformat(), followed)
            except Exception as e:  # noqa: BLE001
                log(f"Schedule lookup failed for {d}: {e}")
                continue
            for fg in fetched:
                g = games.setdefault(fg["id"], {**fg, "status": "scheduled",
                                                "notified": {}, "done": False})
                g.update(start=fg["start"], home=fg["home"], away=fg["away"])
                if g["done"]:
                    continue
                if fg["status"] == "off":
                    g["status"], g["done"] = "off", True
                    log(f"{label(g)}: postponed/cancelled — skipping")
                elif fg["status"] == "final" and g["status"] != "final":
                    g["status"] = "final"
                    g["final_seen"] = iso(now)
                    log(f"{label(g)}: game over — now watching the channels")
                elif fg["status"] != "final":
                    g["status"] = fg["status"]
        state["schedule_day"] = et_today.isoformat()

    # 2) For finished games, look for highlights on channels that haven't posted yet.
    active = [g for g in games.values() if g["status"] == "final" and not g["done"]]
    feeds: dict[str, list[dict]] = {}
    for g in active:
        final_seen = parse_time(g["final_seen"])
        start = parse_time(g["start"])
        for ch, key in zip(cfg["channels"], channel_keys):
            if key in g["notified"]:
                continue
            if key not in feeds:
                try:
                    feeds[key] = fetch_feed(resolve_channel_id(key, state))
                except Exception as e:  # noqa: BLE001
                    log(f"Couldn't read channel {ch['name']}: {e}")
                    feeds[key] = []
            match = next((v for v in sorted(feeds[key], key=lambda v: v["published"])
                          if v["published"] >= start
                          and is_highlight_for(v["title"], [g["away"], g["home"]], cfg)), None)
            if match:
                if send_whatsapp(highlight_message(g, ch["name"], match["id"])):
                    g["notified"][key] = match["id"]
                    log(f"{label(g)}: highlights found on {ch['name']} — WhatsApp sent")
                else:
                    log(f"{label(g)}: highlights found on {ch['name']} but sending failed — will retry")
            else:
                log(f"{label(g)}: nothing yet on {ch['name']}")

        missing = [ch for ch, key in zip(cfg["channels"], channel_keys) if key not in g["notified"]]
        if not missing:
            g["done"] = True
            log(f"{label(g)}: all channels posted — done with this game")
        elif now - final_seen >= give_up:
            g["done"] = True
            names = ", ".join(ch["name"] for ch in missing)
            log(f"{label(g)}: gave up waiting for {names}")
            if cfg["notify_give_up"]:
                send_whatsapp(f"ℹ️ No highlights from {names} for {g['away']} vs {g['home']} "
                              f"after {cfg['give_up_hours']:g}h — stopped checking.")

    # 3) Forget old games, keep a daily heartbeat (keeps the GitHub schedule alive).
    cutoff = now - timedelta(days=FORGET_GAMES_AFTER_DAYS)
    for gid in [gid for gid, g in games.items() if parse_time(g["start"]) < cutoff]:
        del games[gid]
    state["heartbeat"] = now.date().isoformat()
    save_state(state)

    waiting = sum(1 for g in games.values() if not g["done"])
    if not active:
        log(f"Idle — {waiting} upcoming/ongoing game(s) for your teams in the next day or so")


def check() -> None:
    cfg = load_config()
    state = load_state()
    print("Teams:", ", ".join(cfg["teams"]))
    for ch in cfg["channels"]:
        try:
            cid = resolve_channel_id(str(ch['id']), state)
            vids = fetch_feed(cid)
            latest = max((v["published"] for v in vids), default=None)
            print(f"Channel {ch['name']}: {cid} — feed OK, {len(vids)} recent videos, "
                  f"latest upload {latest:%d %b %Y} " if latest else
                  f"Channel {ch['name']}: {cid} — feed OK but no videos found")
        except Exception as e:  # noqa: BLE001
            print(f"Channel {ch['name']}: ERROR {e}")
    now = datetime.now(timezone.utc)
    et_today = now.astimezone(ET_TZ).date()
    for d in (et_today, et_today + timedelta(days=1)):
        for g in fetch_games(d.isoformat(), set(cfg["teams"])):
            local = parse_time(g["start"]).astimezone(ZoneInfo("Asia/Jerusalem"))
            print(f"  {label(g)} — tips off {local:%a %d %b %H:%M} Israel time")
    save_state(state)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--check":
        check()
    elif arg == "--test-whatsapp":
        ok = send_whatsapp("🏀 Test from your NBA Highlights Watcher — it works!\n"
                           "https://www.youtube.com/watch?v=jNQXAC9IVRw")
        print("Sent!" if ok else "Sending failed — check your secrets.")
        sys.exit(0 if ok else 1)
    else:
        run()
