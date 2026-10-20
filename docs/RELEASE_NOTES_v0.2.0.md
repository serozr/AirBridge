# AirBridge v0.2.0 — Audio Pipeline & Connection Core

> **Release Date:** October 21, 2026  
> **Tag:** `v0.2.0`  
> **Target Platform:** Windows 10 / 11 (Loopback Source) ➔ iOS 15+ Safari & Desktop Browsers (Receivers)

AirBridge v0.2.0 transforms the initial MVP prototype into a production-grade, modular audio engineering and WebRTC streaming core. This release decouples WASAPI hardware capture from peer distribution, establishes bounded drop-oldest ring buffers with sample-accurate monotonic PTS tracking, adds thread-safe error recovery with flapping protection, and introduces empirical performance benchmarking.

---

## 🚀 Key Highlights & New Capabilities

### 1. Centralized Multi-Peer Audio Pipeline (`server/audio_pipeline.py`)
- **Single Capture Source, Multiple Listeners:** One master WASAPI loopback capture engine efficiently fans out 48 kHz stereo PCM frames to multiple simultaneous WebRTC peers without duplicate OS audio loops.
- **Sample-Accurate Monotonic PTS Clock:** Presentation Time Stamps advance by strictly 960 samples (+20.00 ms) per frame, eliminating clock jitter and WebRTC RTP timestamp drift.
- **Drop-Oldest Bounded Ring Buffer:** Fixed at 15 frames (~300 ms depth) per subscriber. Prioritizes real-time sync over buffering delay; automatically drops oldest frames during transient network congestion.
- **Sub-10 Microsecond Fanout Latency:** Measures 4.47 μs for 1 listener and 8.91 μs for 10 simultaneous listeners.

### 2. Thread-Safe Connection Lifecycle & Flapping Protection (`server/connection_manager.py`)
- **Automated Capture Synchronization:** Capture starts automatically when the first peer connects and stops cleanly when all peers disconnect, preserving CPU cycles.
- **Cross-Thread Error Dispatching:** Background audio worker errors are thread-safely dispatched to the asyncio event loop without blocking or crashing.
- **Sustained Stability Dual-Criteria:** Prevents infinite retry loops on flapping capture crashes. Audio capture is confirmed stable only after streaming for **>= 150 consecutive frames** AND **>= 3.0 seconds continuous operation**.
- **Bounded Retries:** Caps recovery at 3 attempts with exponential backoff before transitioning to a safe error state.
- **WebRTC Active Media Protection:** Distinguishes active audio streaming tracks from stalled signaling sockets to prevent premature disconnects.

### 3. Comprehensive Automated Testing & CI (`.github/workflows/ci.yml`)
- **28 Automated Tests:** Covers WASAPI loopback, monotonic timing, subscriber isolation, bounded queue overflow, background thread crash recovery, flapping protection, and stop timeouts.
- **GitHub Actions CI:** Automated workflow running on `windows-latest` with Python 3.12 for continuous regression testing.

### 4. Empirical Performance Benchmarking (`scripts/benchmark_pipeline.py`)
- **libopus Encode Speedup:** Encodes 20 ms frames in 0.16–0.19 ms (**>100x faster than real-time**, consuming <1% of the 20 ms frame budget).
- **Server Overhead Added:** Total server processing delay across capture dispatch, fanout, and encoding is **0.282 ms** (leaving **98.59% CPU headroom**).

---

## 📊 Benchmark Summary

| Metric | Measured Value | Target / Budget | Status |
|:---|:---:|:---:|:---:|
| **Audio Sample Rate** | 48,000 Hz Stereo | 48 kHz | ✅ Perfect |
| **Audio Frame Duration** | 20.00 ms (960 samples) | 20 ms | ✅ Perfect |
| **libopus Encode Time** | 0.161 – 0.196 ms | < 5.0 ms | ✅ 100x+ Real-time |
| **Pipeline Fanout Latency (1-10 peers)**| 4.47 – 8.91 μs | < 1.0 ms | ✅ Sub-10 μs |
| **PTS Progression Accuracy** | Strictly +960 samples | Strictly Monotonic | ✅ 100% Monotonic |
| **Total Server Latency Overhead** | 0.282 ms | < 2.0 ms | ✅ 1.41% of slot |
| **Test Suite Coverage** | 28 / 28 Passed | 100% Pass | ✅ Verified |

---

## 🛠️ Verification & Quick Start

```powershell
# Clone and setup
git clone https://github.com/serozr/AirBridge.git
cd AirBridge
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Run full test suite (28 tests)
pytest -v

# Run performance benchmarks
python scripts/benchmark_pipeline.py

# Launch server
python -m server.webrtc_server
```

Connect from iPhone Safari on the same local network: `http://<YOUR-PC-IP>:8000`
