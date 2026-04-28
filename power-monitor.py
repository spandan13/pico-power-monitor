"""
power_monitor.py
================
Python 3 server that monitors home electricity uptime via a Raspberry Pi Pico 2W
(HTTP polling), sends Telegram alerts, logs outage events to a text file,
and serves a web dashboard.

Dependencies:
    pip install requests python-telegram-bot==13.13 flask

Configuration: config.ini  (see template comment below)

LOG FILE FORMAT (power_log.txt)
  Each line: TYPE|START_ISO|END_ISO|DURATION_SECONDS|STATUS
    TYPE   : REAL | SIMULATED
    END_ISO: actual timestamp when closed, or "-" when still open
    DURATION: integer seconds, or 0 when still open
    STATUS : OPEN | CLOSED

  When an outage starts  → a line is written immediately with STATUS=OPEN
  When an outage ends    → that line is found and rewritten with STATUS=CLOSED
  On restart             → any OPEN line is used to restore in-memory state
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
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────
_cfg = configparser.ConfigParser()
if not _cfg.read("config.ini"):
    sys.exit("ERROR: config.ini not found.")

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
#  LOGGING (console)
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("monitor")

# ─────────────────────────────────────────────────────────────────────────────
#  IN-MEMORY STATE
# ─────────────────────────────────────────────────────────────────────────────
state = {
    "current_status":    "unknown",
    "last_seen":         0.0,
    "offline_since":     None,      # epoch float – set when outage begins
    "open_line_idx":     None,      # line number of the current OPEN entry
    "is_simulated":      False,
    "alert_sent":        False,
    "consecutive_fails": 0,
}
state_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
#  LOG FILE  (thread-safe read/write)
# ─────────────────────────────────────────────────────────────────────────────
LOG_FILE = CONFIG["log_file"]
log_lock = threading.Lock()

ISO_FMT = "%Y-%m-%dT%H:%M:%S"


def _now_iso() -> str:
    return datetime.now().strftime(ISO_FMT)


def _make_line(evt_type: str, start_iso: str, end_iso: str,
               duration: int, status: str) -> str:
    return f"{evt_type}|{start_iso}|{end_iso}|{duration}|{status}\n"


# ── Low-level file helpers ────────────────────────────────────────────────────

def _read_lines() -> list:
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE) as f:
        return f.readlines()


def _write_lines(lines: list):
    with open(LOG_FILE, "w") as f:
        f.writelines(lines)


# ── High-level log operations ─────────────────────────────────────────────────

def log_open_entry(evt_type: str, start_ts: float) -> int:
    """
    Append an OPEN entry immediately when an outage begins.
    Returns the line index (0-based) of the new entry.
    """
    start_iso = datetime.fromtimestamp(start_ts).strftime(ISO_FMT)
    line      = _make_line(evt_type, start_iso, "-", 0, "OPEN")
    with log_lock:
        lines = _read_lines()
        idx   = len(lines)
        lines.append(line)
        _write_lines(lines)
    log.info(f"Open entry written at line {idx}: {line.strip()}")
    return idx


def close_open_entry(line_idx: int, end_ts: float):
    """
    Overwrite the OPEN line at line_idx with the completed CLOSED entry.
    """
    with log_lock:
        lines = _read_lines()
        if line_idx < 0 or line_idx >= len(lines):
            log.warning(f"close_open_entry: line {line_idx} not found")
            return
        parts = lines[line_idx].strip().split("|")
        if len(parts) < 5:
            log.warning(f"close_open_entry: malformed line {line_idx}")
            return
        start_dt  = datetime.fromisoformat(parts[1])
        end_dt    = datetime.fromtimestamp(end_ts)
        duration  = int((end_dt - start_dt).total_seconds())
        lines[line_idx] = _make_line(
            parts[0],
            parts[1],
            end_dt.strftime(ISO_FMT),
            max(duration, 0),
            "CLOSED"
        )
        _write_lines(lines)
    log.info(f"Closed entry at line {line_idx}, duration {duration}s")


def parse_log_entries() -> list:
    """
    Parse all log lines. Returns list of dicts, newest-last.
    OPEN entries have end=None and duration=None.
    """
    entries = []
    with log_lock:
        lines = _read_lines()
    for idx, raw in enumerate(lines):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split("|")
        # Support both old 4-field format (all CLOSED) and new 5-field format
        if len(parts) == 4:
            parts.append("CLOSED")
        if len(parts) != 5:
            continue
        try:
            is_open = parts[4].upper() == "OPEN"
            entries.append({
                "idx":      idx,
                "type":     parts[0],
                "start":    datetime.fromisoformat(parts[1]),
                "end":      None if is_open else datetime.fromisoformat(parts[2]),
                "duration": None if is_open else int(parts[3]),
                "open":     is_open,
            })
        except Exception:
            pass
    return entries


def delete_log_entry_by_idx(line_idx: int) -> bool:
    if not os.path.exists(LOG_FILE):
        return False
    with log_lock:
        lines = _read_lines()
        if line_idx < 0 or line_idx >= len(lines):
            return False
        del lines[line_idx]
        _write_lines(lines)
    log.info(f"Deleted log entry at line {line_idx}")
    return True


def update_log_entry(line_idx: int, evt_type: str, start_iso: str,
                     end_iso: str, status: str) -> bool:
    """Replace a line in-place with updated values. Recalculates duration."""
    with log_lock:
        lines = _read_lines()
        if line_idx < 0 or line_idx >= len(lines):
            return False
        if status == "OPEN" or end_iso == "-":
            duration = 0
            end_iso  = "-"
            status   = "OPEN"
        else:
            try:
                s        = datetime.fromisoformat(start_iso)
                e        = datetime.fromisoformat(end_iso)
                duration = max(int((e - s).total_seconds()), 0)
            except Exception:
                return False
        lines[line_idx] = _make_line(evt_type, start_iso, end_iso, duration, status)
        _write_lines(lines)
    log.info(f"Updated log entry at line {line_idx}")
    return True


def add_log_entry(evt_type: str, start_iso: str, end_iso: str) -> bool:
    """Append a brand-new manually-created entry."""
    try:
        s = datetime.fromisoformat(start_iso)
        if end_iso and end_iso != "-":
            e        = datetime.fromisoformat(end_iso)
            duration = max(int((e - s).total_seconds()), 0)
            status   = "CLOSED"
        else:
            duration = 0
            end_iso  = "-"
            status   = "OPEN"
    except Exception:
        return False
    line = _make_line(evt_type, start_iso, end_iso, duration, status)
    with log_lock:
        lines = _read_lines()
        lines.append(line)
        _write_lines(lines)
    log.info(f"Manually added entry: {line.strip()}")
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  DURATION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    if seconds < 3600:
        mins    = round(seconds / 60, 1)
        display = int(mins) if mins == int(mins) else mins
        return f"{display} minute{'s' if display != 1 else ''}"
    hrs     = round(seconds / 3600, 2)
    display = int(hrs) if hrs == int(hrs) else hrs
    return f"{display} hour{'s' if display != 1 else ''}"


def _duration_unit(seconds: int) -> dict:
    if seconds < 60:
        return {"value": seconds,                 "unit": "seconds"}
    if seconds < 3600:
        return {"value": round(seconds / 60, 1),  "unit": "minutes"}
    return     {"value": round(seconds / 3600, 2), "unit": "hours"}


# ─────────────────────────────────────────────────────────────────────────────
#  STARTUP: RESTORE STATE FROM OPEN ENTRIES
# ─────────────────────────────────────────────────────────────────────────────

def restore_state_from_log():
    """
    On startup, scan for any OPEN entry and re-hydrate in-memory state.
    This means a server restart during an outage does NOT lose the start time.
    """
    entries = parse_log_entries()
    log.info(f"Loaded {len(entries)} log entries from {LOG_FILE}")
    open_entries = [e for e in entries if e["open"]]
    if not open_entries:
        return
    # Use the most recent OPEN entry (should only ever be one)
    oe = max(open_entries, key=lambda e: e["start"])
    offline_ts = oe["start"].timestamp()
    is_sim     = (oe["type"] == "SIMULATED")
    with state_lock:
        state["offline_since"]  = offline_ts
        state["open_line_idx"]  = oe["idx"]
        state["is_simulated"]   = is_sim
        state["current_status"] = "offline"
    log.info(
        f"Resumed {'simulated ' if is_sim else ''}outage from log "
        f"(started {oe['start'].strftime(ISO_FMT)}, line {oe['idx']})"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  DOWNTIME STATS  (closed entries only)
# ─────────────────────────────────────────────────────────────────────────────

def _closed_real(entries):
    return [e for e in entries if e["type"] == "REAL" and not e["open"]]


def _total_seconds(entries, since: datetime, until: datetime) -> int:
    total = 0
    for e in entries:
        if e["end"] is None:
            continue
        s  = max(e["start"], since)
        en = min(e["end"],   until)
        if en > s:
            total += int((en - s).total_seconds())
    return total


def daily_stats(entries):
    now   = datetime.now()
    since = now.replace(hour=0, minute=0, second=0, microsecond=0)
    secs  = _total_seconds(_closed_real(entries), since, now)
    return {"seconds": secs, "human": _fmt_duration(secs),
            "percentage": round(secs / 86400 * 100, 2)}


def weekly_stats(entries):
    now   = datetime.now()
    since = now - timedelta(days=7)
    secs  = _total_seconds(_closed_real(entries), since, now)
    return {"seconds": secs, "human": _fmt_duration(secs),
            "percentage": round(secs / (7 * 86400) * 100, 2)}


def monthly_stats(entries):
    now    = datetime.now()
    since  = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    secs   = _total_seconds(_closed_real(entries), since, now)
    window = (now - since).total_seconds()
    return {"seconds": secs, "human": _fmt_duration(secs),
            "percentage": round(secs / window * 100, 2) if window > 0 else 0}


# ─────────────────────────────────────────────────────────────────────────────
#  CHART DATA
# ─────────────────────────────────────────────────────────────────────────────

def _normalise(raw: list, divisor: float) -> list:
    return [round(v / divisor, 2) for v in raw]


def _chart_unit_divisor(raw_r, raw_s):
    mx      = max(max(raw_r, default=0), max(raw_s, default=0))
    ud      = _duration_unit(mx)
    divisor = {"seconds": 1, "minutes": 60, "hours": 3600}[ud["unit"]]
    return ud["unit"], divisor


def chart_daily(entries):
    now    = datetime.now()
    day0   = now.replace(hour=0, minute=0, second=0, microsecond=0)
    raw_r  = [0] * 24
    raw_s  = [0] * 24
    labels = [f"{h:02d}:00" for h in range(24)]
    for e in entries:
        if e["open"] or e["end"] is None:
            continue
        buckets = raw_r if e["type"] == "REAL" else raw_s
        for h in range(24):
            bs = day0 + timedelta(hours=h)
            be = bs + timedelta(hours=1)
            s  = max(e["start"], bs)
            en = min(e["end"],   be)
            if en > s:
                buckets[h] += int((en - s).total_seconds())
    unit, div = _chart_unit_divisor(raw_r, raw_s)
    return {"labels": labels, "real": _normalise(raw_r, div),
            "simulated": _normalise(raw_s, div), "unit": unit}


def chart_weekly(entries):
    now    = datetime.now()
    labels, raw_r, raw_s = [], [], []
    for d in range(6, -1, -1):
        ds = (now - timedelta(days=d)).replace(hour=0, minute=0, second=0, microsecond=0)
        de = ds + timedelta(days=1)
        labels.append(ds.strftime("%a %d"))
        r = s = 0
        for e in entries:
            if e["open"] or e["end"] is None:
                continue
            ov_s = max(e["start"], ds)
            ov_e = min(e["end"],   de)
            if ov_e > ov_s:
                dur = int((ov_e - ov_s).total_seconds())
                if e["type"] == "REAL": r += dur
                else:                   s += dur
        raw_r.append(r)
        raw_s.append(s)
    unit, div = _chart_unit_divisor(raw_r, raw_s)
    return {"labels": labels, "real": _normalise(raw_r, div),
            "simulated": _normalise(raw_s, div), "unit": unit}


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
            if e["open"] or e["end"] is None:
                continue
            ov_s = max(e["start"], day)
            ov_e = min(e["end"],   nd)
            if ov_e > ov_s:
                dur = int((ov_e - ov_s).total_seconds())
                if e["type"] == "REAL": r += dur
                else:                   s += dur
        raw_r.append(r)
        raw_s.append(s)
        day = nd
    unit, div = _chart_unit_divisor(raw_r, raw_s)
    return {"labels": labels, "real": _normalise(raw_r, div),
            "simulated": _normalise(raw_s, div), "unit": unit}


# ─────────────────────────────────────────────────────────────────────────────
#  RECENT EVENTS  (last 6, newest first)
# ─────────────────────────────────────────────────────────────────────────────

def recent_events(entries, n=6) -> list:
    sorted_e = sorted(entries, key=lambda e: e["start"], reverse=True)
    out = []
    for e in sorted_e[:n]:
        out.append({
            "idx":          e["idx"],
            "type":         e["type"],
            "open":         e["open"],
            "start":        e["start"].strftime("%H:%M  %d %b %Y"),
            "start_iso":    e["start"].strftime(ISO_FMT),
            "end":          "Ongoing…" if e["open"] else e["end"].strftime("%H:%M  %d %b %Y"),
            "end_iso":      "-" if e["open"] else e["end"].strftime(ISO_FMT),
            "duration":     "Ongoing" if e["open"] else _fmt_duration(e["duration"]),
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
            return                              # already tracking an outage
        start_ts              = time.time()
        state["offline_since"] = start_ts
        state["is_simulated"]  = simulated
        state["alert_sent"]    = False

    # Write to log file immediately
    evt_type     = "SIMULATED" if simulated else "REAL"
    open_line    = log_open_entry(evt_type, start_ts)

    with state_lock:
        state["open_line_idx"] = open_line

    now_str = datetime.fromtimestamp(start_ts).strftime("%H:%M on %d %b %Y")

    if simulated:
        log.info("Electricity OFFLINE [SIMULATED]")
        send_telegram(
            "🔌 <b>Simulated Power Cut Active</b>\n"
            f"Triggered at <b>{now_str}</b>.\n"
            "Monitor will report <i>offline</i> until simulation is cancelled."
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
        offline_since  = state["offline_since"]
        simulated      = state["is_simulated"]
        open_line_idx  = state["open_line_idx"]
        if offline_since is None:
            return
        state["offline_since"] = None
        state["open_line_idx"] = None
        state["is_simulated"]  = False
        state["alert_sent"]    = False

    end_ts   = time.time()
    duration = int(end_ts - offline_since)

    if duration < 10:
        log.info(f"Ignoring sub-10s blip ({duration}s)")
        # Still close the open entry so it doesn't linger
        if open_line_idx is not None:
            close_open_entry(open_line_idx, end_ts)
        return

    # Close the open log entry that was written when the outage started
    if open_line_idx is not None:
        close_open_entry(open_line_idx, end_ts)

    now_str = datetime.fromtimestamp(end_ts).strftime("%H:%M on %d %b %Y")

    if simulated:
        log.info(f"Simulated outage ended ({_fmt_duration(duration)})")
        send_telegram(
            "✅ <b>Simulation Cancelled</b>\n"
            f"Ended at <b>{now_str}</b>.\n"
            f"Simulated duration: <b>{_fmt_duration(duration)}</b>.\n"
            "Monitor is now reporting live electricity status."
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
            return ("offline", True) if body == "offline" else ("online", False)
        log.warning(f"Pico returned HTTP {r.status_code}")
        return "unreachable", False
    except requests.exceptions.Timeout:
        return "unreachable", False
    except requests.exceptions.ConnectionError:
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
            if new_fails >= OFFLINE_HYSTERESIS:
                with state_lock:
                    state["current_status"] = "offline"
                if prev_status != "offline":
                    handle_went_offline(simulated=False)

        time.sleep(CONFIG["poll_interval"])


# ─────────────────────────────────────────────────────────────────────────────
#  FLASK APP
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────────────
#  DASHBOARD HTML
# ─────────────────────────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Electricity Uptime Monitor</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:      #0d1117;
    --card:    #161b22;
    --card2:   #1a2030;
    --border:  #21262d;
    --border2: #30363d;
    --text:    #c9d1d9;
    --muted:   #484f58;
    --accent:  #38bdf8;
    --green:   #34d399;
    --red:     #f87171;
    --amber:   #fbbf24;
    --blue:    #60a5fa;
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
  .transport-badge {
    display: inline-block; margin-top: .35rem;
    font-family: var(--mono); font-size: .62rem;
    background: rgba(56,189,248,.08); color: var(--accent);
    border: 1px solid rgba(56,189,248,.2); padding: .2rem .6rem; border-radius: 4px;
  }
  .pico-link {
    font-family: var(--mono); font-size: .72rem; color: var(--accent);
    text-decoration: none; border: 1px solid var(--border2);
    padding: .35rem .75rem; border-radius: 6px; transition: background .15s;
  }
  .pico-link:hover { background: rgba(56,189,248,.1); }
  .refresh-badge {
    font-family: var(--mono); font-size: .62rem; color: var(--muted);
    border: 1px solid var(--border); padding: .2rem .5rem; border-radius: 4px;
  }

  /* ── Status hero ── */
  .status-hero {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 1.6rem 1.8rem; display: flex; align-items: center;
    justify-content: space-between; flex-wrap: wrap; gap: 1rem;
    margin-bottom: 1.5rem; position: relative; overflow: hidden;
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
  @keyframes pulse-g { 0%{box-shadow:0 0 0 0 rgba(52,211,153,.4)} 70%{box-shadow:0 0 0 8px rgba(52,211,153,0)} 100%{box-shadow:0 0 0 0 rgba(52,211,153,0)} }
  @keyframes pulse-r { 0%{box-shadow:0 0 0 0 rgba(248,113,113,.4)} 70%{box-shadow:0 0 0 8px rgba(248,113,113,0)} 100%{box-shadow:0 0 0 0 rgba(248,113,113,0)} }

  /* ── Section heading ── */
  .section-head {
    font-size: .65rem; letter-spacing: .14em; text-transform: uppercase;
    color: var(--muted); margin-bottom: .8rem; margin-top: 1.8rem;
    display: flex; align-items: center; justify-content: space-between;
  }
  .add-btn {
    font-family: var(--mono); font-size: .62rem; font-weight: 600;
    background: rgba(96,165,250,.1); color: var(--blue);
    border: 1px solid rgba(96,165,250,.25); padding: .22rem .7rem;
    border-radius: 5px; cursor: pointer; transition: all .15s; letter-spacing: 0;
    text-transform: none;
  }
  .add-btn:hover { background: rgba(96,165,250,.2); }

  /* ── Stats grid ── */
  .stats-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(200px,1fr)); gap: 1rem; margin-bottom: 1.5rem;
  }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); padding: 1.2rem 1.4rem; }
  .stat-card .period { font-size: .62rem; letter-spacing: .12em; text-transform: uppercase; color: var(--muted); margin-bottom: .5rem; }
  .stat-card .dur    { font-family: var(--mono); font-size: 1.1rem; font-weight: 600; color: #e8f0fa; margin-bottom: .25rem; }
  .stat-card .pct b  { color: var(--red); }
  .stat-card .pct    { font-size: .75rem; color: var(--muted); }

  /* ── Timeline ── */
  .timeline { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
  .tl-row {
    display: grid;
    grid-template-columns: 90px 1fr 1fr 110px 72px;
    align-items: center; gap: .6rem;
    padding: .7rem 1.1rem; border-bottom: 1px solid var(--border);
    font-size: .78rem; transition: background .12s;
  }
  .tl-row:last-child { border-bottom: none; }
  .tl-row:hover      { background: rgba(255,255,255,.02); }
  .tl-header {
    font-size: .6rem; letter-spacing: .12em; text-transform: uppercase;
    color: var(--muted); background: rgba(0,0,0,.25); cursor: default;
  }
  .tl-badge {
    display: inline-block; font-size: .6rem; font-weight: 700; letter-spacing: .06em;
    text-transform: uppercase; padding: .18rem .5rem; border-radius: 4px;
  }
  .tl-badge.REAL      { background: rgba(248,113,113,.15); color: var(--red);   border: 1px solid rgba(248,113,113,.25); }
  .tl-badge.SIMULATED { background: rgba(251,191,36,.12);  color: var(--amber); border: 1px solid rgba(251,191,36,.2); }
  .tl-badge.OPEN      { background: rgba(251,191,36,.2);   color: var(--amber); border: 1px solid var(--amber); animation: blink 1.4s infinite; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.45} }
  .tl-time  { font-family: var(--mono); color: var(--text); font-size: .72rem; }
  .tl-dur   { font-family: var(--mono); color: var(--muted); font-size: .72rem; }
  .tl-empty { padding: 1.4rem 1.2rem; color: var(--muted); font-size: .8rem; font-style: italic; }
  .tl-actions { display: flex; gap: .35rem; }
  .tl-btn {
    background: none; border: 1px solid var(--border2); color: var(--muted);
    border-radius: 5px; cursor: pointer; font-size: .72rem; padding: .2rem .45rem;
    transition: all .15s; line-height: 1.4; white-space: nowrap;
  }
  .tl-btn.edit:hover { border-color: var(--blue);  color: var(--blue);  background: rgba(96,165,250,.08); }
  .tl-btn.del:hover  { border-color: var(--red);   color: var(--red);   background: rgba(248,113,113,.08); }

  /* ── Chart cards ── */
  .chart-card {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 1.4rem 1.4rem 1.2rem; margin-bottom: 1rem;
  }
  .chart-title { font-size: .78rem; font-weight: 600; color: #c8d6e8; margin-bottom: 1rem; }
  .chart-unit  { font-size: .65rem; color: var(--muted); font-family: var(--mono); margin-left: .4rem; font-weight: 400; }
  .chart-wrap  { position: relative; height: 180px; }
  .legend      { display: flex; gap: 1.2rem; margin-top: .75rem; flex-wrap: wrap; }
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
  .test-tg-btn:hover    { border-color: var(--accent); color: var(--accent); background: rgba(56,189,248,.06); }
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
    pointer-events: none; z-index: 999; max-width: 300px;
  }
  #toast.show { opacity: 1; transform: translateY(0); }

  /* ── Modals (shared base) ── */
  .modal-overlay {
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,.75); z-index: 900;
    align-items: center; justify-content: center; padding: 1rem;
  }
  .modal-overlay.open { display: flex; }
  .modal {
    background: var(--card); border: 1px solid var(--border2); border-radius: var(--radius);
    padding: 1.8rem; width: 100%; max-width: 440px;
    box-shadow: 0 12px 50px rgba(0,0,0,.7);
    max-height: 90vh; overflow-y: auto;
  }
  .modal h3 { font-size: 1rem; color: #e8f0fa; margin-bottom: 1.2rem; }
  .modal-actions { display: flex; gap: .65rem; justify-content: flex-end; margin-top: 1.4rem; }

  /* ── Form controls inside modals ── */
  .form-group { margin-bottom: 1rem; }
  .form-group label {
    display: block; font-size: .68rem; letter-spacing: .1em;
    text-transform: uppercase; color: var(--muted); margin-bottom: .4rem;
  }
  .form-row { display: grid; grid-template-columns: 1fr 1fr; gap: .6rem; }
  .form-row-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: .6rem; }

  select, input[type="number"] {
    width: 100%; background: var(--bg); border: 1px solid var(--border2);
    color: var(--text); font-family: var(--mono); font-size: .8rem;
    padding: .45rem .6rem; border-radius: 6px; outline: none;
    transition: border-color .15s; appearance: none;
  }
  select:focus, input[type="number"]:focus { border-color: var(--accent); }

  .radio-group { display: flex; gap: 1rem; }
  .radio-item  { display: flex; align-items: center; gap: .4rem; cursor: pointer; font-size: .82rem; }
  .radio-item input[type="radio"] { accent-color: var(--accent); width: 14px; height: 14px; cursor: pointer; }

  .optional-note { font-size: .65rem; color: var(--muted); margin-top: .3rem; font-style: italic; }

  .section-divider {
    font-size: .62rem; letter-spacing: .12em; text-transform: uppercase;
    color: var(--muted); margin: 1.2rem 0 .8rem;
    border-top: 1px solid var(--border); padding-top: .8rem;
  }

  /* ── Modal buttons ── */
  .btn-cancel {
    background: none; border: 1px solid var(--border2); color: var(--muted);
    font-family: var(--sans); font-size: .82rem; padding: .45rem .95rem;
    border-radius: 6px; cursor: pointer; transition: all .12s;
  }
  .btn-cancel:hover { border-color: var(--text); color: var(--text); }
  .btn-danger {
    background: rgba(248,113,113,.15); border: 1px solid rgba(248,113,113,.35);
    color: var(--red); font-family: var(--sans); font-size: .82rem; font-weight: 700;
    padding: .45rem .95rem; border-radius: 6px; cursor: pointer; transition: all .12s;
  }
  .btn-danger:hover { background: rgba(248,113,113,.28); }
  .btn-primary {
    background: rgba(56,189,248,.15); border: 1px solid rgba(56,189,248,.35);
    color: var(--accent); font-family: var(--sans); font-size: .82rem; font-weight: 700;
    padding: .45rem .95rem; border-radius: 6px; cursor: pointer; transition: all .12s;
  }
  .btn-primary:hover { background: rgba(56,189,248,.28); }

  @media (max-width: 600px) {
    .tl-row { grid-template-columns: 80px 1fr 64px; }
    .tl-col-end, .tl-col-dur { display: none; }
    .status-value { font-size: 1.4rem; }
    .chart-wrap   { height: 150px; }
    .form-row, .form-row-3 { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div class="page">

  <!-- Header -->
  <header>
    <div>
      <div class="logo">Electricity<span>Monitor</span></div>
      <span class="transport-badge">HTTP polling · {{ poll_interval }}s</span>
    </div>
    <div style="display:flex;gap:.75rem;align-items:center;flex-wrap:wrap">
      <span class="refresh-badge" id="refresh-counter">refreshing in 10s</span>
      <a class="pico-link" href="{{ pico_url }}" target="_blank">&#8599; Pico Dashboard</a>
    </div>
  </header>

  <!-- Status Hero -->
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

  <!-- Recent Events -->
  <div class="section-head">
    <span>Recent Outage Events (last 6)</span>
    <button class="add-btn" onclick="openAddModal()">+ Add Event</button>
  </div>
  <div class="timeline" id="timeline-wrap">
    <div class="tl-row tl-header" id="tl-header-row">
      <div>Type</div>
      <div>Started</div>
      <div class="tl-col-end">Ended</div>
      <div class="tl-col-dur">Duration</div>
      <div>Actions</div>
    </div>
    {% if recent %}
      {% for ev in recent %}
      <div class="tl-row" data-idx="{{ ev.idx }}">
        <div>
          <span class="tl-badge {{ ev.type }}">{{ ev.type }}</span>
          {% if ev.open %}<span class="tl-badge OPEN" style="margin-left:.3rem;font-size:.52rem">LIVE</span>{% endif %}
        </div>
        <div class="tl-time">{{ ev.start }}</div>
        <div class="tl-time tl-col-end">{{ ev.end }}</div>
        <div class="tl-dur tl-col-dur">{{ ev.duration }}</div>
        <div class="tl-actions">
          <button class="tl-btn edit" onclick="openEditModal({{ ev.idx }},'{{ ev.type }}','{{ ev.start_iso }}','{{ ev.end_iso }}')" title="Edit">✎</button>
          <button class="tl-btn del"  onclick="askDelete({{ ev.idx }},'{{ ev.start }}')" title="Delete">✕</button>
        </div>
      </div>
      {% endfor %}
    {% else %}
      <div class="tl-empty" id="tl-empty">No outage events recorded yet.</div>
    {% endif %}
  </div>

  <!-- Stats -->
  <div class="section-head"><span>Power Downtime Statistics (real outages only)</span></div>
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
  <div class="section-head"><span>Outage Charts</span></div>

  <div class="chart-card">
    <div class="chart-title">Today — hourly breakdown <span class="chart-unit" id="unit-day">({{ chart_data.daily.unit }})</span></div>
    <div class="chart-wrap"><canvas id="chartDay"></canvas></div>
    <div class="legend">
      <div class="legend-item"><div class="legend-dot real"></div> Real outage</div>
      <div class="legend-item"><div class="legend-dot sim"></div> Simulated</div>
    </div>
  </div>

  <div class="chart-card">
    <div class="chart-title">Last 7 Days — daily breakdown <span class="chart-unit" id="unit-week">({{ chart_data.weekly.unit }})</span></div>
    <div class="chart-wrap"><canvas id="chartWeek"></canvas></div>
    <div class="legend">
      <div class="legend-item"><div class="legend-dot real"></div> Real outage</div>
      <div class="legend-item"><div class="legend-dot sim"></div> Simulated</div>
    </div>
  </div>

  <div class="chart-card">
    <div class="chart-title">This Month — daily breakdown <span class="chart-unit" id="unit-month">({{ chart_data.monthly.unit }})</span></div>
    <div class="chart-wrap"><canvas id="chartMonth"></canvas></div>
    <div class="legend">
      <div class="legend-item"><div class="legend-dot real"></div> Real outage</div>
      <div class="legend-item"><div class="legend-dot sim"></div> Simulated</div>
    </div>
  </div>

  <!-- Footer -->
  <footer>
    <div>electricity_monitor.py &nbsp;·&nbsp; location: {{ device_id }} &nbsp;·&nbsp; auto-refresh every 10s</div>
    <button class="test-tg-btn" id="test-tg-btn" onclick="sendTestTelegram()">📨 Send test Telegram message</button>
  </footer>
</div>

<!-- ═══════════════════════════════════════════════════════════ -->
<!--  DELETE CONFIRM MODAL                                       -->
<!-- ═══════════════════════════════════════════════════════════ -->
<div class="modal-overlay" id="del-overlay">
  <div class="modal">
    <h3>⚠ Delete outage event?</h3>
    <p id="del-body" style="font-size:.82rem;color:var(--muted);line-height:1.6">
      This will permanently remove the event from the log. This cannot be undone.
    </p>
    <div class="modal-actions">
      <button class="btn-cancel" onclick="closeModal('del-overlay')">Cancel</button>
      <button class="btn-danger" id="del-confirm-btn">Yes, delete it</button>
    </div>
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════════ -->
<!--  EDIT / ADD MODAL  (shared)                                 -->
<!-- ═══════════════════════════════════════════════════════════ -->
<div class="modal-overlay" id="form-overlay">
  <div class="modal">
    <h3 id="form-title">Edit Outage Event</h3>

    <!-- Type -->
    <div class="form-group">
      <label>Outage Type</label>
      <div class="radio-group">
        <label class="radio-item"><input type="radio" name="ev-type" value="REAL" checked> Real</label>
        <label class="radio-item"><input type="radio" name="ev-type" value="SIMULATED"> Simulated</label>
      </div>
    </div>

    <!-- Start -->
    <div class="section-divider">Start Time</div>
    <div class="form-row" style="margin-bottom:.6rem">
      <div class="form-group" style="margin-bottom:0">
        <label>Date</label>
        <div class="form-row-3">
          <div><label style="font-size:.6rem;margin-bottom:.2rem;display:block">Year</label>
            <select id="s-year"></select></div>
          <div><label style="font-size:.6rem;margin-bottom:.2rem;display:block">Month</label>
            <select id="s-month"></select></div>
          <div><label style="font-size:.6rem;margin-bottom:.2rem;display:block">Day</label>
            <select id="s-day"></select></div>
        </div>
      </div>
      <div class="form-group" style="margin-bottom:0">
        <label>Time</label>
        <div class="form-row">
          <div><label style="font-size:.6rem;margin-bottom:.2rem;display:block">Hour (00–23)</label>
            <select id="s-hour"></select></div>
          <div><label style="font-size:.6rem;margin-bottom:.2rem;display:block">Minute</label>
            <select id="s-min"></select></div>
        </div>
      </div>
    </div>

    <!-- End -->
    <div class="section-divider">End Time <span class="optional-note">(leave blank = still ongoing / OPEN)</span></div>
    <div class="form-row">
      <div class="form-group" style="margin-bottom:0">
        <label>Date</label>
        <div class="form-row-3">
          <div><label style="font-size:.6rem;margin-bottom:.2rem;display:block">Year</label>
            <select id="e-year"></select></div>
          <div><label style="font-size:.6rem;margin-bottom:.2rem;display:block">Month</label>
            <select id="e-month"></select></div>
          <div><label style="font-size:.6rem;margin-bottom:.2rem;display:block">Day</label>
            <select id="e-day"></select></div>
        </div>
      </div>
      <div class="form-group" style="margin-bottom:0">
        <label>Time</label>
        <div class="form-row">
          <div><label style="font-size:.6rem;margin-bottom:.2rem;display:block">Hour (00–23)</label>
            <select id="e-hour"></select></div>
          <div><label style="font-size:.6rem;margin-bottom:.2rem;display:block">Minute</label>
            <select id="e-min"></select></div>
        </div>
      </div>
    </div>
    <div class="form-group" style="margin-top:.6rem">
      <label class="radio-item" style="font-size:.8rem;font-weight:500">
        <input type="checkbox" id="end-open-chk" style="accent-color:var(--accent);width:14px;height:14px">
        &nbsp;Mark as still ongoing (OPEN — no end time)
      </label>
    </div>

    <div class="modal-actions">
      <button class="btn-cancel" onclick="closeModal('form-overlay')">Cancel</button>
      <button class="btn-primary" id="form-save-btn">Save</button>
    </div>
  </div>
</div>

<!-- Toast -->
<div id="toast"></div>

<script>
// ── Chart setup ───────────────────────────────────────────────────────────────
Chart.defaults.color       = '#4a5568';
Chart.defaults.font.family = "'JetBrains Mono', monospace";
Chart.defaults.font.size   = 10;

const RED  = 'rgba(248,113,113,0.85)', AMBER = 'rgba(251,191,36,0.75)';
const RED_B= 'rgba(248,113,113,1)',    AMB_B = 'rgba(251,191,36,1)';

function tooltipCb(unit) { return ctx => ` ${ctx.dataset.label}: ${ctx.parsed.y} ${unit}`; }

function makeChart(id, data) {
  return new Chart(document.getElementById(id), {
    type: 'bar',
    data: { labels: data.labels, datasets: [
        { label:'Real',      data:data.real,      backgroundColor:RED,   borderColor:RED_B, borderWidth:1, borderRadius:3 },
        { label:'Simulated', data:data.simulated, backgroundColor:AMBER, borderColor:AMB_B, borderWidth:1, borderRadius:3 },
    ]},
    options: {
      responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{display:false}, tooltip:{callbacks:{label:tooltipCb(data.unit)}} },
      scales:{
        x:{ stacked:true, grid:{color:'#1e2530'}, ticks:{maxRotation:45,minRotation:0} },
        y:{ stacked:true, grid:{color:'#1e2530'}, beginAtZero:true,
            title:{display:true, text:data.unit, color:'#484f58', font:{size:9}} }
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
  chart.data.labels = data.labels;
  chart.data.datasets[0].data = data.real;
  chart.data.datasets[1].data = data.simulated;
  chart.options.plugins.tooltip.callbacks.label = tooltipCb(data.unit);
  chart.options.scales.y.title.text = data.unit;
  chart.update('none');
  if (unitEl) unitEl.textContent = `(${data.unit})`;
}

function fetchStatus() {
  fetch('/api/status').then(r=>r.json()).then(d=>{
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
  }).catch(e=>console.warn('fetch failed',e));
}

// ── Timeline ──────────────────────────────────────────────────────────────────
function renderTimeline(events) {
  const wrap   = document.getElementById('timeline-wrap');
  const header = document.getElementById('tl-header-row');
  wrap.innerHTML = '';
  wrap.appendChild(header);
  if (!events.length) {
    const el = document.createElement('div');
    el.id='tl-empty'; el.className='tl-empty'; el.textContent='No outage events recorded yet.';
    wrap.appendChild(el); return;
  }
  events.forEach(ev => {
    const row  = document.createElement('div');
    row.className = 'tl-row'; row.dataset.idx = ev.idx;
    const liveBadge = ev.open ? `<span class="tl-badge OPEN" style="margin-left:.3rem;font-size:.52rem">LIVE</span>` : '';
    const qs = s => s.replace(/'/g,"\\'");
    row.innerHTML =
      `<div><span class="tl-badge ${ev.type}">${ev.type}</span>${liveBadge}</div>` +
      `<div class="tl-time">${ev.start}</div>` +
      `<div class="tl-time tl-col-end">${ev.end}</div>` +
      `<div class="tl-dur tl-col-dur">${ev.duration}</div>` +
      `<div class="tl-actions">` +
        `<button class="tl-btn edit" onclick="openEditModal(${ev.idx},'${qs(ev.type)}','${qs(ev.start_iso)}','${qs(ev.end_iso)}')" title="Edit">✎</button>` +
        `<button class="tl-btn del"  onclick="askDelete(${ev.idx},'${qs(ev.start)}')" title="Delete">✕</button>` +
      `</div>`;
    wrap.appendChild(row);
  });
}

// ── Dropdown builders ─────────────────────────────────────────────────────────
function fillSelect(id, values, selected) {
  const sel = document.getElementById(id);
  sel.innerHTML = '';
  values.forEach(v => {
    const opt  = document.createElement('option');
    const val  = String(v).padStart(2,'0');
    opt.value  = val; opt.textContent = val;
    if (String(v) === String(selected) || val === String(selected)) opt.selected = true;
    sel.appendChild(opt);
  });
}

function buildYears(selected) {
  const now  = new Date();
  const yrs  = [];
  for (let y = now.getFullYear(); y >= now.getFullYear() - 5; y--) yrs.push(y);
  fillSelect('s-year', yrs, selected || now.getFullYear());
  fillSelect('e-year', yrs, selected || now.getFullYear());
}

function buildMonths(sSelected, eSelected) {
  const months = Array.from({length:12}, (_,i) => String(i+1).padStart(2,'0'));
  fillSelect('s-month', months, sSelected || new Date().getMonth()+1);
  fillSelect('e-month', months, eSelected || new Date().getMonth()+1);
}

function buildDays(sSelected, eSelected) {
  const days = Array.from({length:31}, (_,i) => String(i+1).padStart(2,'0'));
  fillSelect('s-day', days, sSelected || new Date().getDate());
  fillSelect('e-day', days, eSelected || new Date().getDate());
}

function buildHoursMinutes(sH, sM, eH, eM) {
  const hours = Array.from({length:24}, (_,i) => String(i).padStart(2,'0'));
  const mins  = Array.from({length:60}, (_,i) => String(i).padStart(2,'0'));
  fillSelect('s-hour', hours, sH !== undefined ? sH : new Date().getHours());
  fillSelect('s-min',  mins,  sM !== undefined ? sM : 0);
  fillSelect('e-hour', hours, eH !== undefined ? eH : new Date().getHours());
  fillSelect('e-min',  mins,  eM !== undefined ? eM : 0);
}

// parse "2025-06-15T14:30:00" → {year,month,day,hour,min}
function parseISO(iso) {
  if (!iso || iso === '-') return null;
  const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (!m) return null;
  return { year:m[1], month:m[2], day:m[3], hour:m[4], min:m[5] };
}

function getFormValues() {
  const type     = document.querySelector('input[name="ev-type"]:checked').value;
  const sYear    = document.getElementById('s-year').value;
  const sMonth   = document.getElementById('s-month').value;
  const sDay     = document.getElementById('s-day').value;
  const sHour    = document.getElementById('s-hour').value;
  const sMin     = document.getElementById('s-min').value;
  const startIso = `${sYear}-${sMonth}-${sDay}T${sHour}:${sMin}:00`;

  const isOpen = document.getElementById('end-open-chk').checked;
  let endIso = '-';
  if (!isOpen) {
    const eYear  = document.getElementById('e-year').value;
    const eMonth = document.getElementById('e-month').value;
    const eDay   = document.getElementById('e-day').value;
    const eHour  = document.getElementById('e-hour').value;
    const eMin   = document.getElementById('e-min').value;
    endIso = `${eYear}-${eMonth}-${eDay}T${eHour}:${eMin}:00`;
  }
  return { type, startIso, endIso };
}

// ── Edit modal ────────────────────────────────────────────────────────────────
let editingIdx = null;

function openEditModal(idx, type, startIso, endIso) {
  editingIdx = idx;
  document.getElementById('form-title').textContent = 'Edit Outage Event';
  document.querySelector(`input[name="ev-type"][value="${type}"]`).checked = true;

  const s = parseISO(startIso);
  const e = parseISO(endIso);

  buildYears(s ? s.year : null);
  buildMonths(s ? s.month : null, e ? e.month : null);
  buildDays(s ? s.day : null, e ? e.day : null);
  buildHoursMinutes(s?s.hour:undefined, s?s.min:undefined, e?e.hour:undefined, e?e.min:undefined);

  const isOpen = (!endIso || endIso === '-');
  document.getElementById('end-open-chk').checked = isOpen;

  if (s) {
    fillSelect('s-year',  Array.from({length:6},(_,i)=>new Date().getFullYear()-i), s.year);
    fillSelect('s-month', Array.from({length:12},(_,i)=>String(i+1).padStart(2,'0')), s.month);
    fillSelect('s-day',   Array.from({length:31},(_,i)=>String(i+1).padStart(2,'0')), s.day);
    fillSelect('s-hour',  Array.from({length:24},(_,i)=>String(i).padStart(2,'0')), s.hour);
    fillSelect('s-min',   Array.from({length:60},(_,i)=>String(i).padStart(2,'0')), s.min);
  }
  if (e && !isOpen) {
    fillSelect('e-year',  Array.from({length:6},(_,i)=>new Date().getFullYear()-i), e.year);
    fillSelect('e-month', Array.from({length:12},(_,i)=>String(i+1).padStart(2,'0')), e.month);
    fillSelect('e-day',   Array.from({length:31},(_,i)=>String(i+1).padStart(2,'0')), e.day);
    fillSelect('e-hour',  Array.from({length:24},(_,i)=>String(i).padStart(2,'0')), e.hour);
    fillSelect('e-min',   Array.from({length:60},(_,i)=>String(i).padStart(2,'0')), e.min);
  }

  document.getElementById('form-save-btn').onclick = saveEdit;
  document.getElementById('form-overlay').classList.add('open');
}

function saveEdit() {
  const { type, startIso, endIso } = getFormValues();
  const status = endIso === '-' ? 'OPEN' : 'CLOSED';
  fetch('/api/update_event', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ idx: editingIdx, type, start: startIso, end: endIso, status })
  }).then(r=>r.json()).then(d=>{
    showToast(d.ok ? '✓ Event updated' : '✗ ' + (d.error||'Update failed'));
    if (d.ok) { closeModal('form-overlay'); fetchStatus(); }
  }).catch(()=>showToast('✗ Network error'));
}

// ── Add modal ─────────────────────────────────────────────────────────────────
function openAddModal() {
  editingIdx = null;
  document.getElementById('form-title').textContent = 'Add Outage Event';
  document.querySelector('input[name="ev-type"][value="REAL"]').checked = true;

  const now = new Date();
  buildYears(now.getFullYear());
  buildMonths(now.getMonth()+1, now.getMonth()+1);
  buildDays(now.getDate(), now.getDate());
  buildHoursMinutes(now.getHours(), 0, now.getHours(), 0);
  document.getElementById('end-open-chk').checked = false;

  document.getElementById('form-save-btn').onclick = saveAdd;
  document.getElementById('form-overlay').classList.add('open');
}

function saveAdd() {
  const { type, startIso, endIso } = getFormValues();
  fetch('/api/add_event', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ type, start: startIso, end: endIso })
  }).then(r=>r.json()).then(d=>{
    showToast(d.ok ? '✓ Event added' : '✗ ' + (d.error||'Add failed'));
    if (d.ok) { closeModal('form-overlay'); fetchStatus(); }
  }).catch(()=>showToast('✗ Network error'));
}

// ── Delete modal ──────────────────────────────────────────────────────────────
let pendingDelIdx = null;

function askDelete(idx, startStr) {
  pendingDelIdx = idx;
  document.getElementById('del-body').textContent =
    `Delete the outage event starting at "${startStr}"? This cannot be undone.`;
  document.getElementById('del-confirm-btn').onclick = confirmDelete;
  document.getElementById('del-overlay').classList.add('open');
}

function confirmDelete() {
  if (pendingDelIdx === null) return;
  const idx = pendingDelIdx;
  closeModal('del-overlay');
  fetch('/api/delete_event', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({idx})
  }).then(r=>r.json()).then(d=>{
    showToast(d.ok ? '✓ Event deleted' : '✗ ' + (d.error||'Delete failed'));
    if (d.ok) fetchStatus();
  }).catch(()=>showToast('✗ Network error'));
}

// ── Modal helpers ─────────────────────────────────────────────────────────────
function closeModal(id) {
  document.getElementById(id).classList.remove('open');
}
document.querySelectorAll('.modal-overlay').forEach(o => {
  o.addEventListener('click', e => { if (e.target===o) o.classList.remove('open'); });
});

// ── Telegram test ─────────────────────────────────────────────────────────────
function sendTestTelegram() {
  const btn = document.getElementById('test-tg-btn');
  btn.disabled = true; btn.textContent = '📨 Sending…';
  fetch('/api/test_telegram', {method:'POST'})
    .then(r=>r.json())
    .then(d => showToast(d.ok ? '✓ Test message sent' : '✗ '+(d.error||'Failed')))
    .catch(()=>showToast('✗ Network error'))
    .finally(()=>{ btn.disabled=false; btn.textContent='📨 Send test Telegram message'; });
}

// ── Toast ─────────────────────────────────────────────────────────────────────
let toastTimer = null;
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(()=>t.classList.remove('show'), 3200);
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
    try:
        idx = int(data["idx"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid or missing idx"}), 400
    if not delete_log_entry_by_idx(idx):
        return jsonify({"ok": False, "error": "Entry not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/update_event", methods=["POST"])
def api_update_event():
    data = request.get_json(silent=True) or {}
    try:
        idx    = int(data["idx"])
        etype  = str(data["type"]).upper()
        start  = str(data["start"])
        end    = str(data.get("end", "-"))
        status = str(data.get("status", "CLOSED")).upper()
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid payload"}), 400
    if etype not in ("REAL", "SIMULATED"):
        return jsonify({"ok": False, "error": "type must be REAL or SIMULATED"}), 400
    if not update_log_entry(idx, etype, start, end, status):
        return jsonify({"ok": False, "error": "Update failed – bad index or timestamps"}), 400
    return jsonify({"ok": True})


@app.route("/api/add_event", methods=["POST"])
def api_add_event():
    data = request.get_json(silent=True) or {}
    try:
        etype = str(data["type"]).upper()
        start = str(data["start"])
        end   = str(data.get("end", "-"))
    except (KeyError, TypeError):
        return jsonify({"ok": False, "error": "Invalid payload"}), 400
    if etype not in ("REAL", "SIMULATED"):
        return jsonify({"ok": False, "error": "type must be REAL or SIMULATED"}), 400
    if not add_log_entry(etype, start, end):
        return jsonify({"ok": False, "error": "Add failed – check timestamps"}), 400
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
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("Electricity Uptime Monitor starting")
    log.info(f"  Location ID:   {DEVICE_ID}")
    log.info(f"  Pico URL:      {PICO_STATUS_URL}")
    log.info(f"  Poll interval: {CONFIG['poll_interval']}s")
    log.info(f"  Log file:      {LOG_FILE}")
    log.info(f"  Web UI:        http://{CONFIG['web_host']}:{CONFIG['web_port']}")
    log.info("=" * 60)

    restore_state_from_log()

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