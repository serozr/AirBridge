# AirBridge 📻

> **Ultra-low latency Windows system audio streaming to iPhone & AirPods over local Wi-Fi.**  
> *No native iOS app required. Pure WebRTC in Safari.*

[![Python](https://img.shields.io/badge/Python-3.10%20%7C%203.12%20%7C%203.14-blue.svg)](https://www.python.org/)
[![WebRTC](https://img.shields.io/badge/WebRTC-aiortc-brightgreen.svg)](https://github.com/aiortc/aiortc)
[![Codec](https://img.shields.io/badge/Codec-Opus%2048kHz%20Stereo-purple.svg)](https://opus-codec.org/)
[![FastAPI](https://img.shields.io/badge/Signaling-FastAPI%20%2F%20WebSocket-009688.svg)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Release](https://img.shields.io/badge/Release-v0.1.0--MVP-orange.svg)]()

---

## ⚡ What is AirBridge?

**AirBridge** turns your iPhone and AirPods into wireless headphones for your Windows PC without purchasing Bluetooth dongles, proprietary receivers, or installing native apps on your phone.

Using Windows **WASAPI Loopback**, **real libopus encoding**, and standard **WebRTC audio tracks**, AirBridge captures everything playing on your Windows PC (YouTube, Spotify, games, meetings) and streams it with **sub-50ms latency** directly to Safari on iOS.

```text
 ┌────────────────────────────────────────────────────────┐
 │                      WINDOWS PC                        │
 │  Master Sound Output                                   │
 │       │                                                │
 │       ▼                                                │
 │  WASAPI Loopback Capture (SoundCard)                   │
 │       │                                                │
 │       ▼                                                │
 │  48000 Hz Stereo PCM (20 ms frames)                    │
 │       │                                                │
 │       ▼                                                │
 │  Opus Audio Encoder (libopus @ 96 kbps)                │
 │       │                                                │
 │       ▼                                                │
 │  WebRTC Audio Engine (aiortc SRTP)                     │
 └───────┼────────────────────────────────────────────────┘
         │
         │  Local Wi-Fi Network (LAN)
         ▼
 ┌────────────────────────────────────────────────────────┐
 │                    IPHONE SAFARI                       │
 │  WebRTC AudioReceiver (HTML5 Audio + Web Audio API)    │
 │       │                                                │
 │       ▼                                                │
 │  🎧 AirPods / Earphones (Live, In-Sync Sound)          │
 └────────────────────────────────────────────────────────┘
```

---

## ✨ Features

- **Zero iOS Apps:** Connect straight from Mobile Safari. No TestFlight, App Store downloads, or sideloading needed.
- **Ultra-Low Latency:** Typically **20 ms – 40 ms** on standard home Wi-Fi networks (imperceptible delay for videos and casual games).
- **System-Wide Audio Capture:** Captures the master Windows audio bus directly via WASAPI loopback.
- **Broadcast-Grade Audio:** 48 kHz sampling rate, 2-channel stereo, encoded via native Opus at 96 kbps.
- **Auto-Dropping Queue:** Prevents audio buffer buildup if Wi-Fi fluctuates, ensuring you stay in real-time sync.
- **In-Browser Diagnostics & Visualizer:** Real-time audio frequency visualizer, packet counts, bitrate, and jitter statistics.

---

## 🚀 Quick Start

### 1. Prerequisites

- **Windows 10 or Windows 11**
- **Python 3.10+** (tested and verified on Python 3.12 & 3.14)
- **iPhone / iPad** connected to the same Wi-Fi network

### 2. Installation

Clone the repository and set up a virtual environment:

```powershell
git clone https://github.com/<your-username>/AirBridge.git
cd AirBridge

python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Run the Server

```powershell
python -u server/webrtc_server.py
```

Upon launching, AirBridge prints the active local network address:

```text
==================================================================
  AIRBRIDGE - Phase 4: Local Network & iPhone Streaming Server
==================================================================
  Local PC URL:      http://localhost:8000
  Recommended LAN:   http://192.168.0.105:8000
  Detected IPs:      192.168.0.105, ...
------------------------------------------------------------------
  >> On your iPhone (Safari connected to the same Wi-Fi):
     Open: http://192.168.0.105:8000
==================================================================
```

### 4. Connect with iPhone & AirPods

1. Put on your **AirPods** (paired to your iPhone).
2. Open **Safari** on your iPhone and navigate to the LAN URL displayed in the console (e.g., `http://192.168.0.105:8000`).
3. Tap **▶ Connect & Stream**.
4. Play any audio or video on your PC (YouTube, Spotify, etc.) — you will hear crystal-clear audio in your AirPods!

---

## 🛡️ Windows Firewall Notice

When connecting from another device on the local network (such as your iPhone), Windows Firewall may require allowing incoming traffic on TCP port 8000.

If Safari cannot load the page, open **PowerShell as Administrator** and run:

```powershell
New-NetFirewallRule -DisplayName "AirBridge WebRTC (TCP 8000)" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow
```

---

## 📁 Repository Structure

```text
AirBridge/
├── server/
│   ├── audio_capture.py     # Standalone WASAPI loopback capture test
│   ├── opus_encoder.py      # Standalone libopus PCM compression test
│   └── webrtc_server.py     # Core FastAPI + aiortc streaming server
├── web/
│   └── index.html           # Modern receiver frontend (WebRTC + Visualizer)
├── requirements.txt         # Pinned production dependencies
├── .gitignore               # Clean Git ignore rules
└── README.md                # Documentation & Architecture
```

---

## 🛠️ Architecture & Pipeline Details

| Stage | Technology | Details |
|---|---|---|
| **Capture** | SoundCard (WASAPI) | Master speaker output captured in 20 ms blocks (960 samples @ 48 kHz stereo) |
| **Buffering** | `asyncio.Queue` | Thread-safe dispatch with automatic oldest-frame drop to prevent latency lag |
| **Compression** | libopus (via `aiortc` / `av`) | Broadcast-quality Opus speech/audio profile @ 96 kbps |
| **Transport** | WebRTC (SRTP / UDP) | Direct peer-to-peer RTP transmission with sub-frame packaging |
| **Signaling** | FastAPI + WebSockets | Low-overhead JSON SDP Offer/Answer and ICE candidate exchange |
| **Playback** | HTML5 Audio + Web Audio API | Low-latency audio rendering with user-gesture unlock handling |

---

## 🗺️ Roadmap

- [x] **v0.1.0 (Current):** Working MVP with WASAPI capture, Opus encoding, WebRTC, and Safari support.
- [ ] **v0.2.0:** **QR Code Pairing** — scan directly from PC screen to open on iPhone without typing IP addresses.
- [ ] **v0.3.0:** **Windows System Tray App** — run in background with start on boot option.
- [ ] **v0.4.0:** **Standalone `.exe` Release** — zero-python single file executable.
- [ ] **v0.5.0:** **Dynamic Bitrate & Volume Normalization** — adaptive bitrate based on Wi-Fi link quality.

---

## 📄 License

This project is open-source under the [MIT License](LICENSE).
