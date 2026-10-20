# AirBridge 📻

> **Stream Windows PC audio to your iPhone or AirPods over Wi-Fi — no iOS app required.**  
> *Open the receiver in Safari, connect, and listen.*

[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue.svg)](https://www.microsoft.com/windows) [![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![WebRTC](https://img.shields.io/badge/WebRTC-aiortc-brightgreen.svg)](https://github.com/aiortc/aiortc)
[![Codec](https://img.shields.io/badge/Codec-Opus%2048kHz%20Stereo-purple.svg)](https://opus-codec.org/)
[![FastAPI](https://img.shields.io/badge/Signaling-FastAPI%20%2F%20WebSocket-009688.svg)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Version](https://img.shields.io/badge/status-working%20MVP-orange.svg)](https://github.com/serozr/AirBridge/releases/tag/v0.1.0)

---

## 🎬 Demo

A short screen recording or GIF showing the Windows-to-iPhone audio flow is planned. For now, the project status below reflects manual tests on the maintainer’s own devices.

## ⚡ What is AirBridge?

**AirBridge** turns your iPhone and AirPods into wireless headphones for your Windows PC without purchasing Bluetooth dongles, proprietary receivers, or installing native apps on your phone.

AirBridge captures Windows system output with **WASAPI loopback** and sends live audio to Safari on an iPhone over your local network using **WebRTC**. The current implementation has been manually tested on one Windows PC and one iPhone on the same Wi-Fi network. **Latency has not yet been benchmarked**, so results will depend on the network, browser, audio route, and AirPods.

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
- **Latency-aware buffering:** A bounded audio queue drops old frames when needed to help prevent growing delay. End-to-end latency still needs formal measurement.
- **System-Wide Audio Capture:** Captures the master Windows audio bus directly via WASAPI loopback.
- **Broadcast-Grade Audio:** 48 kHz sampling rate, 2-channel stereo, encoded via native Opus at 96 kbps.
- **Auto-Dropping Queue:** Prevents audio buffer buildup if Wi-Fi fluctuates, ensuring you stay in real-time sync.
- **In-Browser Diagnostics & Visualizer:** Real-time audio frequency visualizer, packet counts, bitrate, and jitter statistics.

---

## 🚀 Quick Start

### 1. Prerequisites

- **Windows 10 or Windows 11**
- **64-bit Python on Windows**. The project has been exercised in the maintainer environment; other Python versions should be treated as unverified until tested.
- **iPhone / iPad** connected to the same Wi-Fi network

### 2. Installation

Clone the repository and set up a virtual environment:

```powershell
git clone https://github.com/serozr/AirBridge.git
cd AirBridge

python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

> **Compatibility note:** Audio/WebRTC dependencies include native components. If installation fails on your Python version, try a supported 64-bit Python release and report the full pip error in an issue.

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
4. Play audio on your PC. Once the connection status is active, the live system audio should play through the iPhone output (including connected AirPods).

---

## 🔐 Network and firewall notes

AirBridge is designed for a trusted local network. The HTTP page and WebSocket signaling currently do not provide application-level authentication. Do not expose port 8000 to the public internet; use it only on a trusted LAN. Windows Firewall may need an inbound rule for TCP port 8000.

If Safari cannot load the page, open **PowerShell as Administrator** and run:

```powershell
New-NetFirewallRule -DisplayName "AirBridge WebRTC (TCP 8000)" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow -Profile Private
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

## 🧪 Validation status

- [x] Windows loopback capture produces live PCM frames.
- [x] Standalone Opus encoder test produces real Opus packets.
- [x] Live WebRTC audio playback verified in a desktop browser.
- [x] Live Windows audio heard on iPhone Safari over the same Wi-Fi network.
- [ ] Automated tests and repeatable end-to-end latency benchmarks.
- [ ] Tested compatibility matrix across Windows/Python/iOS versions.

These are maintainer-reported manual tests, not CI-certified compatibility guarantees.

## 🛠️ Architecture & Pipeline Details

| Stage | Technology | Details |
|---|---|---|
| **Capture** | SoundCard (WASAPI) | Master speaker output captured in 20 ms blocks (960 samples @ 48 kHz stereo) |
| **Buffering** | `asyncio.Queue` | Thread-safe dispatch with automatic oldest-frame drop to prevent latency lag |
| **Codec / media engine** | aiortc + PyAV | WebRTC negotiates Opus for the browser audio track; `server/opus_encoder.py` is a standalone encoder test, not the encoder wired into the main WebRTC server |
| **Transport** | WebRTC (SRTP / UDP) | Direct peer-to-peer RTP transmission with sub-frame packaging |
| **Signaling** | FastAPI + WebSockets | Low-overhead JSON SDP Offer/Answer and ICE candidate exchange |
| **Playback** | HTML5 Audio + Web Audio API | Low-latency audio rendering with user-gesture unlock handling |

---

## 🗺️ Roadmap

- [x] **MVP:** Manually verified Windows-to-iPhone streaming over the same Wi-Fi network.
- [ ] **v0.2.0:** **QR Code Pairing** — scan directly from PC screen to open on iPhone without typing IP addresses.
- [ ] **v0.3.0:** **Windows System Tray App** — run in background with start on boot option.
- [ ] **v0.4.0:** **Standalone `.exe` Release** — zero-python single file executable.
- [ ] **v0.5.0:** **Dynamic Bitrate & Volume Normalization** — adaptive bitrate based on Wi-Fi link quality.

---

## 🤝 Feedback and issues

Bug reports, setup issues, and suggestions are welcome via [GitHub Issues](https://github.com/serozr/AirBridge/issues). Please include your Windows version, Python version, iOS version, browser, network setup, and relevant sanitized diagnostics. Do not attach private audio recordings or sensitive network information.

## 📄 License

This project is open-source under the [MIT License](LICENSE).
