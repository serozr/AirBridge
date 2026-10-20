# AirBridge 📻

> **Stream Windows PC audio to your iPhone or AirPods over Wi-Fi — no iOS app required.**  
> Open the receiver in Safari, connect, and listen.

[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue.svg)](https://www.microsoft.com/windows)
[![Python](https://img.shields.io/badge/Python-3.12%20recommended-blue.svg)](https://www.python.org/)
[![WebRTC](https://img.shields.io/badge/WebRTC-aiortc-brightgreen.svg)](https://github.com/aiortc/aiortc)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Open source](https://img.shields.io/badge/open%20source-MIT-blue.svg)](LICENSE)

## What is AirBridge?

AirBridge captures Windows system output through WASAPI loopback and sends live audio to Safari on an iPhone over the same local Wi-Fi network using WebRTC. No native iOS app is required.

The maintainer has manually verified live audio on a Windows PC, in a desktop browser, and on an iPhone Safari receiver over local Wi-Fi. End-to-end latency has **not** been benchmarked yet.

## How it works

```text
Windows system audio
       ↓
WASAPI loopback (SoundCard)
       ↓
48 kHz stereo PCM frames
       ↓
aiortc / PyAV WebRTC audio track
       ↓
WebRTC over local Wi-Fi
       ↓
iPhone Safari → AirPods / speaker
```

## Features

- No native iOS app; use Safari.
- Captures Windows default output audio.
- WebRTC audio transport with a bounded queue that drops older frames to limit backlog.
- Browser diagnostics, audio visualizer, packet, bitrate, jitter and RTT displays.

## Requirements

- Windows 10 or 11, 64-bit.
- Python 3.12 recommended; other versions should be treated as unverified until tested.
- PC and iPhone connected to the same trusted Wi-Fi network.

## Quick start

```powershell
git clone https://github.com/serozr/AirBridge.git
cd AirBridge
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -u server/webrtc_server.py
```

Open the LAN URL printed in the terminal using Safari on your iPhone, then tap **Connect & Stream**. Play audio on the PC.

If PowerShell blocks virtual-environment activation, use the virtual environment interpreter directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -u server/webrtc_server.py
```

## Network and firewall notes

Use AirBridge only on a trusted local network. The HTTP receiver page and WebSocket signaling do not currently provide application-level authentication. **Do not expose port 8000 to the public internet.**

If your iPhone cannot open the receiver page, confirm both devices are on the same non-guest Wi-Fi network. If necessary, run PowerShell as Administrator:

```powershell
New-NetFirewallRule -DisplayName "AirBridge WebRTC (TCP 8000)" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow -Profile Private
```

## Repository structure

```text
AirBridge/
├── server/
│   ├── audio_capture.py
│   ├── opus_encoder.py
│   └── webrtc_server.py
├── web/
│   └── index.html
├── requirements.txt
├── LICENSE
└── README.md
```

## Validation status

- [x] Windows loopback capture produces live PCM frames.
- [x] Standalone Opus encoder test produces real Opus packets.
- [x] Live WebRTC audio plays in a desktop browser.
- [x] Live Windows audio is heard on iPhone Safari over the same Wi-Fi network.
- [ ] Automated tests and repeatable end-to-end latency benchmarks.
- [ ] Compatibility matrix across Windows, Python and iOS versions.
- [ ] Demo GIF/video.

These are manual maintainer tests, not CI-certified compatibility guarantees.

## Important implementation note

The standalone `server/opus_encoder.py` demonstrates raw libopus encoding. The primary streaming server uses aiortc/PyAV audio frames and WebRTC's negotiated audio codec path; the standalone encoder is not directly connected to the main server.

## Roadmap

- [ ] QR-code pairing to open the correct LAN URL without typing an IP address.
- [ ] Automated tests and repeatable latency benchmarking.
- [ ] Windows tray app and packaged executable.
- [ ] Adaptive bitrate and further audio tuning based on measurements.

## Issues and feedback

Bug reports and setup feedback are welcome via [GitHub Issues](https://github.com/serozr/AirBridge/issues). Include your Windows/Python/iOS versions and relevant sanitized diagnostics. Do not attach private audio recordings or sensitive network details.

## License

MIT — see [LICENSE](LICENSE).
