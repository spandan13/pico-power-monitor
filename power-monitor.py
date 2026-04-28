"""
power_monitor.py
================
Python 3 server that monitors home electricity uptime via a Raspberry Pi Pico 2W
(HTTP polling), sends Telegram alerts, logs outage events to a text file,
and serves a web dashboard.

Dependencies:
    pip install requests python-telegram-bot==13.13 flask

Configuration: config.ini  (see template comment below)
"""

# ─────────────────────────────────────────────────────────────────────────────
#  IMPORTS
# ─────────────────────────────────────────────────────────────────────────────
import os
import sys
import json
import time
import logging
import threading
import configparser
from datetime import datetime, timedelta

import requests
import telegram
from flask import Flask, jsonify, render_template_string, request

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION  (config.ini)
#
#  [pico]
#  url            = http://192.168.1.42
#  device_id      = home_pico
#
#  [telegram]
#  token          = YOUR_BOT_TOKEN
#  chat_id        = YOUR_CHAT_ID
#
#  [monitor]
#  log_file       = power_log.txt
#  poll_interval  = 5
#  http_timeout   = 4
#
#  [web]
#  host           = 0.0.0.0
#  port           = 8080
# ─────────────────────────────────────────────────────────────────────────────
_cfg = configparser.ConfigParser()
if not _cfg.read("config.ini"):
    sys.exit(
        "ERROR: config.ini not found.\n"
        "Create one — see the template comment at the top of this file."
    )

CONFIG = {
    "pico_url":         _cfg.get("pico",    "url").rstrip("/"),
    "device_id":        _cfg.get("pico",    "device_id"),
    "telegram_token":   _cfg.get("telegram", "token"),
    "telegram_chat_id": _cfg.get("telegram", "chat_id"),
    "log_file":         _cfg.get("monitor",    "log_file",      fallback="power_log.txt"),
    "poll_interval":    _cfg.getint("monitor", "poll_interval", fallback=5),
    "http_timeout":     _cfg.getint("monitor", "http_timeout",  fallback=4),
    "web_host":         _cfg.get("web",    "host", fallback="0.0.0.0"),
    "web_port":         _cfg.getint("web", "port", fallback=8080),
}

DEVICE_ID       = CONFIG["device_id"]
PICO_STATUS_URL = CONFIG["pico_url"] + "/status"

# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("monitor")

# ─────────────────────────────────────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────────────────────────────────────
state = {
    "current_status":    "unknown",
    "last_seen":         0.0,
    "offline_since":     None,
    "is_simulated":      False,
    "alert_sent":        False,
    "consecutive_fails": 0,
}
state_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
#  LOG FILE
#  Format: TYPE|START_ISO|END_ISO|DURATION_SECONDS
#  TYPE  : REAL | SIMULATED
# ─────────────────────────────────────────────────────────────────────────────
LOG_FILE = CONFIG["log_file"]
log_lock = threading.Lock()   # protect concurrent file reads/writes


def append_log_entry(evt_type: str, start: float, end: float):
    duration = int(end - start)
    start_s  = datetime.fromtimestamp(start).strftime("%Y-%m-%dT%H:%M:%S")
    end_s    = datetime.fromtimestamp(end).strftime("%Y-%m-%dT%H:%M:%S")
    line     = f"{evt_type}|{start_s}|{end_s}|{duration}\n"
    with log_lock:
        with open(LOG_FILE, "a") as f:
            f.write(line)
    log.info(f"Logged: {line.strip()}")


def parse_log_entries():
    """Return list of dicts (newest-last). Each dict includes 'idx' = line number."""
    entries = []
    if not os.path.exists(LOG_FILE):
        return entries
    with log_lock:
        with open(LOG_FILE) as f:
            lines = f.readlines()
    for idx, raw in enumerate(lines):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split("|")
        if len(parts) != 4:
            continue
        try:
            entries.append({
                "idx":      idx,
                "type":     parts[0],
                "start":    datetime.fromisoformat(parts[1]),
                "end":      datetime.fromisoformat(parts[2]),
                "duration": int(parts[3]),
            })
        except Exception:
            pass
    return entries


def delete_log_entry_by_idx(line_idx: int) -> bool:
    """Remove the line at line_idx (0-based) from the log file."""
    if not os.path.exists(LOG_FILE):
        return False
    with log_lock:
        with open(LOG_FILE) as f:
            lines = f.readlines()
        if line_idx < 0 or line_idx >= len(lines):
            return False
        del lines[line_idx]
        with open(LOG_FILE, "w") as f:
            f.writelines(lines)
    log.info(f"Deleted log entry at line {line_idx}")
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  DURATION FORMATTING
#  < 60 s    → "N seconds"
#  < 3600 s  → "N minutes"
#  >= 3600 s → "N hours"
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    if seconds < 3600:
        mins = round(seconds / 60, 1)
        display = int(mins) if mins == int(mins) else mins
        return f"{display} minute{'s' if display != 1 else ''}"
    hrs = round(seconds / 3600, 2)
    display = int(hrs) if hrs == int(hrs) else hrs
    return f"{display} hour{'s' if display != 1 else ''}"


def _duration_unit(seconds: int) -> dict:
    """Return dict with normalised value and unit label for chart axes."""
    if seconds < 60:
        return {"value": seconds,                "unit": "seconds"}
    if seconds < 3600:
        return {"value": round(seconds / 60, 1), "unit": "minutes"}
    return     {"value": round(seconds / 3600, 2), "unit": "hours"}


# ─────────────────────────────────────────────────────────────────────────────
#  DOWNTIME CALCULATIONS
# ─────────────────────────────────────────────────────────────────────────────

def _filter_real(entries):
    return [e for e in entries if e["type"] == "REAL"]


def _total_seconds(entries, since: datetime, until: datetime) -> int:
    total = 0
    for e in entries:
        s  = max(e["start"], since)
        en = min(e["end"],   until)
        if en > s:
            total += int((en - s).total_seconds())
    return total


def daily_stats(entries):
    now   = datetime.now()
    since = now.replace(hour=0, minute=0, second=0, microsecond=0)
    secs  = _total_seconds(_filter_real(entries), since, now)
    return {"seconds": secs, "human": _fmt_duration(secs),
            "percentage": round(secs / 86400 * 100, 2)}


def weekly_stats(entries):
    now   = datetime.now()
    since = now - timedelta(days=7)
    secs  = _total_seconds(_filter_real(entries), since, now)
    return {"seconds": secs, "human": _fmt_duration(secs),
            "percentage": round(secs / (7 * 86400) * 100, 2)}


def monthly_stats(entries):
    now    = datetime.now()
    since  = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    secs   = _total_seconds(_filter_real(entries), since, now)
    window = (now - since).total_seconds()
    return {"seconds": secs, "human": _fmt_duration(secs),
            "percentage": round(secs / window * 100, 2) if window > 0 else 0}


# ─────────────────────────────────────────────────────────────────────────────
#  CHART DATA  (each chart picks its own best unit based on its max bucket)
# ─────────────────────────────────────────────────────────────────────────────

def _normalise_series(raw_list: list, divisor: float) -> list:
    return [round(v / divisor, 2) for v in raw_list]


def _chart_unit_and_divisor(raw_real, raw_sim):
    max_val  = max(max(raw_real, default=0), max(raw_sim, default=0))
    ud       = _duration_unit(max_val)
    divisor  = {"seconds": 1, "minutes": 60, "hours": 3600}[ud["unit"]]
    return ud["unit"], divisor


def chart_daily(entries):
    now    = datetime.now()
    day0   = now.replace(hour=0, minute=0, second=0, microsecond=0)
    raw_r  = [0] * 24
    raw_s  = [0] * 24
    labels = [f"{h:02d}:00" for h in range(24)]
    for e in entries:
        buckets = raw_r if e["type"] == "REAL" else raw_s
        for h in range(24):
            bs = day0 + timedelta(hours=h)
            be = bs + timedelta(hours=1)
            s  = max(e["start"], bs)
            en = min(e["end"],   be)
            if en > s:
                buckets[h] += int((en - s).total_seconds())
    unit, div = _chart_unit_and_divisor(raw_r, raw_s)
    return {"labels": labels, "real": _normalise_series(raw_r, div),
            "simulated": _normalise_series(raw_s, div), "unit": unit}


def chart_weekly(entries):
    now    = datetime.now()
    labels, raw_r, raw_s = [], [], []
    for d in range(6, -1, -1):
        ds = (now - timedelta(days=d)).replace(hour=0, minute=0, second=0, microsecond=0)
        de = ds + timedelta(days=1)
        labels.append(ds.strftime("%a %d"))
        r = s = 0
        for e in entries:
            ov_s = max(e["start"], ds)
            ov_e = min(e["end"],   de)
            if ov_e > ov_s:
                dur = int((ov_e - ov_s).total_seconds())
                if e["type"] == "REAL": r += dur
                else:                   s += dur
        raw_r.append(r)
        raw_s.append(s)
    unit, div = _chart_unit_and_divisor(raw_r, raw_s)
    return {"labels": labels, "real": _normalise_series(raw_r, div),
            "simulated": _normalise_series(raw_s, div), "unit": unit}


def chart_monthly(entries):
    now     = datetime.now()
    month_s = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    labels, raw_r, raw_s = [], [], []
    day = month_s
    while day.month == now.month and day <= now:
        nd = day + timedelta(days=1)
        labels.append(day.strftime("%-d"))
        r = s = 0
        for e in entries:
            ov_s = max(e["start"], day)
            ov_e = min(e["end"],   nd)
            if ov_e > ov_s:
                dur = int((ov_e - ov_s).total_seconds())
                if e["type"] == "REAL": r += dur
                else:                   s += dur
        raw_r.append(r)
        raw_s.append(s)
        day = nd
    unit, div = _chart_unit_and_divisor(raw_r, raw_s)
    return {"labels": labels, "real": _normalise_series(raw_r, div),
            "simulated": _normalise_series(raw_s, div), "unit": unit}


# ─────────────────────────────────────────────────────────────────────────────
#  RECENT EVENTS  (last 6, newest first)
# ─────────────────────────────────────────────────────────────────────────────

def recent_events(entries, n=6):
    sorted_e = sorted(entries, key=lambda e: e["start"], reverse=True)
    out = []
    for e in sorted_e[:n]:
        out.append({
            "idx":      e["idx"],
            "type":     e["type"],
            "start":    e["start"].strftime("%H:%M  %d %b %Y"),
            "end":      e["end"].strftime("%H:%M  %d %b %Y"),
            "duration": _fmt_duration(e["duration"]),
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────────────────────────────────────
_tg_bot = None


def _get_bot():
    global _tg_bot
    if _tg_bot is None and CONFIG["telegram_token"] not in ("", "YOUR_BOT_TOKEN"):
        try:
            _tg_bot = telegram.Bot(token=CONFIG["telegram_token"])
        except Exception as e:
            log.warning(f"Telegram bot init failed: {e}")
    return _tg_bot


def send_telegram(text: str):
    bot = _get_bot()
    if bot is None:
        log.info(f"[Telegram skipped – not configured] {text}")
        return
    try:
        bot.send_message(
            chat_id    = CONFIG["telegram_chat_id"],
            text       = text,
            parse_mode = telegram.ParseMode.HTML,
        )
        log.info(f"Telegram sent: {text[:100]}")
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  POWER EVENT HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

def handle_went_offline(simulated: bool):
    with state_lock:
        if state["offline_since"] is not None:
            return
        state["offline_since"] = time.time()
        state["is_simulated"]  = simulated
        state["alert_sent"]    = False

    now_str = datetime.now().strftime("%H:%M on %d %b %Y")

    if simulated:
        log.info("Electricity OFFLINE [SIMULATED]")
        send_telegram(
            "🔌 <b>Simulated Power Cut Active</b>\n"
            f"A manual power-cut simulation was triggered at <b>{now_str}</b>.\n"
            "The monitor will report <i>offline</i> until the simulation is cancelled."
        )
    else:
        log.info("Electricity OFFLINE [REAL]")
        send_telegram(
            "⚠️ <b>Power Outage Detected</b>\n"
            f"Electricity went offline at <b>{now_str}</b>.\n"
            "You will be notified when power is restored."
        )


def handle_came_online():
    with state_lock:
        offline_since = state["offline_since"]
        simulated     = state["is_simulated"]
        if offline_since is None:
            return
        state["offline_since"] = None
        state["is_simulated"]  = False
        state["alert_sent"]    = False

    now      = time.time()
    duration = int(now - offline_since)

    if duration < 10:
        log.info(f"Ignoring sub-10s offline→online transition ({duration}s)")
        return

    evt_type = "SIMULATED" if simulated else "REAL"
    append_log_entry(evt_type, offline_since, now)
    now_str  = datetime.now().strftime("%H:%M on %d %b %Y")

    if simulated:
        log.info(f"Simulated outage ended ({_fmt_duration(duration)})")
        send_telegram(
            "✅ <b>Simulation Cancelled</b>\n"
            f"Power-cut simulation ended at <b>{now_str}</b>.\n"
            f"Simulated duration: <b>{_fmt_duration(duration)}</b>.\n"
            "The monitor is now reporting live electricity status again."
        )
        return

    entries   = parse_log_entries()
    day_stats = daily_stats(entries)
    send_telegram(
        "✅ <b>Power Restored</b>\n"
        f"Electricity came back at <b>{now_str}</b>.\n"
        f"Outage duration: <b>{_fmt_duration(duration)}</b>\n"
        f"Total downtime today: <b>{day_stats['human']}</b> ({day_stats['percentage']}%)"
    )
    log.info(f"Electricity ONLINE – outage duration: {_fmt_duration(duration)}")


# ─────────────────────────────────────────────────────────────────────────────
#  HTTP POLL LOOP
# ─────────────────────────────────────────────────────────────────────────────
OFFLINE_HYSTERESIS = 3


def poll_pico():
    try:
        r = requests.get(PICO_STATUS_URL, timeout=CONFIG["http_timeout"])
        if r.status_code == 200:
            body = r.text.strip().lower()
            if body == "offline":
                return "offline", True
            return "online", False
        log.warning(f"Pico returned HTTP {r.status_code}")
        return "unreachable", False
    except requests.exceptions.Timeout:
        log.debug("Poll timeout")
        return "unreachable", False
    except requests.exceptions.ConnectionError:
        log.debug("Poll connection error")
        return "unreachable", False
    except Exception as e:
        log.warning(f"Poll error: {e}")
        return "unreachable", False


def poll_loop():
    log.info(f"Poll loop started → {PICO_STATUS_URL} every {CONFIG['poll_interval']}s")
    while True:
        result, simulated = poll_pico()
        now = time.time()

        with state_lock:
            prev_status = state["current_status"]
            fails       = state["consecutive_fails"]

        if result == "online":
            with state_lock:
                state["last_seen"]         = now
                state["consecutive_fails"] = 0
                state["current_status"]    = "online"
            if prev_status != "online":
                handle_came_online()

        elif result == "offline":
            with state_lock:
                state["last_seen"]         = now
                state["consecutive_fails"] = 0
                state["current_status"]    = "offline"
            if prev_status != "offline":
                handle_went_offline(simulated=True)

        else:
            new_fails = fails + 1
            with state_lock:
                state["consecutive_fails"] = new_fails
            log.debug(f"Pico unreachable (fail #{new_fails})")
            if new_fails >= OFFLINE_HYSTERESIS:
                with state_lock:
                    state["current_status"] = "offline"
                if prev_status != "offline":
                    handle_went_offline(simulated=False)

        time.sleep(CONFIG["poll_interval"])


# ─────────────────────────────────────────────────────────────────────────────
#  WEB DASHBOARD  (Flask)
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Electricity Uptime Monitor</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Inter:wght@400;500;700;800&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:      #0d1117;
    --card:    #161b22;
    --border:  #21262d;
    --border2: #30363d;
    --text:    #c9d1d9;
    --muted:   #484f58;
    --accent:  #38bdf8;
    --green:   #34d399;
    --red:     #f87171;
    --amber:   #fbbf24;
    --mono:    'JetBrains Mono', monospace;
    --sans:    'Inter', sans-serif;
    --radius:  10px;
  }

  html, body {
    background: var(--bg); color: var(--text);
    font-family: var(--sans); font-size: 14px;
    min-height: 100vh; padding: 1.5rem 1rem 3rem;
  }
  .page { max-width: 960px; margin: 0 auto; }

  /* ── Header ── */
  header {
    display: flex; align-items: flex-start; justify-content: space-between;
    flex-wrap: wrap; gap: 1rem;
    margin-bottom: 2rem; padding-bottom: 1.2rem; border-bottom: 1px solid var(--border);
  }
  .logo { font-size: 1.25rem; font-weight: 800; color: #e8f0fa; letter-spacing: -.02em; }
  .logo span { color: var(--accent); }
  .header-badges { display: flex; flex-direction: column; gap: .35rem; margin-top: .15rem; }
  .pico-link {
    font-family: var(--mono); font-size: .72rem; color: var(--accent);
    text-decoration: none; border: 1px solid var(--border2);
    padding: .35rem .75rem; border-radius: 6px; transition: background .15s;
    align-self: flex-start;
  }
  .pico-link:hover { background: rgba(56,189,248,.1); }
  .transport-badge {
    font-family: var(--mono); font-size: .62rem;
    background: rgba(56,189,248,.08); color: var(--accent);
    border: 1px solid rgba(56,189,248,.2); padding: .2rem .6rem; border-radius: 4px;
    display: inline-block;
  }
  .refresh-badge {
    font-family: var(--mono); font-size: .62rem; color: var(--muted);
    border: 1px solid var(--border); padding: .2rem .5rem; border-radius: 4px;
  }

  /* ── Status hero ── */
  .status-hero {
    background: var(--card); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 1.6rem 1.8rem;
    display: flex; align-items: center; justify-content: space-between;
    flex-wrap: wrap; gap: 1rem; margin-bottom: 1.5rem;
    position: relative; overflow: hidden;
  }
  .status-hero::before {
    content: ''; position: absolute; inset: 0;
    background: radial-gradient(ellipse 60% 80% at 90% 50%, rgba(56,189,248,.04), transparent);
    pointer-events: none;
  }
  .status-label { font-size: .65rem; letter-spacing: .12em; text-transform: uppercase; color: var(--muted); margin-bottom: .4rem; }
  .status-value { font-size: 1.8rem; font-weight: 800; letter-spacing: -.03em; }
  .status-value.online  { color: var(--green); }
  .status-value.offline { color: var(--red); }
  .status-value.unknown { color: var(--muted); }
  .status-meta { font-family: var(--mono); font-size: .75rem; color: var(--muted); margin-top: .3rem; }

  .pulse { width: 14px; height: 14px; border-radius: 50%; flex-shrink: 0; }
  .pulse.online  { background: var(--green); animation: pulse-g 2s infinite; }
  .pulse.offline { background: var(--red);   animation: pulse-r 2s infinite; }
  .pulse.unknown { background: var(--muted); }
  @keyframes pulse-g {
    0%   { box-shadow: 0 0 0 0   rgba(52,211,153,.4); }
    70%  { box-shadow: 0 0 0 8px rgba(52,211,153,0);  }
    100% { box-shadow: 0 0 0 0   rgba(52,211,153,0);  }
  }
  @keyframes pulse-r {
    0%   { box-shadow: 0 0 0 0   rgba(248,113,113,.4); }
    70%  { box-shadow: 0 0 0 8px rgba(248,113,113,0);  }
    100% { box-shadow: 0 0 0 0   rgba(248,113,113,0);  }
  }

  /* ── Section heading ── */
  .section-head {
    font-size: .65rem; letter-spacing: .14em; text-transform: uppercase;
    color: var(--muted); margin-bottom: .8rem; margin-top: 1.8rem;
  }

  /* ── Stats grid ── */
  .stats-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 1rem; margin-bottom: 1.5rem;
  }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); padding: 1.2rem 1.4rem; }
  .stat-card .period { font-size: .62rem; letter-spacing: .12em; text-transform: uppercase; color: var(--muted); margin-bottom: .5rem; }
  .stat-card .dur    { font-family: var(--mono); font-size: 1.1rem; font-weight: 600; color: #e8f0fa; margin-bottom: .25rem; }
  .stat-card .pct    { font-size: .75rem; color: var(--muted); }
  .stat-card .pct b  { color: var(--red); }

  /* ── Timeline ── */
  .timeline { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
  .tl-row {
    display: grid;
    grid-template-columns: 90px 1fr 1fr 110px 36px;
    align-items: center; gap: .75rem;
    padding: .75rem 1.2rem; border-bottom: 1px solid var(--border);
    font-size: .78rem; transition: background .12s;
  }
  .tl-row:last-child { border-bottom: none; }
  .tl-row:hover { background: rgba(255,255,255,.02); }
  .tl-header {
    font-size: .6rem; letter-spacing: .12em; text-transform: uppercase;
    color: var(--muted); background: rgba(0,0,0,.25); cursor: default;
  }
  .tl-badge {
    display: inline-block; font-size: .6rem; font-weight: 700; letter-spacing: .08em;
    text-transform: uppercase; padding: .18rem .5rem; border-radius: 4px;
  }
  .tl-badge.REAL      { background: rgba(248,113,113,.15); color: var(--red);   border: 1px solid rgba(248,113,113,.25); }
  .tl-badge.SIMULATED { background: rgba(251,191,36,.12);  color: var(--amber); border: 1px solid rgba(251,191,36,.2); }
  .tl-time { font-family: var(--mono); color: var(--text); font-size: .72rem; }
  .tl-dur  { font-family: var(--mono); color: var(--muted); font-size: .72rem; }
  .tl-del-btn {
    background: none; border: 1px solid var(--border2); color: var(--muted);
    border-radius: 5px; cursor: pointer; font-size: .75rem; padding: .2rem .45rem;
    transition: all .15s; line-height: 1.4;
  }
  .tl-del-btn:hover { border-color: var(--red); color: var(--red); background: rgba(248,113,113,.08); }
  .tl-empty { padding: 1.4rem 1.2rem; color: var(--muted); font-size: .8rem; font-style: italic; }

  /* ── Chart cards ── */
  .chart-card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 1.4rem 1.4rem 1.2rem; margin-bottom: 1rem;
  }
  .chart-title { font-size: .78rem; font-weight: 600; color: #c8d6e8; margin-bottom: 1rem; }
  .chart-unit  { font-size: .65rem; color: var(--muted); font-family: var(--mono); margin-left: .4rem; font-weight: 400; }
  .chart-wrap  { position: relative; height: 180px; }

  /* ── Legend ── */
  .legend { display: flex; gap: 1.2rem; margin-top: .75rem; flex-wrap: wrap; }
  .legend-item { display: flex; align-items: center; gap: .4rem; font-size: .68rem; color: var(--muted); }
  .legend-dot  { width: 8px; height: 8px; border-radius: 2px; flex-shrink: 0; }
  .legend-dot.real { background: var(--red); }
  .legend-dot.sim  { background: var(--amber); }

  /* ── Footer ── */
  footer {
    margin-top: 2.5rem; padding-top: 1.2rem; border-top: 1px solid var(--border);
    display: flex; flex-direction: column; align-items: center; gap: .7rem;
    font-size: .65rem; color: var(--muted); font-family: var(--mono); text-align: center;
  }
  .test-tg-btn {
    background: none; border: 1px solid var(--border2); color: var(--muted);
    font-family: var(--mono); font-size: .68rem; padding: .35rem 1rem;
    border-radius: 6px; cursor: pointer; transition: all .15s;
  }
  .test-tg-btn:hover { border-color: var(--accent); color: var(--accent); background: rgba(56,189,248,.06); }
  .test-tg-btn:disabled { opacity: .35; cursor: default; }

  /* ── Toast ── */
  #toast {
    position: fixed; bottom: 1.5rem; right: 1.5rem;
    background: #1e2535; border: 1px solid var(--border2);
    color: var(--text); font-family: var(--mono); font-size: .75rem;
    padding: .6rem 1.1rem; border-radius: 8px;
    box-shadow: 0 4px 24px rgba(0,0,0,.5);
    opacity: 0; transform: translateY(8px);
    transition: opacity .25s, transform .25s;
    pointer-events: none; z-index: 999; max-width: 280px;
  }
  #toast.show { opacity: 1; transform: translateY(0); }

  /* ── Confirm Modal ── */
  .modal-overlay {
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,.7); z-index: 900;
    align-items: center; justify-content: center;
  }
  .modal-overlay.open { display: flex; }
  .modal {
    background: var(--card); border: 1px solid var(--border2);
    border-radius: var(--radius); padding: 2rem 1.8rem;
    max-width: 400px; width: 92%;
    box-shadow: 0 12px 50px rgba(0,0,0,.7);
  }
  .modal h3 { font-size: 1rem; color: #e8f0fa; margin-bottom: .65rem; }
  .modal p  { font-size: .82rem; color: var(--muted); margin-bottom: 1.6rem; line-height: 1.6; }
  .modal-actions { display: flex; gap: .65rem; justify-content: flex-end; }
  .btn-cancel {
    background: none; border: 1px solid var(--border2); color: var(--muted);
    font-family: var(--sans); font-size: .82rem; padding: .45rem .95rem;
    border-radius: 6px; cursor: pointer; transition: all .12s;
  }
  .btn-cancel:hover { border-color: var(--text); color: var(--text); }
  .btn-delete {
    background: rgba(248,113,113,.15); border: 1px solid rgba(248,113,113,.35);
    color: var(--red); font-family: var(--sans); font-size: .82rem; font-weight: 700;
    padding: .45rem .95rem; border-radius: 6px; cursor: pointer; transition: all .12s;
  }
  .btn-delete:hover { background: rgba(248,113,113,.28); }

  @media (max-width: 600px) {
    .tl-row { grid-template-columns: 80px 1fr 32px; }
    .tl-col-end, .tl-col-dur { display: none; }
    .status-value { font-size: 1.4rem; }
    .chart-wrap   { height: 150px; }
  }
</style>
</head>
<body>
<div class="page">

  <!-- ── Header ── -->
  <header>
    <div>
      <div class="logo">Electricity<span>Monitor</span></div>
      <div class="header-badges">
        <span class="transport-badge">HTTP polling · {{ poll_interval }}s</span>
      </div>
    </div>
    <div style="display:flex;gap:.75rem;align-items:center;flex-wrap:wrap">
      <span class="refresh-badge" id="refresh-counter">refreshing in 10s</span>
      <a class="pico-link" href="{{ pico_url }}" target="_blank">&#8599; Pico Dashboard</a>
    </div>
  </header>

  <!-- ── Status Hero ── -->
  <div class="status-hero">
    <div>
      <div class="status-label">&#9679; Electricity Status</div>
      <div class="status-value {{ status_class }}" id="status-value">{{ status_text }}</div>
      <div class="status-meta" id="status-meta">{{ status_meta }}</div>
      <div class="status-meta" style="margin-top:.5rem">
        <span style="color:var(--muted)">Last outage: </span>
        <span id="last-offline" style="color:var(--text)">{{ last_offline }}</span>
      </div>
      <div class="status-meta" style="margin-top:.3rem">
        <span style="color:var(--muted)">Pico last seen: </span>
        <span id="last-seen" style="color:var(--text)">{{ last_seen }}</span>
      </div>
    </div>
    <div class="pulse {{ status_class }}" id="pulse-dot"></div>
  </div>

  <!-- ── Recent Outage Events ── -->
  <div class="section-head">Recent Outage Events (last 6)</div>
  <div class="timeline" id="timeline-wrap">
    <div class="tl-row tl-header" id="tl-header-row">
      <div>Type</div>
      <div>Started</div>
      <div class="tl-col-end">Ended</div>
      <div class="tl-col-dur">Duration</div>
      <div></div>
    </div>
    {% if recent %}
      {% for ev in recent %}
      <div class="tl-row" data-idx="{{ ev.idx }}">
        <div><span class="tl-badge {{ ev.type }}">{{ ev.type }}</span></div>
        <div class="tl-time">{{ ev.start }}</div>
        <div class="tl-time tl-col-end">{{ ev.end }}</div>
        <div class="tl-dur tl-col-dur">{{ ev.duration }}</div>
        <div><button class="tl-del-btn" onclick="askDelete({{ ev.idx }},'{{ ev.start }}','{{ ev.type }}')" title="Delete event">✕</button></div>
      </div>
      {% endfor %}
    {% else %}
      <div class="tl-empty" id="tl-empty">No outage events recorded yet.</div>
    {% endif %}
  </div>

  <!-- ── Downtime Stats ── -->
  <div class="section-head">Power Downtime Statistics (real outages only)</div>
  <div class="stats-grid">
    <div class="stat-card">
      <div class="period">Today</div>
      <div class="dur" id="day-dur">{{ day.human }}</div>
      <div class="pct">Downtime: <b>{{ day.percentage }}%</b></div>
    </div>
    <div class="stat-card">
      <div class="period">Last 7 Days</div>
      <div class="dur" id="week-dur">{{ week.human }}</div>
      <div class="pct">Downtime: <b>{{ week.percentage }}%</b></div>
    </div>
    <div class="stat-card">
      <div class="period">This Month</div>
      <div class="dur" id="month-dur">{{ month.human }}</div>
      <div class="pct">Downtime: <b>{{ month.percentage }}%</b></div>
    </div>
  </div>

  <!-- ── Outage Charts ── -->
  <div class="section-head">Outage Charts</div>

  <div class="chart-card">
    <div class="chart-title">
      Today — hourly breakdown
      <span class="chart-unit" id="unit-day">({{ chart_data.daily.unit }})</span>
    </div>
    <div class="chart-wrap"><canvas id="chartDay"></canvas></div>
    <div class="legend">
      <div class="legend-item"><div class="legend-dot real"></div> Real outage</div>
      <div class="legend-item"><div class="legend-dot sim"></div> Simulated</div>
    </div>
  </div>

  <div class="chart-card">
    <div class="chart-title">
      Last 7 Days — daily breakdown
      <span class="chart-unit" id="unit-week">({{ chart_data.weekly.unit }})</span>
    </div>
    <div class="chart-wrap"><canvas id="chartWeek"></canvas></div>
    <div class="legend">
      <div class="legend-item"><div class="legend-dot real"></div> Real outage</div>
      <div class="legend-item"><div class="legend-dot sim"></div> Simulated</div>
    </div>
  </div>

  <div class="chart-card">
    <div class="chart-title">
      This Month — daily breakdown
      <span class="chart-unit" id="unit-month">({{ chart_data.monthly.unit }})</span>
    </div>
    <div class="chart-wrap"><canvas id="chartMonth"></canvas></div>
    <div class="legend">
      <div class="legend-item"><div class="legend-dot real"></div> Real outage</div>
      <div class="legend-item"><div class="legend-dot sim"></div> Simulated</div>
    </div>
  </div>

  <!-- ── Footer ── -->
  <footer>
    <div>electricity_monitor.py &nbsp;·&nbsp; location: {{ device_id }} &nbsp;·&nbsp; auto-refresh every 10s</div>
    <button class="test-tg-btn" id="test-tg-btn" onclick="sendTestTelegram()">
      📨 Send test Telegram message
    </button>
  </footer>
</div>

<!-- ── Delete Confirm Modal ── -->
<div class="modal-overlay" id="modal-overlay">
  <div class="modal">
    <h3>⚠ Delete outage event?</h3>
    <p id="modal-body">This will permanently remove the event from the log file. This action cannot be undone.</p>
    <div class="modal-actions">
      <button class="btn-cancel" onclick="closeModal()">Cancel</button>
      <button class="btn-delete" id="modal-confirm-btn">Yes, delete it</button>
    </div>
  </div>
</div>

<!-- ── Toast ── -->
<div id="toast"></div>

<script>
// ── Chart setup ───────────────────────────────────────────────────────────────
Chart.defaults.color        = '#4a5568';
Chart.defaults.font.family  = "'JetBrains Mono', monospace";
Chart.defaults.font.size    = 10;

const RED   = 'rgba(248,113,113,0.85)';
const AMBER = 'rgba(251,191,36,0.75)';
const RED_B = 'rgba(248,113,113,1)';
const AMB_B = 'rgba(251,191,36,1)';

function buildTooltipLabel(unit) {
  return ctx => ` ${ctx.dataset.label}: ${ctx.parsed.y} ${unit}`;
}

function makeChart(id, data) {
  return new Chart(document.getElementById(id), {
    type: 'bar',
    data: {
      labels: data.labels,
      datasets: [
        { label: 'Real',      data: data.real,      backgroundColor: RED,   borderColor: RED_B, borderWidth: 1, borderRadius: 3 },
        { label: 'Simulated', data: data.simulated, backgroundColor: AMBER, borderColor: AMB_B, borderWidth: 1, borderRadius: 3 },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: buildTooltipLabel(data.unit) } }
      },
      scales: {
        x: { stacked: true, grid: { color: '#1e2530' }, ticks: { maxRotation: 45, minRotation: 0 } },
        y: {
          stacked: true, grid: { color: '#1e2530' }, beginAtZero: true,
          title: { display: true, text: data.unit, color: '#484f58', font: { size: 9 } }
        }
      }
    }
  });
}

const initData   = {{ chart_data_json|safe }};
const chartDay   = makeChart('chartDay',   initData.daily);
const chartWeek  = makeChart('chartWeek',  initData.weekly);
const chartMonth = makeChart('chartMonth', initData.monthly);

// ── Auto-refresh ──────────────────────────────────────────────────────────────
let countdown = 10;
const counterEl = document.getElementById('refresh-counter');

setInterval(() => {
  countdown--;
  counterEl.textContent = `refreshing in ${countdown}s`;
  if (countdown <= 0) { countdown = 10; fetchStatus(); }
}, 1000);

function updateChart(chart, unitEl, data) {
  chart.data.labels           = data.labels;
  chart.data.datasets[0].data = data.real;
  chart.data.datasets[1].data = data.simulated;
  chart.options.plugins.tooltip.callbacks.label = buildTooltipLabel(data.unit);
  chart.options.scales.y.title.text = data.unit;
  chart.update('none');
  if (unitEl) unitEl.textContent = `(${data.unit})`;
}

function fetchStatus() {
  fetch('/api/status')
    .then(r => r.json())
    .then(d => {
      document.getElementById('status-value').className   = 'status-value ' + d.status_class;
      document.getElementById('status-value').textContent = d.status_text;
      document.getElementById('pulse-dot').className      = 'pulse ' + d.status_class;
      document.getElementById('status-meta').textContent  = d.status_meta;
      document.getElementById('last-offline').textContent = d.last_offline;
      document.getElementById('last-seen').textContent    = d.last_seen;
      document.getElementById('day-dur').textContent      = d.day.human;
      document.getElementById('week-dur').textContent     = d.week.human;
      document.getElementById('month-dur').textContent    = d.month.human;
      updateChart(chartDay,   document.getElementById('unit-day'),   d.charts.daily);
      updateChart(chartWeek,  document.getElementById('unit-week'),  d.charts.weekly);
      updateChart(chartMonth, document.getElementById('unit-month'), d.charts.monthly);
      if (d.recent) renderTimeline(d.recent);
    })
    .catch(e => console.warn('Status fetch failed:', e));
}

// ── Timeline render ───────────────────────────────────────────────────────────
function renderTimeline(events) {
  const wrap   = document.getElementById('timeline-wrap');
  const header = document.getElementById('tl-header-row');
  wrap.innerHTML = '';
  wrap.appendChild(header);
  if (!events.length) {
    const empty = document.createElement('div');
    empty.id = 'tl-empty'; empty.className = 'tl-empty';
    empty.textContent = 'No outage events recorded yet.';
    wrap.appendChild(empty);
    return;
  }
  events.forEach(ev => {
    const row = document.createElement('div');
    row.className = 'tl-row'; row.dataset.idx = ev.idx;
    row.innerHTML =
      `<div><span class="tl-badge ${ev.type}">${ev.type}</span></div>` +
      `<div class="tl-time">${ev.start}</div>` +
      `<div class="tl-time tl-col-end">${ev.end}</div>` +
      `<div class="tl-dur tl-col-dur">${ev.duration}</div>` +
      `<div><button class="tl-del-btn" onclick="askDelete(${ev.idx},'${ev.start.replace(/'/g,"\\'")}','${ev.type}')" title="Delete">✕</button></div>`;
    wrap.appendChild(row);
  });
}

// ── Delete modal ──────────────────────────────────────────────────────────────
let pendingDeleteIdx = null;

function askDelete(idx, startStr, type) {
  pendingDeleteIdx = idx;
  document.getElementById('modal-body').textContent =
    `You are about to permanently delete the ${type.toLowerCase()} outage event starting at "${startStr}". This cannot be undone.`;
  document.getElementById('modal-overlay').classList.add('open');
  document.getElementById('modal-confirm-btn').onclick = confirmDelete;
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('open');
  pendingDeleteIdx = null;
}

// Close on overlay background click
document.getElementById('modal-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeModal();
});

function confirmDelete() {
  if (pendingDeleteIdx === null) return;
  const idx = pendingDeleteIdx;
  closeModal();
  fetch('/api/delete_event', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ idx })
  })
  .then(r => r.json())
  .then(d => {
    showToast(d.ok ? '✓ Event deleted successfully' : '✗ ' + (d.error || 'Delete failed'));
    if (d.ok) fetchStatus();
  })
  .catch(() => showToast('✗ Network error — please retry'));
}

// ── Test Telegram ─────────────────────────────────────────────────────────────
function sendTestTelegram() {
  const btn = document.getElementById('test-tg-btn');
  btn.disabled = true;
  btn.textContent = '📨 Sending…';
  fetch('/api/test_telegram', { method: 'POST' })
    .then(r => r.json())
    .then(d => showToast(d.ok ? '✓ Test message sent to Telegram' : '✗ ' + (d.error || 'Failed')))
    .catch(() => showToast('✗ Network error'))
    .finally(() => { btn.disabled = false; btn.textContent = '📨 Send test Telegram message'; });
}

// ── Toast ─────────────────────────────────────────────────────────────────────
let toastTimer = null;
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 3200);
}
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
#  FLASK HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _last_offline_str(entries) -> str:
    real = [e for e in entries if e["type"] == "REAL"]
    if not real:
        return "No outages recorded"
    latest = max(real, key=lambda e: e["start"])
    return latest["start"].strftime("%H:%M  %d %b %Y")


def _last_seen_str() -> str:
    with state_lock:
        ts = state["last_seen"]
    if ts == 0.0:
        return "never"
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S  %d %b %Y")


def _build_status_context():
    entries = parse_log_entries()
    with state_lock:
        curr  = state["current_status"]
        off_s = state["offline_since"]
        fails = state["consecutive_fails"]

    if curr == "online":
        sc, st, sm = "online", "ELECTRICITY ON", "Power is available"
    elif curr == "offline":
        sc, st = "offline", "ELECTRICITY OFF"
        if off_s:
            sm = f"Outage in progress · {_fmt_duration(int(time.time() - off_s))}"
        else:
            sm = "Power is unavailable"
        if fails:
            sm += f" ({fails} failed poll{'s' if fails != 1 else ''})"
    else:
        sc, st, sm = "unknown", "UNKNOWN", "Waiting for first poll…"

    return entries, sc, st, sm


# ─────────────────────────────────────────────────────────────────────────────
#  FLASK ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    entries, sc, st, sm = _build_status_context()
    chart_data = {
        "daily":   chart_daily(entries),
        "weekly":  chart_weekly(entries),
        "monthly": chart_monthly(entries),
    }
    return render_template_string(
        DASHBOARD_HTML,
        status_class    = sc,
        status_text     = st,
        status_meta     = sm,
        last_offline    = _last_offline_str(entries),
        last_seen       = _last_seen_str(),
        day             = daily_stats(entries),
        week            = weekly_stats(entries),
        month           = monthly_stats(entries),
        chart_data      = chart_data,
        chart_data_json = json.dumps(chart_data),
        recent          = recent_events(entries, 6),
        device_id       = DEVICE_ID,
        pico_url        = CONFIG["pico_url"],
        poll_interval   = CONFIG["poll_interval"],
    )


@app.route("/api/status")
def api_status():
    entries, sc, st, sm = _build_status_context()
    return jsonify({
        "status_class": sc,
        "status_text":  st,
        "status_meta":  sm,
        "last_offline": _last_offline_str(entries),
        "last_seen":    _last_seen_str(),
        "day":          daily_stats(entries),
        "week":         weekly_stats(entries),
        "month":        monthly_stats(entries),
        "recent":       recent_events(entries, 6),
        "charts": {
            "daily":   chart_daily(entries),
            "weekly":  chart_weekly(entries),
            "monthly": chart_monthly(entries),
        },
    })


@app.route("/api/delete_event", methods=["POST"])
def api_delete_event():
    data = request.get_json(silent=True) or {}
    idx  = data.get("idx")
    if idx is None:
        return jsonify({"ok": False, "error": "Missing idx parameter"}), 400
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "idx must be an integer"}), 400
    ok = delete_log_entry_by_idx(idx)
    if not ok:
        return jsonify({"ok": False, "error": "Event not found (may already have been deleted)"}), 404
    return jsonify({"ok": True})


@app.route("/api/test_telegram", methods=["POST"])
def api_test_telegram():
    now_str = datetime.now().strftime("%H:%M:%S on %d %b %Y")
    try:
        send_telegram(
            "🔔 <b>Test Message</b>\n"
            f"Electricity Monitor is working correctly.\n"
            f"Sent at <b>{now_str}</b>\n"
            f"Location ID: <code>{DEVICE_ID}</code>"
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
#  STARTUP & MAIN
# ─────────────────────────────────────────────────────────────────────────────

def restore_state():
    entries = parse_log_entries()
    log.info(f"Loaded {len(entries)} outage log entries from {LOG_FILE}")


def main():
    log.info("=" * 60)
    log.info("Electricity Uptime Monitor starting")
    log.info(f"  Location ID:   {DEVICE_ID}")
    log.info(f"  Pico URL:      {PICO_STATUS_URL}")
    log.info(f"  Poll interval: {CONFIG['poll_interval']}s")
    log.info(f"  Log file:      {LOG_FILE}")
    log.info(f"  Web UI:        http://{CONFIG['web_host']}:{CONFIG['web_port']}")
    log.info("=" * 60)

    restore_state()

    t_poll = threading.Thread(target=poll_loop, daemon=True, name="http-poll")
    t_poll.start()

    app.run(
        host         = CONFIG["web_host"],
        port         = CONFIG["web_port"],
        debug        = False,
        use_reloader = False,
    )


if __name__ == "__main__":
    main()