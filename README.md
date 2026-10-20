# AirBridge 📻

> **Stream Windows PC audio to your iPhone or AirPods over Wi-Fi — no iOS app required.**  
> Open the receiver in Safari, connect, and listen.

[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue.svg)](https://www.microsoft.com/windows)
[![Python](https://img.shields.io/badge/Python-3.10%20%7C%203.12%20%7C%203.14-blue.svg)](https://www.python.org/)
[![WebRTC](https://img.shields.io/badge/WebRTC-aiortc-brightgreen.svg)](https://github.com/aiortc/aiortc)
[![Signaling](https://img.shields.io/badge/Signaling-FastAPI%20%2F%20WebSocket-009688.svg)](https://fastapi.tiangolo.com/)
[![Tests](https://img.shields.io/badge/Tests-21%20passed-success.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## ⚡ What is AirBridge?

AirBridge captures Windows system output through WASAPI loopback and streams live audio to Safari on iOS over the local Wi-Fi network using WebRTC. No native iOS app, sideloading, or Bluetooth dongle is required.

The pipeline uses Windows **WASAPI Loopback**, a central **Audio Pipeline** with monotonic sample-based PTS, and standard **WebRTC audio tracks** (`aiortc`), streaming system audio with low latency directly to Safari on iOS and desktop browsers.

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
- **Multi-Peer Architecture:** ONE capture engine feeds multiple simultaneous listeners without duplicating WASAPI loops or CPU overhead.
- **Low-Latency Drop-Oldest Buffer:** Prioritizes real-time sync over backlog buildup. Drops stale frames if network jitter occurs.
- **Monotonic Sample-Based PTS:** Audio Presentation Time Stamps advance by exactly 960 samples per frame, eliminating clock jitter and timestamp drift.
- **Connection Lifecycle Management:** Auto-starts capture when the first client connects; pauses capture when all clients disconnect to preserve CPU.
- **In-Browser Diagnostics & Visualizer:** Real-time frequency visualizer, packet counts, bitrate, and jitter statistics.

---

## 🚀 Quick Start

### 1. Prerequisites

- **Windows 10 or Windows 11 (64-bit)**
- **Python 3.10+** (tested on 3.12 and 3.14)
- **iPhone / iPad** connected to the same trusted Wi-Fi network

### 2. Installation

```powershell
git clone https://github.com/serozr/AirBridge.git
cd AirBridge

python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

If PowerShell blocks virtual-environment activation, you can run directly with the virtualenv interpreter:
```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -u server/webrtc_server.py
```

### 3. Run the Server

```powershell
python -u server/webrtc_server.py
```

Upon launching, AirBridge displays the detected local network address:

```text
==================================================================
  AirBridge v0.2.0 - Audio Pipeline & Connection Core
==================================================================
  Audio:
    Sample rate:  48000 Hz
    Channels:     2 (Stereo)
    Frame:        20 ms / 960 samples
    Buffer policy: max 15 frames (~300 ms, drop-oldest)
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
2. Open **Safari** on your iPhone and navigate to the LAN URL displayed in the console (e.g. `http://192.168.0.105:8000`).
3. Tap **▶ Connect & Stream**.
4. Play any audio or video on your PC (YouTube, Spotify, etc.) — audio plays in your AirPods!

---

## 🧪 Running the Test Suite

The test suite validates the core audio pipeline, bounded buffer, monotonic PTS, multi-subscriber dispatch, and connection management without requiring physical sound hardware:

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

Output:
```text
tests/test_audio_capture.py::test_audio_capture_start_stop_idempotent PASSED
tests/test_audio_capture.py::test_audio_capture_frame_delivery PASSED
tests/test_audio_capture.py::test_audio_capture_error_handling_and_retry PASSED
tests/test_audio_pipeline.py::test_audio_frame_data_validation PASSED
tests/test_audio_pipeline.py::test_monotonic_pts_progression PASSED
tests/test_audio_pipeline.py::test_invalid_buffer_size_rejected PASSED
tests/test_audio_pipeline.py::test_duplicate_subscriber_id_rejected PASSED
tests/test_audio_pipeline.py::test_bounded_buffer_and_oldest_frame_drop PASSED
tests/test_audio_pipeline.py::test_consecutive_underruns_advance_pts PASSED
tests/test_audio_pipeline.py::test_frame_arriving_during_wait PASSED
tests/test_audio_pipeline.py::test_dropped_frames_followed_by_silence PASSED
tests/test_audio_pipeline.py::test_subscriber_buffer_isolation PASSED
tests/test_audio_timing.py::test_frame_samples_math PASSED
tests/test_audio_timing.py::test_pts_monotonic_integrity_under_load PASSED
tests/test_connection_manager.py::test_client_registration_and_removal PASSED
tests/test_connection_manager.py::test_duplicate_client_id_rejected PASSED
tests/test_connection_manager.py::test_concurrent_client_removal PASSED
tests/test_connection_manager.py::test_stale_connection_cleanup PASSED
tests/test_multi_peer_signaling.py::test_two_simultaneous_webrtc_peers_receive_audio PASSED
tests/test_webrtc_pipeline.py::test_pipeline_to_webrtc_track_integration PASSED
tests/test_webrtc_pipeline.py::test_webrtc_track_consecutive_underruns_and_recovery PASSED
============================= 21 passed in 1.70s ==============================
```

---

## 🛡️ Windows Firewall Notice

Use AirBridge only on a trusted local network. The HTTP receiver page and WebSocket signaling do not currently provide application-level authentication. **Do not expose port 8000 to the public internet.**

If your iPhone cannot open the receiver page, confirm both devices are on the same non-guest Wi-Fi network. If necessary, run PowerShell as Administrator:

```powershell
New-NetFirewallRule -DisplayName "AirBridge WebRTC (TCP 8000)" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow -Profile Private
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
│   ├── test_audio_capture.py
│   ├── test_audio_pipeline.py
│   ├── test_audio_timing.py
│   ├── test_connection_manager.py
│   ├── test_multi_peer_signaling.py
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
| **Buffering** | `server/audio_pipeline.py` | Max 15 frames (~300 ms, drop-oldest policy) prevents backlog buildup |
| **Sessions** | `server/connection_manager.py` | Auto-starts capture on first client; auto-stops on zero clients |
| **WebRTC Track** | `server/webrtc_server.py` | `PipelineAudioTrack` yields `av.AudioFrame` directly to WebRTC audio engine |
| **Signaling** | FastAPI + WebSockets | Low-overhead JSON SDP Offer/Answer and ICE candidate exchange |
| **Playback** | HTML5 Audio + Web Audio | Low-latency audio rendering with user-gesture unlock handling |

> [!NOTE]
> The standalone `server/opus_encoder.py` demonstrates raw libopus encoding. The primary streaming server feeds PCM frames to `aiortc`/PyAV, which handles Opus compression within WebRTC's negotiated RTP media transport.

---

## 🗺️ Roadmap

- [x] **v0.1.0:** Initial working MVP (WASAPI capture, Opus encoding, WebRTC, Safari support).
- [ ] **v0.2.0 (In Progress):** **Audio Pipeline & Connection Core** (Multi-peer fanout, monotonic PTS, bounded buffers, decoupled capture, unit & integration test suite).
- [ ] **v0.3.0:** **QR Code Pairing** — scan directly from PC screen to open on iPhone without typing IP addresses.
- [ ] **v0.4.0:** **Windows System Tray App & Standalone `.exe`** — zero-python executable running in the system tray with auto-start.
- [ ] **v0.5.0:** **Adaptive Bitrate & Diagnostics Dashboard** — dynamic bitrate adjustment based on live jitter & packet loss.

---

## 📄 License

This project is open-source under the [MIT License](LICENSE).
