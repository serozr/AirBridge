# AirBridge 📻

> **Ultra-low latency Windows system audio streaming to iPhone & AirPods over local Wi-Fi.**  
> *No native iOS app required. Pure WebRTC in Safari.*

[![Python](https://img.shields.io/badge/Python-3.10%20%7C%203.12%20%7C%203.14-blue.svg)](https://www.python.org/)
[![WebRTC](https://img.shields.io/badge/WebRTC-aiortc-brightgreen.svg)](https://github.com/aiortc/aiortc)
[![Codec](https://img.shields.io/badge/Codec-Opus%2048kHz%20Stereo-purple.svg)](https://opus-codec.org/)
[![FastAPI](https://img.shields.io/badge/Signaling-FastAPI%20%2F%20WebSocket-009688.svg)](https://fastapi.tiangolo.com/)
[![Tests](https://img.shields.io/badge/Tests-10%20passed-success.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Release](https://img.shields.io/badge/Release-v0.2.0-blue.svg)]()

---

## ⚡ What is AirBridge?

**AirBridge** turns your iPhone and AirPods into wireless headphones for your Windows PC without purchasing Bluetooth dongles, proprietary receivers, or installing native apps on your phone.

Using Windows **WASAPI Loopback**, a central **Audio Pipeline** with monotonic sample-based PTS, and standard **WebRTC audio tracks**, AirBridge captures everything playing on your Windows PC (YouTube, Spotify, games, meetings) and streams it with **sub-50ms latency** directly to Safari on iOS.

```text
 ┌────────────────────────────────────────────────────────┐
 │                      WINDOWS PC                        │
 │  Master Sound Output                                   │
 │       │                                                │
 │       ▼                                                │
 │  AudioCapture (WASAPI Loopback via SoundCard)          │
 │       │                                                │
 │       ▼                                                │
 │  AudioPipeline (Monotonic PTS, Bounded Queue, Drop)    │
 │       │                                                │
 │       ├─────────────────────────┬──────────────────────┤
 │       ▼                         ▼                      ▼
 │  Peer Track A (Chrome)     Peer Track B (iPhone)   Peer Track C
 │       │                         │                      │
 └───────┼─────────────────────────┼──────────────────────┼───────┘
         │                         │                      │
         │  Local Wi-Fi Network (LAN)                     │
         ▼                         ▼                      ▼
 ┌─────────────────┐       ┌─────────────────┐    ┌───────────────┐
 │ Desktop Chrome  │       │  iPhone Safari  │    │ Other Devices │
 │ 🔊 Local Audio  │       │  🎧 AirPods     │    │               │
 └─────────────────┘       └─────────────────┘    └───────────────┘
```

---

## ✨ Features

- **Zero iOS Apps:** Connect straight from Mobile Safari. No TestFlight, App Store downloads, or sideloading needed.
- **Ultra-Low Latency:** Typically **20 ms – 40 ms** on standard home Wi-Fi networks (imperceptible delay for videos and casual games).
- **Multi-Peer Architecture:** ONE capture engine feeds multiple simultaneous listeners without duplicating WASAPI loops or CPU overhead.
- **Low-Latency Drop-Oldest Buffer:** Prioritizes real-time sync over backlog buildup. Drops stale frames if Wi-Fi fluctuates.
- **Monotonic Sample-Based PTS:** Audio Presentation Time Stamps advance by exactly 960 samples per frame, eliminating clock jitter and timestamp drift.
- **Connection Lifecycle Management:** Auto-starts capture when the first client arrives; pauses capture when all clients disconnect to save CPU.
- **In-Browser Diagnostics & Visualizer:** Real-time frequency visualizer, packet counts, bitrate, and jitter statistics.

---

## 🚀 Quick Start

### 1. Prerequisites

- **Windows 10 or Windows 11**
- **Python 3.10+** (tested on Python 3.12 & 3.14)
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

### 3. Run the Server

```powershell
python -u server/webrtc_server.py
```

Upon launching, AirBridge prints the active local network address:

```text
==================================================================
  AirBridge v0.2.0 - Audio Pipeline & Connection Core
==================================================================
  Audio:
    Sample rate:  48000 Hz
    Channels:     2 (Stereo)
    Frame:        20 ms / 960 samples
    Buffer:       max 15 frames (~300 ms, drop-oldest)
  Network:
    Listening:    0.0.0.0:8000
    Local PC URL: http://localhost:8000
    LAN URL:      http://192.168.0.105:8000
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

## 🧪 Running the Test Suite

The test suite validates the core audio pipeline, bounded buffer, monotonic PTS, multi-subscriber dispatch, and connection management without requiring physical sound hardware:

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

Output:
```text
tests/test_audio_pipeline.py::test_audio_frame_data_validation PASSED
tests/test_audio_pipeline.py::test_monotonic_pts_progression PASSED
tests/test_audio_pipeline.py::test_bounded_buffer_and_oldest_frame_drop PASSED
tests/test_audio_pipeline.py::test_underrun_detection_and_silence_fallback PASSED
tests/test_audio_pipeline.py::test_multi_subscriber_fanout PASSED
tests/test_audio_timing.py::test_frame_samples_math PASSED
tests/test_audio_timing.py::test_pts_monotonic_integrity_under_load PASSED
tests/test_connection_manager.py::test_client_registration_and_removal PASSED
tests/test_connection_manager.py::test_stale_connection_cleanup PASSED
tests/test_webrtc_pipeline.py::test_pipeline_to_webrtc_track_integration PASSED
============================= 10 passed in 1.10s ==============================
```

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
│   ├── config.py            # Global audio, network & buffer configuration
│   ├── models.py            # AudioFrameData & PipelineMetrics models
│   ├── audio_capture.py     # Windows WASAPI loopback capture (SoundCard)
│   ├── audio_pipeline.py    # Central audio pipeline, monotonic PTS & fanout
│   ├── connection_manager.py# Client sessions, WebRTC lifecycles & capture trigger
│   ├── opus_encoder.py      # Standalone libopus compression verification
│   └── webrtc_server.py     # Thin FastAPI server & WebSocket signaling
├── web/
│   └── index.html           # Web receiver (WebRTC, audio visualizer, stats)
├── tests/
│   ├── test_audio_pipeline.py
│   ├── test_audio_timing.py
│   ├── test_connection_manager.py
│   └── test_webrtc_pipeline.py
├── requirements.txt         # Production dependencies
├── .gitignore               # Git ignore rules
└── README.md                # Documentation & Architecture
```

---

## 🛠️ Architecture & Pipeline Details

| Stage | Module | Responsibility |
|---|---|---|
| **Capture** | `server/audio_capture.py` | WASAPI loopback via SoundCard, reads 20 ms blocks @ 48 kHz stereo s16 |
| **Pipeline** | `server/audio_pipeline.py` | Monotonic sample-based PTS progression, bounded subscriber ring buffers |
| **Buffering** | `server/audio_pipeline.py` | Max 15 frames (~300 ms). Drops oldest stale frames if Wi-Fi lags |
| **Sessions** | `server/connection_manager.py` | Auto-starts capture on first client; auto-stops on zero clients |
| **Encoding** | `aiortc` (libopus) | Broadcast-quality Opus speech/audio profile @ 96 kbps |
| **Transport** | WebRTC (SRTP / UDP) | Direct peer-to-peer RTP transmission with sub-frame packaging |
| **Signaling** | FastAPI + WebSockets | Low-overhead JSON SDP Offer/Answer and ICE candidate exchange |
| **Playback** | HTML5 Audio + Web Audio | Low-latency audio rendering with user-gesture unlock handling |

---

## 🗺️ Roadmap

- [x] **v0.1.0:** Initial working MVP (WASAPI capture, Opus encoding, WebRTC, Safari support).
- [x] **v0.2.0 (Current):** **Audio Pipeline & Connection Core** (Multi-peer fanout, monotonic PTS, bounded buffers, decoupled capture, unit & integration test suite).
- [ ] **v0.3.0:** **QR Code Pairing** — scan directly from PC screen to open on iPhone without typing IP addresses.
- [ ] **v0.4.0:** **Windows System Tray App & Standalone `.exe`** — zero-python executable running in the system tray with auto-start.
- [ ] **v0.5.0:** **Adaptive Bitrate & Diagnostics Dashboard** — dynamic bitrate adjustment based on live jitter & packet loss.

---

## 📄 License

This project is open-source under the [MIT License](LICENSE).
