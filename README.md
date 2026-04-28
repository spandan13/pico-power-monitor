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
├── power-monitor.py      # Main server script
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
python power-monitor.py
```

---

## ⚙️ Running as a Service (systemd)

To run the monitor automatically on boot, you can use a systemd service.

The repository includes a ready-to-use service file:

```text
electricity-monitor.service
```

---

### 🛠️ Configuration

Before installing, edit the following lines inside the service file:

```ini
WorkingDirectory=/home/pi/electricity-monitor
ExecStart=/home/pi/electricity-monitor/venv/bin/python power-monitor.py
```

Update them to match your setup:

* `WorkingDirectory` → path where `power-monitor.py` is located
* `ExecStart` → path to your Python binary

Replace `ExecStart` with:

```bash
which python3
```

---

### 👤 Update User

Replace:

```ini
User=pi
Group=pi
```

With your actual system username if different.

---

### 🚀 Installation

Copy the service file and enable it:

```bash
sudo cp electricity-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable electricity-monitor   
# start on boot
sudo systemctl start electricity-monitor    
# start immediately
```

---

### 📜 Viewing Logs

System logs are handled by journald.

#### 🔴 Live logs (real-time)

```bash
journalctl -u electricity-monitor -f
```

#### 📄 Last 200 lines

```bash
journalctl -u electricity-monitor -n 200
```

---

### 🧰 Useful Commands

```bash
sudo systemctl status electricity-monitor    
# check service status
sudo systemctl restart electricity-monitor   
# restart service
sudo systemctl stop electricity-monitor      
# stop service
sudo systemctl disable electricity-monitor   
# disable auto-start
```

This ensures your power monitoring system runs continuously and reliably in the background.

## 🌐 Dashboard

Accessible at:

```
http://<server-ip>:<port>
```

Displays:

* Current device status
* Downtime statistics
* Historical charts
