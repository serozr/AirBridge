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
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel
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
from server.security import security_manager

class AuthRequest(BaseModel):
    pin: Optional[str] = None
    token: Optional[str] = None

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


@app.post("/api/auth")
async def authenticate(request: Request, body: AuthRequest):
    """
    Verifies pairing PIN or validates an existing session token.
    Enforces origin validation, constant-time PIN comparison, and rate-limiting.
    """
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if not security_manager.validate_origin(origin, host):
        raise HTTPException(status_code=403, detail="Unauthorized Origin")

    client_ip = request.client.host if request.client else "unknown"

    if body.token:
        if security_manager.validate_token(body.token, client_ip):
            return {
                "status": "ok",
                "authenticated": True,
                "token": body.token,
                "session_id": security_manager.server_session_id,
            }
        else:
            raise HTTPException(status_code=401, detail="Invalid or expired token")

    if body.pin:
        success, token, err = security_manager.verify_pin(body.pin, client_ip)
        if success and token:
            return {
                "status": "ok",
                "authenticated": True,
                "token": token,
                "session_id": security_manager.server_session_id,
            }
        else:
            is_locked, _ = security_manager.rate_limiter.is_locked(client_ip)
            status_code = 429 if is_locked else 401
            raise HTTPException(status_code=status_code, detail=err or "Invalid PIN")

    raise HTTPException(status_code=400, detail="Either 'pin' or 'token' must be provided")


@app.get("/api/auth/status")
async def auth_status(request: Request, token: Optional[str] = None):
    """Checks session validity for the client."""
    client_ip = request.client.host if request.client else "unknown"
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()

    is_valid = security_manager.validate_token(token, client_ip)
    return {
        "authenticated": is_valid,
        "security_enabled": security_manager.enabled,
        "session_id": security_manager.server_session_id,
    }


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
        "version": "0.3.0",
        "clients": connection_manager.client_count,
        "active_peers": connection_manager.client_count,
        "lan_ip": info["primary_ip"],
        "lan_url": info["lan_url"],
        "security": {
            "enabled": security_manager.enabled,
            "session_id": security_manager.server_session_id,
            "active_sessions": security_manager.active_session_count(),
        },
        "capture": capture_health,
        "audio": metrics.to_dict(),
    }


@app.websocket("/ws")
async def websocket_signaling(websocket: WebSocket):
    """
    WebSocket signaling endpoint for exchanging SDP offers/answers and ICE candidates.
    Protected by origin validation and token/PIN session authentication.
    """
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if not security_manager.validate_origin(origin, host):
        logger.warning("WebSocket rejected: unauthorized origin '%s'", origin)
        await websocket.close(code=4403, reason="Unauthorized Origin")
        return

    client_ip = websocket.client.host if websocket.client else "unknown"
    client_addr = f"{client_ip}:{websocket.client.port}" if websocket.client else "unknown"

    token = websocket.query_params.get("token")
    authenticated = False

    if token:
        if security_manager.validate_token(token, client_ip):
            authenticated = True
        else:
            logger.warning("WebSocket rejected: invalid or expired query token from %s", client_addr)
            await websocket.close(code=4401, reason="Invalid or Expired Token")
            return

    await websocket.accept()

    # If not authenticated via query parameter, require immediate auth handshake message
    if not authenticated:
        try:
            raw_auth = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
            auth_msg = json.loads(raw_auth)
            if auth_msg.get("type") == "auth":
                msg_token = auth_msg.get("token")
                msg_pin = auth_msg.get("pin")
                if msg_token and security_manager.validate_token(msg_token, client_ip):
                    authenticated = True
                    await websocket.send_json({"type": "auth_ok", "token": msg_token})
                elif msg_pin:
                    valid, new_token, err = security_manager.verify_pin(msg_pin, client_ip)
                    if valid and new_token:
                        authenticated = True
                        await websocket.send_json({"type": "auth_ok", "token": new_token})
                    else:
                        await websocket.send_json({"type": "auth_fail", "error": err or "Invalid PIN"})
                        await websocket.close(code=4401, reason="Authentication Failed")
                        return
                else:
                    await websocket.send_json({"type": "auth_fail", "error": "Invalid auth payload"})
                    await websocket.close(code=4401, reason="Authentication Failed")
                    return
            else:
                await websocket.send_json({"type": "auth_fail", "error": "Authentication required"})
                await websocket.close(code=4401, reason="Authentication Required")
                return
        except asyncio.TimeoutError:
            logger.warning("WebSocket auth handshake timeout from %s", client_addr)
            await websocket.close(code=4408, reason="Authentication Timeout")
            return
        except Exception as e:
            logger.warning("WebSocket auth error from %s: %s", client_addr, e)
            await websocket.close(code=4400, reason="Bad Request")
            return

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
    print("  AirBridge v0.3.0 - Security Layer & Audio Pipeline")
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
    print(security_manager.get_security_banner())
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
