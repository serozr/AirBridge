"""
AirBridge - Server-side WebRTC Streaming Service

Pipeline:
    Windows Master Output
    -> WASAPI Loopback (SoundCard)
    -> 48000 Hz, Stereo, 20ms frames (960 samples/channel)
    -> s16 PCM AudioFrame (av)
    -> aiortc Opus Encoding & SRTP Transport
    -> Browser HTML5 WebRTC AudioReceiver
"""

import asyncio
import fractions
import json
import logging
import os
import threading
from typing import Set, Dict, Optional

import av
import numpy as np
import soundcard as sc
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration
from aiortc.mediastreams import AudioStreamTrack
from aiortc.sdp import candidate_from_sdp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# Configure clean logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("AirBridge.WebRTC")

SAMPLE_RATE = 48000
CHANNELS = 2
FRAME_DURATION_MS = 20
FRAME_SIZE = int(SAMPLE_RATE * (FRAME_DURATION_MS / 1000.0))  # 960 samples per channel
BYTES_PER_FRAME_S16 = FRAME_SIZE * CHANNELS * 2              # 960 * 2 * 2 = 3840 bytes


class WASAPIBroadcaster:
    """
    Manages a single background thread that records WASAPI loopback audio
    from the Windows default output device and dispatches PCM frames to all
    active WebRTC client tracks.
    """

    def __init__(self) -> None:
        self._tracks: Set["WASAPIAudioTrack"] = set()
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def register(self, track: "WASAPIAudioTrack") -> None:
        with self._lock:
            self._tracks.add(track)
            logger.info("Registered new audio track. Total active listeners: %d", len(self._tracks))
            if not self._running:
                self._running = True
                self._thread = threading.Thread(target=self._capture_loop, daemon=True)
                self._thread.start()

    def unregister(self, track: "WASAPIAudioTrack") -> None:
        with self._lock:
            self._tracks.discard(track)
            logger.info("Unregistered audio track. Remaining active listeners: %d", len(self._tracks))
            if not self._tracks:
                self._running = False

    def _capture_loop(self) -> None:
        logger.info("WASAPI background capture thread started.")
        speaker = sc.default_speaker()
        loopback = sc.get_microphone(speaker.name, include_loopback=True)
        logger.info("Capturing from default output: %s [Loopback]", speaker.name)

        try:
            with loopback.recorder(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                blocksize=FRAME_SIZE,
            ) as recorder:
                while self._running:
                    # Synchronous read of 20ms (960 frames)
                    data = recorder.record(numframes=FRAME_SIZE)
                    data = np.asarray(data, dtype=np.float32)

                    # Convert float32 [-1.0, 1.0] to int16 [-32768, 32767]
                    data_s16 = np.clip(data * 32767.0, -32768.0, 32767.0).astype(np.int16)
                    pcm_bytes = data_s16.tobytes()

                    # Broadcast to all active tracks
                    with self._lock:
                        for track in list(self._tracks):
                            track.push_pcm(pcm_bytes)

        except Exception as e:
            logger.error("Exception in WASAPI capture loop: %s", e)
        finally:
            logger.info("WASAPI background capture thread stopped.")


broadcaster = WASAPIBroadcaster()


class WASAPIAudioTrack(AudioStreamTrack):
    """
    WebRTC AudioStreamTrack delivering 48kHz stereo 20ms frames to aiortc.
    Frames are passed to aiortc's native libopus encoder for WebRTC transmission.
    """

    kind = "audio"

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self._loop = loop
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=15)  # Max ~300ms buffer
        self._pts = 0
        self._time_base = fractions.Fraction(1, SAMPLE_RATE)
        broadcaster.register(self)

    def push_pcm(self, pcm_bytes: bytes) -> None:
        """Called from WASAPIBroadcaster capture thread."""
        def _enqueue():
            if self._queue.full():
                try:
                    self._queue.get_nowait()  # Drop oldest frame to maintain ultra-low latency
                except asyncio.QueueEmpty:
                    pass
            try:
                self._queue.put_nowait(pcm_bytes)
            except asyncio.QueueFull:
                pass

        self._loop.call_soon_threadsafe(_enqueue)

    async def recv(self) -> av.AudioFrame:
        """Called by aiortc media engine for each outgoing 20ms audio frame."""
        try:
            pcm_bytes = await asyncio.wait_for(self._queue.get(), timeout=0.08)
        except asyncio.TimeoutError:
            # Generate 20ms of silence if queue underruns
            pcm_bytes = b"\x00" * BYTES_PER_FRAME_S16

        frame = av.AudioFrame(format="s16", layout="stereo", samples=FRAME_SIZE)
        frame.planes[0].update(pcm_bytes)
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = self._time_base
        self._pts += FRAME_SIZE
        return frame

    def stop(self) -> None:
        super().stop()
        broadcaster.unregister(self)


# -----------------------------------------------------------------------------
# FastAPI App & WebSocket Signaling
# -----------------------------------------------------------------------------
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("AirBridge WebRTC Server starting up...")
    yield
    logger.info("Server shutting down, closing all active PeerConnections...")
    for pc in list(active_peer_connections):
        await pc.close()
    active_peer_connections.clear()


app = FastAPI(title="AirBridge WebRTC Audio Server", lifespan=lifespan)

# Serve web directory
WEB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "web"))
INDEX_HTML = os.path.join(WEB_DIR, "index.html")

# Keep track of active peer connections
active_peer_connections: Set[RTCPeerConnection] = set()


@app.get("/")
async def get_index():
    """Serves the WebRTC test client."""
    return FileResponse(INDEX_HTML)


def get_lan_info() -> dict:
    """Detects primary LAN IPv4 address and generates recommended URLs."""
    import socket
    import ifaddr

    primary_ip = None
    all_ips = []

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("8.8.8.8", 80))
        cand = s.getsockname()[0]
        s.close()
        if cand and not cand.startswith(("127.", "169.254.")):
            primary_ip = cand
            all_ips.append(cand)
    except Exception:
        pass

    try:
        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                if ip.is_IPv4 and not ip.ip.startswith(("127.", "169.254.")):
                    if ip.ip not in all_ips:
                        all_ips.append(ip.ip)
    except Exception:
        pass

    if not primary_ip and all_ips:
        primary_ip = all_ips[0]

    port = 8000
    lan_url = f"http://{primary_ip}:{port}" if primary_ip else f"http://localhost:{port}"

    return {
        "primary_ip": primary_ip or "127.0.0.1",
        "lan_url": lan_url,
        "all_ips": all_ips,
        "port": port,
    }


@app.get("/api/info")
async def get_network_info():
    """Returns detected LAN addresses and recommended iPhone connection URL."""
    return get_lan_info()


@app.get("/health")
async def health_check():
    """Service health endpoint."""
    info = get_lan_info()
    return {
        "status": "ok",
        "service": "airbridge",
        "active_peers": len(active_peer_connections),
        "lan_ip": info["primary_ip"],
        "lan_url": info["lan_url"],
    }


@app.websocket("/ws")
async def websocket_signaling(websocket: WebSocket):
    """
    WebSocket signaling endpoint for exchanging SDP offers/answers and ICE candidates.
    """
    await websocket.accept()
    client_addr = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "unknown"
    logger.info("WebSocket signaling connected from: %s", client_addr)

    loop = asyncio.get_running_loop()
    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
    active_peer_connections.add(pc)

    track = WASAPIAudioTrack(loop=loop)
    pc.addTrack(track)

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        logger.info("[WebRTC] PeerConnection state: %s (%s)", pc.connectionState, client_addr)
        if pc.connectionState in ["failed", "closed"]:
            await pc.close()
            active_peer_connections.discard(pc)

    @pc.on("iceconnectionstatechange")
    async def on_iceconnectionstatechange():
        logger.info("[WebRTC] ICE Connection state: %s (%s)", pc.iceConnectionState, client_addr)

    try:
        while True:
            raw_data = await websocket.receive_text()
            message = json.loads(raw_data)
            msg_type = message.get("type")

            if msg_type == "offer":
                logger.info("[Signaling] Received SDP Offer from %s", client_addr)
                offer = RTCSessionDescription(sdp=message["sdp"], type="offer")
                await pc.setRemoteDescription(offer)

                # Generate Answer
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)

                logger.info("[Signaling] Generated & sending SDP Answer to %s", client_addr)
                await websocket.send_json({
                    "type": "answer",
                    "sdp": pc.localDescription.sdp
                })

            elif msg_type == "candidate":
                cand_data = message.get("candidate")
                if cand_data and cand_data.get("candidate"):
                    cand_str = cand_data["candidate"]
                    if cand_str.startswith("candidate:"):
                        cand_str = cand_str[10:]
                    try:
                        candidate = candidate_from_sdp(cand_str)
                        candidate.sdpMid = cand_data.get("sdpMid")
                        candidate.sdpMLineIndex = cand_data.get("sdpMLineIndex")
                        await pc.addIceCandidate(candidate)
                        logger.debug("[Signaling] Added ICE candidate from %s", client_addr)
                    except Exception as e:
                        logger.warning("Failed to parse/add ICE candidate: %s", e)

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: %s", client_addr)
    except Exception as e:
        logger.error("Exception in WebSocket session (%s): %s", client_addr, e)
    finally:
        track.stop()
        await pc.close()
        active_peer_connections.discard(pc)
        logger.info("Cleaned up PeerConnection for %s", client_addr)


if __name__ == "__main__":
    info = get_lan_info()
    print("=" * 66)
    print("  AIRBRIDGE - Phase 4: Local Network & iPhone Streaming Server")
    print("=" * 66)
    print(f"  Local PC URL:      http://localhost:{info['port']}")
    print(f"  Recommended LAN:   {info['lan_url']}")
    print(f"  Detected IPs:      {', '.join(info['all_ips'])}")
    print("-" * 66)
    print(f"  >> On your iPhone (Safari connected to the same Wi-Fi):")
    print(f"     Open: {info['lan_url']}")
    print("=" * 66)
    print()

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=info["port"],
        log_level="info",
    )
