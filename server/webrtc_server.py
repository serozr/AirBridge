"""
AirBridge - WebRTC Audio Streaming Server

Orchestration layer:
    - FastAPI HTTP & WebSocket signaling.
    - WebRTC SDP offer/answer exchange via aiortc.
    - PipelineAudioTrack yielding 48kHz stereo frames to WebRTC's Opus encoder.
    - Delegates client lifecycle and audio capture to ConnectionManager.
"""

import asyncio
import fractions
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import av
from aiortc import (
    AudioStreamTrack,
    RTCConfiguration,
    RTCIceCandidate,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.sdp import candidate_from_sdp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
import uvicorn

from server.config import (
    CHANNELS,
    DEFAULT_HOST,
    DEFAULT_PORT,
    FRAME_DURATION_MS,
    FRAME_SAMPLES,
    MAX_BUFFER_FRAMES,
    SAMPLE_RATE,
)
from server.audio_pipeline import AudioSubscriber, pipeline
from server.connection_manager import connection_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("AirBridge.WebRTCServer")


class PipelineAudioTrack(AudioStreamTrack):
    """
    aiortc AudioStreamTrack consuming frames from the central AudioPipeline.
    Delivers 48kHz stereo 20ms frames to WebRTC's native Opus encoder.
    Maintains a strictly monotonic outgoing PTS timeline.
    """

    kind = "audio"

    def __init__(self, subscriber: AudioSubscriber) -> None:
        super().__init__()
        self.subscriber = subscriber
        self._time_base = fractions.Fraction(1, SAMPLE_RATE)
        self._last_track_pts: Optional[int] = None

    async def recv(self) -> av.AudioFrame:
        """Called by aiortc media engine for each outgoing 20ms audio frame."""
        frame_data = await self.subscriber.get_frame(timeout=0.08)

        # Ensure strictly monotonic PTS progression for the outgoing WebRTC track
        if self._last_track_pts is None:
            self._last_track_pts = frame_data.pts
        else:
            expected_pts = self._last_track_pts + frame_data.samples
            if frame_data.pts > expected_pts:
                # Normal forward gap when frames were dropped intentionally
                self._last_track_pts = frame_data.pts
            else:
                # Stale frame or repeated silence: guarantee monotonic progress
                self._last_track_pts = expected_pts

        frame = av.AudioFrame(
            format="s16",
            layout="stereo",
            samples=frame_data.samples,
        )
        frame.planes[0].update(frame_data.data)
        frame.sample_rate = frame_data.sample_rate
        frame.pts = self._last_track_pts
        frame.time_base = self._time_base
        return frame

    def stop(self) -> None:
        super().stop()
        self.subscriber.close()


def get_lan_info() -> Dict[str, Any]:
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

    port = DEFAULT_PORT
    lan_url = f"http://{primary_ip}:{port}" if primary_ip else f"http://localhost:{port}"

    return {
        "primary_ip": primary_ip or "127.0.0.1",
        "lan_url": lan_url,
        "all_ips": all_ips,
        "port": port,
    }


# -----------------------------------------------------------------------------
# FastAPI Application & Lifespan
# -----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    connection_manager.set_loop(loop)
    logger.info("AirBridge WebRTC Server starting up...")
    cleanup_task = asyncio.create_task(
        connection_manager.start_cleanup_loop(interval_seconds=15.0, max_idle_seconds=120.0)
    )
    yield
    logger.info("Server shutting down, closing all connections...")
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    await connection_manager.close_all()


app = FastAPI(title="AirBridge WebRTC Audio Server", lifespan=lifespan)

WEB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "web"))
INDEX_HTML = os.path.join(WEB_DIR, "index.html")


@app.get("/")
async def get_index():
    """Serves the WebRTC audio receiver interface."""
    return FileResponse(INDEX_HTML)


@app.get("/api/info")
async def get_network_info():
    """Returns detected LAN addresses and recommended connection URL."""
    return get_lan_info()


@app.get("/health")
async def health_check():
    """Service health and telemetry endpoint."""
    info = get_lan_info()
    metrics = pipeline.get_metrics()
    capture_health = connection_manager.get_capture_health()
    overall_status = "ok" if capture_health["status"] != "error" else "degraded"
    return {
        "status": overall_status,
        "service": "airbridge",
        "version": "0.2.0",
        "clients": connection_manager.client_count,
        "active_peers": connection_manager.client_count,
        "lan_ip": info["primary_ip"],
        "lan_url": info["lan_url"],
        "capture": capture_health,
        "audio": metrics.to_dict(),
    }


@app.websocket("/ws")
async def websocket_signaling(websocket: WebSocket):
    """
    WebSocket signaling endpoint for exchanging SDP offers/answers and ICE candidates.
    """
    await websocket.accept()
    client_addr = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "unknown"
    loop = asyncio.get_running_loop()

    cid: Optional[str] = None
    try:
        # Register client session
        session = await connection_manager.register_client(
            client_address=client_addr,
            websocket=websocket,
        )
        cid = session.client_id
        logger.info("Signaling connected: Client ID %s (%s)", cid, client_addr)

        # Initialize WebRTC peer connection (local ICE for sub-millisecond handshake)
        pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
        session.pc = pc

        # Subscribe to central audio pipeline
        subscriber = pipeline.subscribe(subscriber_id=cid, loop=loop)
        session.subscriber = subscriber

        track = PipelineAudioTrack(subscriber=subscriber)
        pc.addTrack(track)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            state = pc.connectionState
            logger.info("[WebRTC] Client %s PeerConnection state: %s", cid, state)
            await connection_manager.update_states(cid, connection_state=state)
            if state in ("failed", "closed"):
                await connection_manager.remove_client(cid)

        @pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            state = pc.iceConnectionState
            logger.info("[WebRTC] Client %s ICE state: %s", cid, state)
            await connection_manager.update_states(cid, ice_state=state)

        while True:
            raw_data = await websocket.receive_text()
            session.touch()
            message = json.loads(raw_data)
            msg_type = message.get("type")

            if msg_type == "offer":
                logger.info("[Signaling] Received SDP Offer from %s (%s)", cid, client_addr)
                offer = RTCSessionDescription(sdp=message["sdp"], type="offer")
                await pc.setRemoteDescription(offer)

                # Generate and apply Answer
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)

                logger.info("[Signaling] Generated & sending SDP Answer to %s", cid)
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
                    except Exception as e:
                        logger.warning("Failed to parse ICE candidate from %s: %s", cid, e)

            elif msg_type == "ping":
                session.touch()
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for client %s", cid)
    except Exception as e:
        logger.error("Exception in WebSocket session (%s): %s", cid, e)
    finally:
        if cid:
            await connection_manager.remove_client(cid)


if __name__ == "__main__":
    info = get_lan_info()
    print("=" * 66)
    print("  AirBridge v0.2.0 - Audio Pipeline & Connection Core")
    print("=" * 66)
    print("  Audio:")
    print(f"    Sample rate:  {SAMPLE_RATE} Hz")
    print(f"    Channels:     {CHANNELS} (Stereo)")
    print(f"    Frame:        {FRAME_DURATION_MS} ms / {FRAME_SAMPLES} samples")
    print(f"    Buffer policy: max {MAX_BUFFER_FRAMES} frames (~{FRAME_DURATION_MS * MAX_BUFFER_FRAMES} ms, drop-oldest)")
    print("  Network:")
    print(f"    Listening:    {DEFAULT_HOST}:{info['port']}")
    print(f"    Local PC URL: http://localhost:{info['port']}")
    print(f"    LAN URL:      {info['lan_url']}")
    print("-" * 66)
    print(f"  >> On your iPhone (Safari connected to the same Wi-Fi):")
    print(f"     Open: {info['lan_url']}")
    print("=" * 66)
    print()

    uvicorn.run(
        app,
        host=DEFAULT_HOST,
        port=info["port"],
        log_level="info",
    )
