# ⚡ An Overengineered Power Uptime Monitor

A reliable (and overkill) power outage monitoring system built using a **Raspberry Pi Pico 2W** and a Python backend.

---

## 🧠 Overview

This project monitors power uptime by using a WiFi-enabled microcontroller as an edge probe and a server-side polling system for analysis, alerting, and visualization.

---
## Screenshots

### Dashboard
![Dashboard Screenshot](https://i.snipboard.io/0e13mn.jpg)

### Statistics
![Dashboard Screenshot](https://i.snipboard.io/VADOf2.jpg)

### Telegram Notifications
![Dashboard Screenshot](https://i.snipboard.io/mrTu8g.jpg)

---

## 🏗️ Architecture

```
Pico 2W  ──HTTP──▶  Python Server  ──▶ Telegram Alerts
    │                     │
    │                     └──▶ Web Dashboard (stats + charts)
    │
    └──▶ Local HTTP endpoint (/status)
```

### Components

* **Device**: Raspberry Pi Pico 2W (MicroPython)
* **Transport**: HTTP polling
* **Backend**: Python (Flask + polling loop)
* **Alerts**: Telegram Bot API
* **Storage**: Flat log file (`power_log.txt`)
* **Dashboard**: Built-in web UI with charts

---

## 🔧 Features

### 📊 Downtime Analytics

* Daily / Weekly / Monthly downtime
* Percentage uptime tracking
* Human-readable durations

### 📈 Visual Dashboard

* Real-time status (ONLINE / OFFLINE)
* Hourly / Daily breakdown charts
* Real vs simulated outage distinction

### 🔔 Smart Alerts (Telegram)

* Power Lost / Power Restored notifications
* Includes downtime duration
* Tracks cumulative daily downtime

### 🧪 Simulation Support

* Manually simulate outages via Pico HTTP UI
* Simulated outages excluded from real stats

### 📡 Network-Aware Monitoring

* Handles transient network failures
* Prevents alert spam using debounce logic
* Optional latency tracking (recommended)

---

## 🔄 How It Works

1. The Pico exposes an HTTP endpoint (`/status`)
2. The server polls it every few seconds
3. If polls fail → device marked OFFLINE
4. If polls succeed → device marked ONLINE
5. Events are logged and alerts are sent accordingly

---

## 📁 Project Structure

```
.
├── power_monitor.py      # Main server script
├── config.ini           # Configuration (Ports, Telegram, etc.)
├── power_log.txt        # Event log
└── README.md
```

---

## ⚙️ Setup

### 📟 Pico Firmware (pi-pico-main.py)

This file contains the MicroPython script that runs on the **Raspberry Pi Pico 2W** and exposes the HTTP endpoint used by the server.

### 🛠️ Setup Instructions

1. Rename the file:

```bash
pi-pico-main.py → main.py
```

2. Open the file and update the required configuration:

   * WiFi SSID and password
   * Server IP / endpoint (if applicable)

3. Flash/upload the file to your Pico 2W:

   * Use tools like **Thonny**, **mpremote**, or any MicroPython uploader
   * Ensure the file is placed as `main.py` on the device

4. Reboot the Pico

---

### Python Server Script
### 1. Install dependencies

```bash
pip install paho-mqtt python-telegram-bot flask
```

### 2. Configure

Copy and edit:

```bash
config.example.ini → config.ini
```

Fill in:

* Telegram bot token + chat ID
* Device ID
* Web settings

### 3. Run

```bash
python power_monitor.py
```

---

## 🌐 Dashboard

Accessible at:

```
http://<server-ip>:<port>
```

Displays:

* Current device status
* Downtime statistics
* Historical charts