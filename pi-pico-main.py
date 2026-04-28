"""
Pico Power Monitor — main.py
Raspberry Pi Pico 2W | MicroPython
Single-file, self-contained HTTP uptime monitor with web UI.
"""

import network
import socket
import time
import machine

# ─────────────────────────────────────────────
# CONFIGURATION  — edit these before flashing
# ─────────────────────────────────────────────
WIFI_SSID     = ""
WIFI_PASSWORD = ""
HTTP_PORT     = 80
POLL_INTERVAL = 0.1   # seconds between socket polls (keeps loop responsive)

# ─────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────
state = {
    "current_status"  : "online",   # "online" | "offline"
    "last_offline_time": None,       # raw seconds from time.time() or None
    "manual_override"  : False,      # True  → simulate power-off
}

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def log(msg):
    print("[PICO]", msg)

def fmt_time(ts):
    """Format a time.time() value as HH:MM DD-MM-YYYY (local, no DST)."""
    if ts is None:
        return "Never"
    t = time.localtime(ts)
    return "{:02d}:{:02d} {:02d}-{:02d}-{:04d}".format(
        t[3], t[4], t[2], t[1], t[0]
    )

# ─────────────────────────────────────────────
# WI-FI
# ─────────────────────────────────────────────
wlan = network.WLAN(network.STA_IF)

def connect_wifi():
    wlan.active(True)
    if wlan.isconnected():
        return True
    log("Connecting to WiFi '{}'...".format(WIFI_SSID))
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)
    for _ in range(20):           # wait up to ~10 s
        if wlan.isconnected():
            log("WiFi connected. IP: {}".format(wlan.ifconfig()[0]))
            return True
        time.sleep(0.5)
    log("WiFi connection failed.")
    return False

def ensure_wifi():
    if not wlan.isconnected():
        log("WiFi lost — reconnecting…")
        connect_wifi()

# ─────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────
def build_html():
    status      = state["current_status"]
    override    = state["manual_override"]
    last_off    = fmt_time(state["last_offline_time"])

    status_cls  = "status-online" if status == "online" else "status-offline"
    status_lbl  = "ONLINE" if status == "online" else "OFFLINE"
    btn_label   = "Back ONLINE" if override else "Simulate Power OFF"
    btn_cls     = "btn-on" if override else "btn-off"

    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="5">
  <title>Pico Power Monitor</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Rajdhani:wght@400;600;700&display=swap');

    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    :root {{
      --bg:        #0b0e14;
      --surface:   #111520;
      --border:    #1e2535;
      --accent:    #00e5ff;
      --online:    #00e676;
      --offline:   #ff1744;
      --text:      #c9d1e0;
      --muted:     #4a5568;
      --mono:      'Share Tech Mono', monospace;
      --display:   'Rajdhani', sans-serif;
    }}

    html, body {{
      height: 100%;
      background: var(--bg);
      color: var(--text);
      font-family: var(--display);
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1rem;
    }}

    /* subtle grid overlay */
    body::before {{
      content: '';
      position: fixed;
      inset: 0;
      background-image:
        linear-gradient(rgba(0,229,255,.03) 1px, transparent 1px),
        linear-gradient(90deg, rgba(0,229,255,.03) 1px, transparent 1px);
      background-size: 40px 40px;
      pointer-events: none;
    }}

    .card {{
      position: relative;
      width: 100%;
      max-width: 480px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 2.5rem 2rem;
      box-shadow: 0 0 40px rgba(0,229,255,.06), 0 8px 32px rgba(0,0,0,.5);
      overflow: hidden;
    }}

    /* top accent bar */
    .card::before {{
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0;
      height: 3px;
      background: linear-gradient(90deg, var(--accent), transparent);
    }}

    .brand {{
      display: flex;
      align-items: center;
      gap: .6rem;
      margin-bottom: 2.2rem;
    }}

    .brand-icon {{
      width: 36px; height: 36px;
      border: 2px solid var(--accent);
      border-radius: 8px;
      display: grid;
      place-items: center;
    }}

    .brand-icon svg {{ display: block; }}

    h1 {{
      font-size: 1.4rem;
      font-weight: 700;
      letter-spacing: .12em;
      text-transform: uppercase;
      color: #fff;
    }}

    .divider {{
      height: 1px;
      background: var(--border);
      margin: 0 0 2rem;
    }}

    .row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1.4rem;
    }}

    .label {{
      font-size: .75rem;
      letter-spacing: .15em;
      text-transform: uppercase;
      color: var(--muted);
    }}

    .value {{
      font-family: var(--mono);
      font-size: .95rem;
    }}

    .status-online  {{ color: var(--online);  text-shadow: 0 0 12px var(--online);  }}
    .status-offline {{ color: var(--offline); text-shadow: 0 0 12px var(--offline); }}

    .pulse {{
      display: inline-block;
      width: 8px; height: 8px;
      border-radius: 50%;
      margin-right: 8px;
      vertical-align: middle;
      animation: pulse 1.4s ease-in-out infinite;
    }}
    .status-online  .pulse {{ background: var(--online); }}
    .status-offline .pulse {{ background: var(--offline); animation-play-state: paused; }}

    @keyframes pulse {{
      0%, 100% {{ opacity: 1; transform: scale(1);    }}
      50%       {{ opacity: .4; transform: scale(1.5); }}
    }}

    .btn {{
      display: block;
      width: 100%;
      margin-top: 2rem;
      padding: .85rem 1rem;
      border: none;
      border-radius: 10px;
      font-family: var(--display);
      font-size: 1rem;
      font-weight: 700;
      letter-spacing: .1em;
      text-transform: uppercase;
      cursor: pointer;
      transition: transform .1s, opacity .1s;
    }}
    .btn:active {{ transform: scale(.97); opacity: .85; }}

    .btn-off {{
      background: linear-gradient(135deg, #ff1744, #d50000);
      color: #fff;
      box-shadow: 0 4px 20px rgba(255,23,68,.3);
    }}
    .btn-on {{
      background: linear-gradient(135deg, #00e676, #00c853);
      color: #0b0e14;
      box-shadow: 0 4px 20px rgba(0,230,118,.3);
    }}

    .footer {{
      margin-top: 2rem;
      font-size: .7rem;
      color: var(--muted);
      font-family: var(--mono);
      text-align: center;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="brand">
      <div class="brand-icon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#00e5ff" stroke-width="2">
          <polyline points="13 2 13 9 20 9"/>
          <path d="M3 22V11l9-9 9 9v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>
        </svg>
      </div>
      <h1>Pico Power Monitor</h1>
    </div>

    <div class="divider"></div>

    <div class="row">
      <span class="label">Status</span>
      <span class="value {status_cls}">
        <span class="pulse"></span>{status_lbl}
      </span>
    </div>

    <div class="row">
      <span class="label">Last Offline</span>
      <span class="value">{last_off}</span>
    </div>

    <form method="POST" action="/toggle">
      <button class="btn {btn_cls}" type="submit">{btn_label}</button>
    </form>

    <p class="footer">auto-refresh &bull; 5s &nbsp;|&nbsp; port {port}</p>
  </div>
</body>
</html>""".format(
        status_cls=status_cls,
        status_lbl=status_lbl,
        last_off=last_off,
        btn_label=btn_label,
        btn_cls=btn_cls,
        port=HTTP_PORT,
    )

# ─────────────────────────────────────────────
# HTTP RESPONSES
# ─────────────────────────────────────────────
def send_response(conn, code, content_type, body):
    status_text = {200: "OK", 303: "See Other", 404: "Not Found"}.get(code, "OK")
    header = "HTTP/1.1 {} {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n".format(
        code, status_text, content_type, len(body)
    )
    if code == 303:
        header += "Location: /\r\n"
    header += "\r\n"
    conn.sendall(header.encode() + body.encode())

def send_redirect(conn):
    header = "HTTP/1.1 303 See Other\r\nLocation: /\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    conn.sendall(header.encode())

# ─────────────────────────────────────────────
# REQUEST HANDLING
# ─────────────────────────────────────────────
def handle_request(conn):
    try:
        raw = conn.recv(1024).decode("utf-8", "ignore")
    except OSError:
        return

    if not raw:
        return

    # Parse first line
    first_line = raw.split("\r\n")[0]
    parts = first_line.split(" ")
    if len(parts) < 2:
        return
    method, path = parts[0], parts[1]

    log("{} {}".format(method, path))

    # ── /status  — machine-readable endpoint for external polling ──
    if path == "/status":
        body = state["current_status"]   # "online" or "offline"
        send_response(conn, 200, "text/plain", body)
        return

    # ── /toggle  — flip manual override ──
    if method == "POST" and path == "/toggle":
        if not state["manual_override"]:
            # Going offline
            state["manual_override"]   = True
            state["current_status"]    = "offline"
            state["last_offline_time"] = time.time()
            log("Manual override ON  → status = offline")
        else:
            # Coming back online
            state["manual_override"] = False
            state["current_status"]  = "online"
            log("Manual override OFF → status = online")
        send_redirect(conn)
        return

    # ── / (root) ──
    if path in ("/", "/index.html"):
        send_response(conn, 200, "text/html", build_html())
        return

    # 404
    send_response(conn, 404, "text/plain", "Not Found")

# ─────────────────────────────────────────────
# SOCKET SETUP
# ─────────────────────────────────────────────
def create_server_socket():
    """Create a non-blocking TCP server socket, reusing the address."""
    addr = socket.getaddrinfo("0.0.0.0", HTTP_PORT, 0, socket.SOCK_STREAM)[0][-1]
    srv  = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(addr)
    srv.listen(4)
    srv.setblocking(False)
    log("HTTP server listening on port {}".format(HTTP_PORT))
    return srv

# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────
def main():
    connect_wifi()

    srv = create_server_socket()
    last_wifi_check = time.time()

    while True:
        # Periodic WiFi health check (every 10 s)
        now = time.time()
        if now - last_wifi_check > 10:
            ensure_wifi()
            last_wifi_check = now

        # Non-blocking accept
        try:
            conn, addr = srv.accept()
            conn.settimeout(3.0)
            try:
                handle_request(conn)
            except Exception as e:
                log("Request error: {}".format(e))
            finally:
                conn.close()
        except OSError:
            # No connection waiting — that's fine
            pass

        time.sleep(POLL_INTERVAL)

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
try:
    main()
except Exception as e:
    log("Fatal: {} — rebooting in 5 s".format(e))
    time.sleep(5)
    machine.reset()
