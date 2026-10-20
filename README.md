# AirBridge 📻

> **Stream Windows PC audio to your iPhone or AirPods over Wi-Fi — no iOS app required.**  
> Open the receiver in Safari, connect, and listen.

[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue.svg)](https://www.microsoft.com/windows)
[![Python](https://img.shields.io/badge/Python-3.10%20%7C%203.12%20%7C%203.14-blue.svg)](https://www.python.org/)
[![WebRTC](https://img.shields.io/badge/WebRTC-aiortc-brightgreen.svg)](https://github.com/aiortc/aiortc)
[![Signaling](https://img.shields.io/badge/Signaling-FastAPI%20%2F%20WebSocket-009688.svg)](https://fastapi.tiangolo.com/)
[![CI](https://github.com/serozr/AirBridge/actions/workflows/ci.yml/badge.svg)](https://github.com/serozr/AirBridge/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/Tests-43%20passed-success.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## ⚡ What is AirBridge?

AirBridge captures Windows system output through WASAPI loopback and streams live audio to Safari on iOS over the local Wi-Fi network using WebRTC. No native iOS app, sideloading, or Bluetooth dongle is required.

The pipeline uses Windows **WASAPI Loopback**, a central **Audio Pipeline** with monotonic sample-based PTS, a **Security Layer** protecting signaling with pairing PINs and tokens, and standard **WebRTC audio tracks** (`aiortc`), streaming system audio with low latency directly to Safari on iOS and desktop browsers.

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
         │  Local Wi-Fi Network (LAN + Security Token/PIN)
         ▼                         ▼                      ▼
 ┌─────────────────┐       ┌─────────────────┐    ┌───────────────┐
 │ Desktop Chrome  │       │  iPhone Safari  │    │ Other Devices │
 │ 🔊 Local Audio  │       │  🎧 AirPods     │    │               │
 └─────────────────┘       └─────────────────┘    └───────────────┘
```

---

## ✨ Features

- **Zero iOS Apps:** Connect straight from Mobile Safari. No TestFlight, App Store downloads, or sideloading needed.
- **Security Layer (v0.3.0):** Pairing PIN verification, cryptographically secure session tokens, IP brute-force rate-limiting, and CSWSH Origin validation.
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

The test suite validates the core audio pipeline, bounded buffer, monotonic PTS, multi-subscriber dispatch, connection management, and the security layer without requiring physical sound hardware:

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
tests/test_connection_manager.py::test_rapid_connect_disconnect_race_condition PASSED
tests/test_connection_manager.py::test_silent_active_webrtc_client_not_pruned_as_stale PASSED
tests/test_connection_manager.py::test_capture_error_from_background_thread_dispatches_safely PASSED
tests/test_connection_manager.py::test_flapping_capture_does_not_reset_retries PASSED
tests/test_connection_manager.py::test_sustained_stability_threshold_resets_retry_counter PASSED
tests/test_connection_manager.py::test_stop_timeout_reports_error_health PASSED
tests/test_connection_manager.py::test_recovery_cancelled_when_last_client_leaves PASSED
tests/test_connection_manager.py::test_heartbeat_updates_last_seen PASSED
tests/test_multi_peer_signaling.py::test_two_simultaneous_webrtc_peers_receive_audio PASSED
tests/test_security.py::test_valid_token_authentication PASSED
tests/test_security.py::test_invalid_token_rejected PASSED
tests/test_security.py::test_missing_token_rejected PASSED
tests/test_security.py::test_expired_token_rejected PASSED
tests/test_security.py::test_valid_pin_generates_token PASSED
tests/test_security.py::test_invalid_pin_rejected PASSED
tests/test_security.py::test_rate_limit PASSED
tests/test_security.py::test_rate_limit_reset_on_success PASSED
tests/test_security.py::test_session_rotation PASSED
tests/test_security.py::test_origin_validation PASSED
tests/test_security.py::test_unauthenticated_websocket_rejected PASSED
tests/test_security.py::test_invalid_token_websocket_rejected PASSED
tests/test_security.py::test_unauthorized_origin_websocket_rejected PASSED
tests/test_security.py::test_valid_token_websocket_accepted PASSED
tests/test_security.py::test_api_auth_endpoint_full_flow PASSED
tests/test_webrtc_pipeline.py::test_pipeline_to_webrtc_track_integration PASSED
tests/test_webrtc_pipeline.py::test_webrtc_track_consecutive_underruns_and_recovery PASSED
============================= 43 passed in 4.78s ==============================
```

> [!NOTE]
> The automated test suite verifies thread concurrency, error dispatching, sustained capture stability, cryptographic authentication, and multi-peer WebRTC streaming in local environments and GitHub Actions CI. Live acoustic streaming over Wi-Fi to physical iPhone Safari and AirPods was confirmed during MVP milestones.

---

## 🔐 Security Architecture (v0.3.0)

AirBridge incorporates a dedicated zero-trust local network security layer:

- **Pairing PIN Verification:** A 6-digit numeric PIN generated on each server run protects against unauthorized LAN eavesdropping. Constant-time comparison (`hmac.compare_digest`) prevents timing attacks.
- **Cryptographic Session Tokens:** Authenticated peers receive 32-byte URL-safe cryptographic tokens. WebSocket signaling and SDP offer/answer exchanges strictly reject unauthenticated connections (HTTP 401 / WS 4401).
- **IP Brute-Force Rate Limiting:** Failed PIN verification attempts are tracked per IP address. Exceeding 3 failed attempts temporarily locks out the offending address (HTTP 429).
- **Origin & Host Validation:** Prevents Cross-Site WebSocket Hijacking (CSWSH) by restricting signaling requests to private RFC-1918 networks, localhost, `.local` mDNS, and whitelisted origins.

If necessary to permit local inbound traffic across Windows Defender Firewall, run PowerShell as Administrator:

```powershell
New-NetFirewallRule -DisplayName "AirBridge WebRTC (TCP 8000)" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow -Profile Private
```

---

## 📁 Repository Structure

```text
AirBridge/
├── server/
│   ├── config.py            # Global audio, network & security configuration
│   ├── models.py            # AudioFrameData & PipelineMetrics models
│   ├── audio_capture.py     # Windows WASAPI loopback capture (SoundCard)
│   ├── audio_pipeline.py    # Central audio pipeline, monotonic PTS & fanout
│   ├── connection_manager.py# Client sessions, WebRTC lifecycles & capture trigger
│   ├── opus_encoder.py      # Standalone libopus compression verification
│   ├── security.py          # Pairing PIN, session tokens, rate limiting & origin check
│   └── webrtc_server.py     # Thin FastAPI server & WebSocket signaling
├── web/
│   └── index.html           # Web receiver (WebRTC, audio visualizer, PIN dialog)
├── scripts/
│   └── benchmark_pipeline.py# Empirical latency & throughput benchmark suite
├── tests/
│   ├── test_audio_capture.py
│   ├── test_audio_pipeline.py
│   ├── test_audio_timing.py
│   ├── test_connection_manager.py
│   ├── test_multi_peer_signaling.py
│   ├── test_security.py     # 15 authentication & security tests
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
| **Security** | `server/security.py` | Pairing PIN, cryptographic tokens, rate limiting, and origin validation |
| **Sessions** | `server/connection_manager.py` | Auto-starts capture on first client; auto-stops on zero clients |
| **WebRTC Track** | `server/webrtc_server.py` | `PipelineAudioTrack` yields `av.AudioFrame` directly to WebRTC audio engine |
| **Signaling** | FastAPI + WebSockets | Low-overhead JSON SDP Offer/Answer and ICE candidate exchange |
| **Playback** | HTML5 Audio + Web Audio | Low-latency audio rendering with user-gesture unlock handling |

---

## 📊 Performance & Latency Benchmarks

AirBridge includes an automated benchmark suite (`scripts/benchmark_pipeline.py`) measuring Opus encoder throughput, ring buffer latency, and multi-subscriber scaling on Windows:

```powershell
python scripts/benchmark_pipeline.py
```

### 1. libopus Encoding Latency (48 kHz Stereo @ 96 kbps Target)

| Audio Content | Avg Encode Time | 95th Percentile | 99th Percentile | Real-Time Factor (RTF) | Speedup vs Real-Time | Effective Bitrate |
|:---|:---:|:---:|:---:|:---:|:---:|:---:|
| **Sine Wave (440 Hz)** | **195.9 μs** (0.196 ms) | 250.6 μs | 290.5 μs | **0.98%** | **102x faster** | 96.4 kbps |
| **Complex Audio / Music** | **160.8 μs** (0.161 ms) | 224.7 μs | 280.1 μs | **0.80%** | **124x faster** | 96.4 kbps |
| **Silence / Idle** | **104.9 μs** (0.105 ms) | 219.7 μs | 318.3 μs | **0.52%** | **191x faster** | 1.2 kbps |

*At 20 ms per frame, libopus processes audio in under 200 microseconds, consuming less than 1% of the available real-time audio budget.*

### 2. AudioPipeline Multi-Subscriber Fanout & Ring Buffer Latency

| Active Listeners | Fanout Publish Latency (Mean) | 95th Percentile | Buffer Retrieval Latency | Monotonic PTS Accuracy |
|:---:|:---:|:---:|:---:|:---:|
| **1 peer**  | **4.47 μs** (0.004 ms) | 5.20 μs | 1.47 μs | **100% strictly +960 samples / 20.00 ms** |
| **2 peers** | **4.74 μs** (0.005 ms) | 5.40 μs | 1.21 μs | **100% strictly +960 samples / 20.00 ms** |
| **5 peers** | **6.63 μs** (0.007 ms) | 7.20 μs | 1.08 μs | **100% strictly +960 samples / 20.00 ms** |
| **10 peers**| **8.91 μs** (0.009 ms) | 9.40 μs | 1.03 μs | **100% strictly +960 samples / 20.00 ms** |

### 3. Server Processing Overhead & CPU Budget (per 20 ms audio frame)

```text
 ┌────────────────────────────────────────────────────────────────────────┐
 │                      20.000 ms Audio Frame Window                      │
 │ █ Server Processing: 0.282 ms (1.41%)                                  │
 │ ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ CPU Headroom: 19.718 ms (98.59%)    │
 └────────────────────────────────────────────────────────────────────────┘
```

| Component | Mean Duration | % of 20 ms Slot |
|:---|:---:|:---:|
| **WASAPI Loopback Capture Window** | 20.000 ms | 100.00% |
| **AudioPipeline Fanout & Monotonic PTS** | 0.004 ms | 0.02% |
| **Ring Buffer Deque Retrieval** | 0.001 ms | 0.01% |
| **libopus Compression (Stereo @ 96k)** | 0.196 ms | 0.98% |
| **WebRTC RTP Packetization & Queue** | ~0.080 ms | 0.40% |
| **TOTAL SERVER OVERHEAD ADDED** | **0.282 ms** | **1.41%** |
| **Available CPU Headroom** | **19.718 ms** | **98.59%** |

---

## 🗺️ Roadmap

- [x] **v0.1.0:** Initial working MVP (WASAPI capture, Opus encoding, WebRTC, Safari support).
- [x] **v0.2.0:** **Audio Pipeline & Connection Core** (Multi-peer fanout, monotonic PTS, bounded buffers, decoupled capture, sustained stability validation, 28/28 test suite, GitHub Actions CI, empirical benchmarks).
- [x] **v0.3.0:** **Security Layer** (Pairing PIN verification, cryptographic session tokens, IP rate limiting & brute-force lockout, Origin/Host validation, 43 automated tests).
- [ ] **v0.4.0:** **QR Code Pairing** — scan directly from PC screen to open on iPhone and auto-authenticate without typing IP or PIN.
- [ ] **v0.5.0:** **Windows System Tray App & Standalone `.exe`** — zero-python executable running in the system tray with auto-start.
- [ ] **v0.6.0:** **Adaptive Bitrate & Diagnostics Dashboard** — dynamic bitrate adjustment based on live jitter & packet loss.

---

## 📄 License

This project is open-source under the [MIT License](LICENSE).
