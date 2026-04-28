"""
power_monitor.py
================
Python 3 server that monitors a Raspberry Pi Pico 2W power status via HTTP polling,
sends Telegram alerts, logs events to a text file, and serves a web dashboard.

Dependencies (install via pip):
    pip install requests python-telegram-bot==13.13 flask

Usage:
    python power_monitor.py

Configuration: edit the CONFIG block below, or use config.ini.
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
from flask import Flask, jsonify, render_template_string

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION  (loaded from config.ini)
#
#  config.ini example:
#
#  [pico]
#  url            = http://192.168.1.42     ; Pico's IP (printed on its serial console)
#  device_id      = pico_01
#
#  [telegram]
#  token          = YOUR_BOT_TOKEN
#  chat_id        = YOUR_CHAT_ID
#
#  [monitor]
#  log_file       = power_log.txt
#  poll_interval  = 5                       ; seconds between HTTP polls
#  offline_timeout = 15                     ; seconds of no reply → declare offline
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
    # Pico HTTP
    "pico_url":         _cfg.get("pico",    "url").rstrip("/"),
    "device_id":        _cfg.get("pico",    "device_id"),

    # Telegram
    "telegram_token":   _cfg.get("telegram", "token"),
    "telegram_chat_id": _cfg.get("telegram", "chat_id"),

    # Monitor behaviour
    "log_file":         _cfg.get("monitor",    "log_file",        fallback="power_log.txt"),
    "poll_interval":    _cfg.getint("monitor", "poll_interval",   fallback=5),
    "offline_timeout":  _cfg.getint("monitor", "offline_timeout", fallback=15),
    "http_timeout":     _cfg.getint("monitor", "http_timeout",    fallback=4),

    # Flask web server
    "web_host":         _cfg.get("web",    "host", fallback="0.0.0.0"),
    "web_port":         _cfg.getint("web", "port", fallback=8080),
}

DEVICE_ID = CONFIG["device_id"]
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
    "current_status":   "unknown",  # "online" | "offline" | "unknown"
    "last_seen":        0.0,        # epoch of last successful HTTP response
    "offline_since":    None,       # epoch when outage started, or None
    "is_simulated":     False,      # True when Pico reports manual override
    "alert_sent":       False,      # True after "Power Lost" alert sent
    "consecutive_fails": 0,         # failed poll count (for hysteresis)
}

state_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
#  LOG FILE  (power_log.txt)
#  Format per line:  TYPE|START_ISO|END_ISO|DURATION_SECONDS
#  TYPE: REAL | SIMULATED
# ─────────────────────────────────────────────────────────────────────────────
LOG_FILE = CONFIG["log_file"]


def append_log_entry(evt_type: str, start: float, end: float):
    duration = int(end - start)
    start_s  = datetime.fromtimestamp(start).strftime("%Y-%m-%dT%H:%M:%S")
    end_s    = datetime.fromtimestamp(end).strftime("%Y-%m-%dT%H:%M:%S")
    line     = f"{evt_type}|{start_s}|{end_s}|{duration}\n"
    with open(LOG_FILE, "a") as f:
        f.write(line)
    log.info(f"Logged: {line.strip()}")


def parse_log_entries():
    """Return list of dicts from the log file. Returns [] if missing."""
    entries = []
    if not os.path.exists(LOG_FILE):
        return entries
    with open(LOG_FILE) as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            parts = raw.split("|")
            if len(parts) != 4:
                continue
            try:
                entries.append({
                    "type":     parts[0],
                    "start":    datetime.fromisoformat(parts[1]),
                    "end":      datetime.fromisoformat(parts[2]),
                    "duration": int(parts[3]),
                })
            except Exception:
                pass
    return entries


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


def _fmt_duration(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}h {m:02d}m {s:02d}s"


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
    now   = datetime.now()
    since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    secs  = _total_seconds(_filter_real(entries), since, now)
    window = (now - since).total_seconds()
    return {"seconds": secs, "human": _fmt_duration(secs),
            "percentage": round(secs / window * 100, 2) if window > 0 else 0}


# ─────────────────────────────────────────────────────────────────────────────
#  CHART DATA
# ─────────────────────────────────────────────────────────────────────────────

def chart_daily(entries):
    now  = datetime.now()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    real_b = [0] * 24
    sim_b  = [0] * 24
    labels = [f"{h:02d}:00" for h in range(24)]
    for e in entries:
        buckets = real_b if e["type"] == "REAL" else sim_b
        for h in range(24):
            bs = day0 + timedelta(hours=h)
            be = bs + timedelta(hours=1)
            s  = max(e["start"], bs)
            en = min(e["end"],   be)
            if en > s:
                buckets[h] += int((en - s).total_seconds())
    return {"labels": labels, "real": real_b, "simulated": sim_b}


def chart_weekly(entries):
    now    = datetime.now()
    labels, real_b, sim_b = [], [], []
    for d in range(6, -1, -1):
        ds = (now - timedelta(days=d)).replace(hour=0, minute=0, second=0, microsecond=0)
        de = ds + timedelta(days=1)
        labels.append(ds.strftime("%a %d"))
        r = s = 0
        for e in entries:
            ov_s  = max(e["start"], ds)
            ov_e  = min(e["end"],   de)
            if ov_e > ov_s:
                dur = int((ov_e - ov_s).total_seconds())
                if e["type"] == "REAL":
                    r += dur
                else:
                    s += dur
        real_b.append(r)
        sim_b.append(s)
    return {"labels": labels, "real": real_b, "simulated": sim_b}


def chart_monthly(entries):
    now     = datetime.now()
    month_s = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    labels, real_b, sim_b = [], [], []
    day = month_s
    while day.month == now.month and day <= now:
        next_day = day + timedelta(days=1)
        labels.append(day.strftime("%-d"))
        r = s = 0
        for e in entries:
            ov_s  = max(e["start"], day)
            ov_e  = min(e["end"],   next_day)
            if ov_e > ov_s:
                dur = int((ov_e - ov_s).total_seconds())
                if e["type"] == "REAL":
                    r += dur
                else:
                    s += dur
        real_b.append(r)
        sim_b.append(s)
        day = next_day
    return {"labels": labels, "real": real_b, "simulated": sim_b}


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
        log.info(f"Telegram sent: {text[:80]}")
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  POWER EVENT HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

def handle_went_offline(simulated: bool):
    with state_lock:
        if state["offline_since"] is not None:
            return                          # already tracking an outage
        state["offline_since"] = time.time()
        state["is_simulated"]  = simulated
        state["alert_sent"]    = False

    tag = " [SIMULATED]" if simulated else ""
    log.info(f"Power OFFLINE{tag}")

    if not simulated:
        send_telegram("⚠️ <b>Power Lost</b>\nYour monitored device has gone offline.")


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

    # Ignore very short blips (transient HTTP failures during Pico WiFi reconnect)
    if duration < 10:
        log.info(f"Ignoring sub-10s offline→online transition ({duration}s)")
        return

    evt_type = "SIMULATED" if simulated else "REAL"
    append_log_entry(evt_type, offline_since, now)

    if simulated:
        log.info(f"Simulated outage ended ({_fmt_duration(duration)})")
        return

    entries   = parse_log_entries()
    day_stats = daily_stats(entries)

    msg = (
        "✅ <b>Power Restored</b>\n"
        f"Downtime this session: <b>{_fmt_duration(duration)}</b>\n"
        f"Total downtime today:  <b>{day_stats['human']}</b> ({day_stats['percentage']}%)"
    )
    send_telegram(msg)
    log.info(f"Power ONLINE – session downtime: {_fmt_duration(duration)}")


# ─────────────────────────────────────────────────────────────────────────────
#  HTTP POLL LOOP
#  Replaces MQTT entirely.
#
#  The Pico exposes GET /status → plain-text "online" or "offline".
#  We poll it every poll_interval seconds.
#
#  Transition logic:
#    • Response body == "offline"  → simulated override active on the Pico
#    • HTTP success + body "online" → device is up and running normally
#    • Request exception / non-200  → device is unreachable (real outage)
#
#  Hysteresis: we require 3 consecutive failures before declaring offline
#  to avoid false positives from momentary WiFi reconnects on the Pico.
# ─────────────────────────────────────────────────────────────────────────────
OFFLINE_HYSTERESIS = 3   # consecutive failures before flipping to offline


def poll_pico():
    """Single poll of the Pico /status endpoint. Returns ("online"|"offline"|"unreachable", simulated)."""
    try:
        r = requests.get(
            PICO_STATUS_URL,
            timeout=CONFIG["http_timeout"],
        )
        if r.status_code == 200:
            body = r.text.strip().lower()
            if body == "offline":
                return "offline", True      # Pico told us it's simulating offline
            return "online", False          # Pico is up and happy
        # Non-200 counts as unreachable
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
    """Background thread: poll the Pico and drive state transitions."""
    global state
    log.info(f"Poll loop started → {PICO_STATUS_URL} every {CONFIG['poll_interval']}s")

    while True:
        result, simulated = poll_pico()
        now = time.time()

        with state_lock:
            prev_status = state["current_status"]
            fails       = state["consecutive_fails"]

        if result == "online":
            # ── Device replied "online" ──────────────────────────────────────
            with state_lock:
                state["last_seen"]          = now
                state["consecutive_fails"]  = 0
                state["current_status"]     = "online"

            if prev_status != "online":
                handle_came_online()

        elif result == "offline":
            # ── Device replied "offline" (manual override on Pico) ───────────
            with state_lock:
                state["last_seen"]         = now      # device IS reachable
                state["consecutive_fails"] = 0
                state["current_status"]    = "offline"

            if prev_status != "offline":
                handle_went_offline(simulated=True)

        else:
            # ── Unreachable ─────────────────────────────────────────────────
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
  <title>Power Monitor — {{ device_id }}</title>
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
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    font-size: 14px;
    min-height: 100vh;
    padding: 1.5rem 1rem 3rem;
  }

  .page { max-width: 960px; margin: 0 auto; }

  /* ── Header ── */
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 1rem;
    margin-bottom: 2rem;
    padding-bottom: 1.2rem;
    border-bottom: 1px solid var(--border);
  }
  .logo { font-size: 1.25rem; font-weight: 800; color: #e8f0fa; letter-spacing: -.02em; }
  .logo span { color: var(--accent); }
  .pico-link {
    font-family: var(--mono);
    font-size: .72rem;
    color: var(--accent);
    text-decoration: none;
    border: 1px solid var(--border2);
    padding: .35rem .75rem;
    border-radius: 6px;
    transition: background .15s;
  }
  .pico-link:hover { background: rgba(56,189,248,.1); }

  /* ── Status hero ── */
  .status-hero {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.6rem 1.8rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 1rem;
    margin-bottom: 1.5rem;
    position: relative;
    overflow: hidden;
  }
  .status-hero::before {
    content: '';
    position: absolute; inset: 0;
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
  .pulse.online  { background: var(--green); box-shadow: 0 0 0 4px rgba(52,211,153,.18); animation: pulse-g 2s infinite; }
  .pulse.offline { background: var(--red);   box-shadow: 0 0 0 4px rgba(248,113,113,.18); animation: pulse-r 2s infinite; }
  .pulse.unknown { background: var(--muted); }

  @keyframes pulse-g {
    0%   { box-shadow: 0 0 0 0   rgba(52,211,153,.4); }
    70%  { box-shadow: 0 0 0 8px rgba(52,211,153,0); }
    100% { box-shadow: 0 0 0 0   rgba(52,211,153,0); }
  }
  @keyframes pulse-r {
    0%   { box-shadow: 0 0 0 0   rgba(248,113,113,.4); }
    70%  { box-shadow: 0 0 0 8px rgba(248,113,113,0); }
    100% { box-shadow: 0 0 0 0   rgba(248,113,113,0); }
  }

  /* ── Stats grid ── */
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 1rem;
    margin-bottom: 1.5rem;
  }
  .stat-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.2rem 1.4rem;
  }
  .stat-card .period { font-size: .62rem; letter-spacing: .12em; text-transform: uppercase; color: var(--muted); margin-bottom: .5rem; }
  .stat-card .dur    { font-family: var(--mono); font-size: 1.1rem; font-weight: 600; color: #e8f0fa; margin-bottom: .25rem; }
  .stat-card .pct    { font-size: .75rem; color: var(--muted); }
  .stat-card .pct b  { color: var(--red); }

  /* ── Section heading ── */
  .section-head {
    font-size: .65rem; letter-spacing: .14em; text-transform: uppercase;
    color: var(--muted); margin-bottom: .8rem; margin-top: 1.8rem;
  }

  /* ── Chart cards ── */
  .chart-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.4rem 1.4rem 1.2rem;
    margin-bottom: 1rem;
  }
  .chart-title { font-size: .78rem; font-weight: 600; color: #c8d6e8; margin-bottom: 1rem; }
  .chart-wrap  { position: relative; height: 180px; }

  /* ── Legend ── */
  .legend { display: flex; gap: 1.2rem; margin-top: .75rem; flex-wrap: wrap; }
  .legend-item { display: flex; align-items: center; gap: .4rem; font-size: .68rem; color: var(--muted); }
  .legend-dot  { width: 8px; height: 8px; border-radius: 2px; flex-shrink: 0; }
  .legend-dot.real { background: var(--red); }
  .legend-dot.sim  { background: var(--amber); }

  /* ── Footer ── */
  footer { margin-top: 2.5rem; text-align: center; font-size: .65rem; color: var(--muted); font-family: var(--mono); }

  /* ── Refresh badge ── */
  .refresh-badge {
    font-family: var(--mono); font-size: .62rem; color: var(--muted);
    border: 1px solid var(--border); padding: .2rem .5rem; border-radius: 4px;
  }

  /* ── Connection method badge ── */
  .transport-badge {
    font-family: var(--mono); font-size: .62rem;
    background: rgba(56,189,248,.08); color: var(--accent);
    border: 1px solid rgba(56,189,248,.2); padding: .2rem .6rem; border-radius: 4px;
  }

  @media (max-width: 480px) {
    .status-value { font-size: 1.4rem; }
    .chart-wrap   { height: 150px; }
  }
</style>
</head>
<body>
<div class="page">

  <header>
    <div>
      <div class="logo">Power<span>Monitor</span></div>
      <span class="transport-badge" style="margin-top:.35rem;display:inline-block">HTTP polling · {{ poll_interval }}s</span>
    </div>
    <div style="display:flex;gap:.75rem;align-items:center;flex-wrap:wrap">
      <span class="refresh-badge" id="refresh-counter">refreshing in 10s</span>
      <a class="pico-link" href="{{ pico_url }}" target="_blank">&#8599; Pico Management</a>
    </div>
  </header>

  <!-- Status hero -->
  <div class="status-hero">
    <div>
      <div class="status-label">&#9679; Device Status</div>
      <div class="status-value {{ status_class }}" id="status-value">{{ status_text }}</div>
      <div class="status-meta" id="status-meta">{{ status_meta }}</div>
      <div class="status-meta" style="margin-top:.5rem">
        <span style="color:var(--muted)">Last offline: </span>
        <span id="last-offline" style="color:var(--text)">{{ last_offline }}</span>
      </div>
      <div class="status-meta" style="margin-top:.3rem">
        <span style="color:var(--muted)">Last seen: </span>
        <span id="last-seen" style="color:var(--text)">{{ last_seen }}</span>
      </div>
    </div>
    <div class="pulse {{ status_class }}" id="pulse-dot"></div>
  </div>

  <!-- Stats -->
  <div class="section-head">Downtime Statistics (REAL outages only)</div>
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

  <!-- Charts -->
  <div class="section-head">Downtime Charts</div>

  <div class="chart-card">
    <div class="chart-title">Today — hourly breakdown (seconds offline)</div>
    <div class="chart-wrap"><canvas id="chartDay"></canvas></div>
    <div class="legend">
      <div class="legend-item"><div class="legend-dot real"></div> Real outage</div>
      <div class="legend-item"><div class="legend-dot sim"></div> Simulated</div>
    </div>
  </div>

  <div class="chart-card">
    <div class="chart-title">Last 7 Days — daily breakdown (seconds offline)</div>
    <div class="chart-wrap"><canvas id="chartWeek"></canvas></div>
    <div class="legend">
      <div class="legend-item"><div class="legend-dot real"></div> Real outage</div>
      <div class="legend-item"><div class="legend-dot sim"></div> Simulated</div>
    </div>
  </div>

  <div class="chart-card">
    <div class="chart-title">This Month — daily breakdown (seconds offline)</div>
    <div class="chart-wrap"><canvas id="chartMonth"></canvas></div>
    <div class="legend">
      <div class="legend-item"><div class="legend-dot real"></div> Real outage</div>
      <div class="legend-item"><div class="legend-dot sim"></div> Simulated</div>
    </div>
  </div>

  <footer>power_monitor.py &nbsp;·&nbsp; device: {{ device_id }} &nbsp;·&nbsp; auto-refresh every 10 s</footer>
</div>

<script>
Chart.defaults.color = '#4a5568';
Chart.defaults.font.family = "'JetBrains Mono', monospace";
Chart.defaults.font.size   = 10;

const RED   = 'rgba(248,113,113,0.85)';
const AMBER = 'rgba(251,191,36,0.75)';
const RED_B = 'rgba(248,113,113,1)';
const AMB_B = 'rgba(251,191,36,1)';

function makeChart(id, labels, real, sim) {
  return new Chart(document.getElementById(id), {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Real',      data: real, backgroundColor: RED,   borderColor: RED_B, borderWidth: 1, borderRadius: 3 },
        { label: 'Simulated', data: sim,  backgroundColor: AMBER, borderColor: AMB_B, borderWidth: 1, borderRadius: 3 },
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { stacked: true, grid: { color: '#1e2530' }, ticks: { maxRotation: 45, minRotation: 0 } },
        y: { stacked: true, grid: { color: '#1e2530' }, beginAtZero: true,
             ticks: { callback: v => v >= 3600 ? (v/3600).toFixed(1)+'h' : v >= 60 ? (v/60).toFixed(0)+'m' : v+'s' } }
      }
    }
  });
}

const initData   = {{ chart_data_json|safe }};
const chartDay   = makeChart('chartDay',   initData.daily.labels,   initData.daily.real,   initData.daily.simulated);
const chartWeek  = makeChart('chartWeek',  initData.weekly.labels,  initData.weekly.real,  initData.weekly.simulated);
const chartMonth = makeChart('chartMonth', initData.monthly.labels, initData.monthly.real, initData.monthly.simulated);

// ── Auto-refresh via /api/status ──────────────────────────────────────────
let countdown = 10;
const counterEl = document.getElementById('refresh-counter');

setInterval(function() {
  countdown--;
  counterEl.textContent = `refreshing in ${countdown}s`;
  if (countdown <= 0) { countdown = 10; fetchStatus(); }
}, 1000);

function updateChart(chart, data) {
  chart.data.labels           = data.labels;
  chart.data.datasets[0].data = data.real;
  chart.data.datasets[1].data = data.simulated;
  chart.update('none');
}

function fetchStatus() {
  fetch('/api/status')
    .then(r => r.json())
    .then(d => {
      const sv = document.getElementById('status-value');
      const pd = document.getElementById('pulse-dot');
      const sm = document.getElementById('status-meta');
      sv.className   = 'status-value ' + d.status_class;
      sv.textContent = d.status_text;
      pd.className   = 'pulse '        + d.status_class;
      sm.textContent = d.status_meta;
      document.getElementById('last-offline').textContent = d.last_offline;
      document.getElementById('last-seen').textContent    = d.last_seen;

      document.getElementById('day-dur').textContent   = d.day.human;
      document.getElementById('week-dur').textContent  = d.week.human;
      document.getElementById('month-dur').textContent = d.month.human;

      updateChart(chartDay,   d.charts.daily);
      updateChart(chartWeek,  d.charts.weekly);
      updateChart(chartMonth, d.charts.monthly);
    })
    .catch(e => console.warn('Status fetch failed:', e));
}
</script>
</body>
</html>
"""


def _last_offline_str(entries) -> str:
    real = [e for e in entries if e["type"] == "REAL"]
    if not real:
        return "---"
    latest = max(real, key=lambda e: e["start"])
    return latest["start"].strftime("%H:%M  %d-%m-%Y")


def _last_seen_str() -> str:
    with state_lock:
        ts = state["last_seen"]
    if ts == 0.0:
        return "never"
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S  %d-%m-%Y")


def _build_status_context():
    entries = parse_log_entries()
    with state_lock:
        curr  = state["current_status"]
        off_s = state["offline_since"]
        fails = state["consecutive_fails"]

    if curr == "online":
        sc, st, sm = "online", "ONLINE", "Device is reachable"
    elif curr == "offline":
        sc, st = "offline", "OFFLINE"
        if off_s:
            sm = f"Down for {_fmt_duration(int(time.time() - off_s))}"
        else:
            sm = "Device is unreachable"
        if fails:
            sm += f" ({fails} failed poll{'s' if fails != 1 else ''})"
    else:
        sc, st, sm = "unknown", "UNKNOWN", "Waiting for first poll…"

    return entries, sc, st, sm


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
        chart_data_json = json.dumps(chart_data),
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
        "charts": {
            "daily":   chart_daily(entries),
            "weekly":  chart_weekly(entries),
            "monthly": chart_monthly(entries),
        },
    })


# ─────────────────────────────────────────────────────────────────────────────
#  STARTUP
# ─────────────────────────────────────────────────────────────────────────────

def restore_state():
    entries = parse_log_entries()
    log.info(f"Loaded {len(entries)} log entries from {LOG_FILE}")


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("Power Monitor (HTTP mode) starting up")
    log.info(f"  Device:       {DEVICE_ID}")
    log.info(f"  Pico URL:     {PICO_STATUS_URL}")
    log.info(f"  Poll interval:{CONFIG['poll_interval']}s")
    log.info(f"  Offline after:{CONFIG['offline_timeout']}s silence")
    log.info(f"  Log:          {LOG_FILE}")
    log.info(f"  Web UI:       http://{CONFIG['web_host']}:{CONFIG['web_port']}")
    log.info("=" * 60)

    restore_state()

    # HTTP poll thread
    t_poll = threading.Thread(target=poll_loop, daemon=True, name="http-poll")
    t_poll.start()

    # Flask (runs on main thread)
    app.run(
        host         = CONFIG["web_host"],
        port         = CONFIG["web_port"],
        debug        = False,
        use_reloader = False,
    )


if __name__ == "__main__":
    main()