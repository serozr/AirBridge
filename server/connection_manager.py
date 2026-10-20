"""
AirBridge - Connection Manager

Responsibilities:
    - Generates unique client IDs.
    - Manages peer connection lifecycles (connect, signaling, disconnect, cleanup).
    - Coordinates AudioCapture start/stop race-safely via serialized lifecycle locking.
    - Tracks connection state, heartbeats, and peer telemetry.
    - Distinguishes active WebRTC media sessions from stale signaling channels.
    - Manages AudioCapture error handling, bounded recovery, and health status.
    - Supports multiple simultaneous listeners on a single audio capture pipeline.
    - Manages periodic background cleanup for stale/dead sessions.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Any

from aiortc import RTCPeerConnection

from server.audio_capture import AudioCapture
from server.audio_pipeline import AudioPipeline, AudioSubscriber, pipeline

logger = logging.getLogger("AirBridge.ConnectionManager")


@dataclass
class ClientSession:
    """
    State tracking for a single connected WebRTC peer client.
    """
    client_id: str
    client_address: str
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    pc: Optional[RTCPeerConnection] = None
    subscriber: Optional[AudioSubscriber] = None
    websocket: Optional[object] = None
    connection_state: str = "new"
    ice_state: str = "new"
    closed: bool = False

    def touch(self) -> None:
        self.last_seen = time.time()

    def to_dict(self) -> dict:
        return {
            "client_id": self.client_id,
            "address": self.client_address,
            "connected_at": self.connected_at,
            "duration_seconds": round(time.time() - self.connected_at, 1),
            "connection_state": self.connection_state,
            "ice_state": self.ice_state,
        }


class ConnectionManager:
    """
    Thread-safe and async-safe manager for WebRTC peers and capture lifecycle.
    """

    MAX_CAPTURE_RETRIES: int = 3

    def __init__(
        self,
        audio_pipeline: Optional[AudioPipeline] = None,
        audio_capture: Optional[AudioCapture] = None,
    ) -> None:
        self.pipeline = audio_pipeline or pipeline
        self.capture = audio_capture or AudioCapture(
            on_frame=self.pipeline.publish_pcm,
            on_error=self._handle_capture_error,
        )
        # If an externally-created AudioCapture was passed, wire on_error if not set
        if hasattr(self.capture, "on_error") and self.capture.on_error is None:
            self.capture.on_error = self._handle_capture_error

        self._clients: Dict[str, ClientSession] = {}
        self._lock = asyncio.Lock()

        # Dedicated serialized lifecycle lock for capture start/stop synchronization
        self._capture_lifecycle_lock = asyncio.Lock()
        self._capture_error: Optional[str] = None
        self._capture_retry_count: int = 0

        self._cleanup_task: Optional[asyncio.Task] = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def capture_error(self) -> Optional[str]:
        return self._capture_error

    def get_capture_health(self) -> Dict[str, Any]:
        """Returns capture operational state for /health telemetry."""
        if self._capture_error:
            status = "error"
        elif self.capture.is_running:
            status = "running"
        else:
            status = "idle"

        return {
            "status": status,
            "is_running": self.capture.is_running,
            "device_name": getattr(self.capture, "device_name", "Unknown"),
            "error": self._capture_error,
            "retry_count": self._capture_retry_count,
        }

    def _handle_capture_error(self, error: Exception) -> None:
        """Called when AudioCapture background thread encounters an error."""
        err_msg = str(error)
        logger.error("AudioCapture failure reported: %s", err_msg)
        self._capture_error = err_msg

        # Schedule bounded recovery attempt on the event loop if active clients exist
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        if loop.is_running():
            loop.create_task(self._attempt_capture_recovery())

    async def _attempt_capture_recovery(self) -> None:
        """Attempts bounded recovery of AudioCapture when listeners are waiting."""
        async with self._capture_lifecycle_lock:
            async with self._lock:
                active_count = len(self._clients)

            if active_count == 0:
                logger.info("AudioCapture error with 0 clients; no restart needed.")
                return

            if self._capture_retry_count >= self.MAX_CAPTURE_RETRIES:
                logger.error(
                    "AudioCapture recovery limit reached (%d attempts). Marked in error state.",
                    self.MAX_CAPTURE_RETRIES,
                )
                return

            self._capture_retry_count += 1
            backoff = 0.5 * self._capture_retry_count
            logger.info(
                "Attempting AudioCapture recovery in %.1fs (attempt %d/%d)...",
                backoff,
                self._capture_retry_count,
                self.MAX_CAPTURE_RETRIES,
            )
            await asyncio.sleep(backoff)

            async with self._lock:
                if len(self._clients) == 0:
                    return

            try:
                self.capture.start()
                logger.info("AudioCapture successfully recovered on attempt %d.", self._capture_retry_count)
                self._capture_error = None
                self._capture_retry_count = 0
            except Exception as retry_err:
                logger.error("AudioCapture recovery attempt %d failed: %s", self._capture_retry_count, retry_err)
                self._capture_error = str(retry_err)

    async def _sync_capture_state(self) -> None:
        """
        Coordinates AudioCapture start/stop with active client count.
        Serialized by _capture_lifecycle_lock to prevent start/stop race conditions.
        """
        async with self._capture_lifecycle_lock:
            async with self._lock:
                active_count = len(self._clients)

            if active_count > 0:
                if not self.capture.is_running:
                    logger.info("Active clients present (%d). Starting audio capture.", active_count)
                    try:
                        self.capture.start()
                        self._capture_error = None
                        self._capture_retry_count = 0
                    except Exception as e:
                        logger.error("Failed to start audio capture: %s", e)
                        self._capture_error = str(e)
            else:
                if self.capture.is_running:
                    logger.info("No active clients remaining (0). Stopping audio capture.")
                    self.capture.stop()
                self._capture_error = None
                self._capture_retry_count = 0

    async def register_client(
        self,
        client_address: str,
        websocket: Optional[object] = None,
        client_id: Optional[str] = None,
    ) -> ClientSession:
        """
        Registers a new client session.
        Rejects duplicate explicit client IDs and preserves existing live sessions.
        Atomically updates client set and coordinates capture start without race conditions.
        """
        async with self._lock:
            if client_id is not None:
                if client_id in self._clients:
                    raise ValueError(
                        f"Client ID '{client_id}' is already registered and active."
                    )
                cid = client_id
            else:
                cid = str(uuid.uuid4())[:8]
                while cid in self._clients:
                    cid = str(uuid.uuid4())[:8]

            session = ClientSession(
                client_id=cid,
                client_address=client_address,
                websocket=websocket,
            )
            self._clients[cid] = session

        logger.info(
            "Client registered: %s (%s). Total clients: %d",
            cid,
            client_address,
            len(self._clients),
        )

        # Coordinate capture start via lifecycle lock (handles concurrent disconnects safely)
        await self._sync_capture_state()
        return session

    async def remove_client(self, client_id: str) -> None:
        """
        Closes and removes a client session safely.
        Separates atomic state removal from slow peer connection and capture shutdown.
        If no clients remain, automatically stops audio capture via serialized lifecycle sync.
        """
        async with self._lock:
            session = self._clients.pop(client_id, None)
            if not session or session.closed:
                # Client was already removed by concurrent teardown
                return
            session.closed = True
            remaining_clients = len(self._clients)

        # Cleanup subscriber and peer connection outside the manager lock
        if session.subscriber:
            session.subscriber.close()

        if session.pc:
            try:
                await session.pc.close()
            except Exception as e:
                logger.debug("Error closing peer connection for %s: %s", client_id, e)

        logger.info(
            "Client removed: %s. Remaining clients: %d",
            client_id,
            remaining_clients,
        )

        # Synchronize capture lifecycle (stops capture cleanly if remaining clients == 0)
        await self._sync_capture_state()

    async def get_client(self, client_id: str) -> Optional[ClientSession]:
        async with self._lock:
            return self._clients.get(client_id)

    async def get_clients(self) -> List[ClientSession]:
        async with self._lock:
            return list(self._clients.values())

    async def update_states(
        self,
        client_id: str,
        connection_state: Optional[str] = None,
        ice_state: Optional[str] = None,
    ) -> None:
        async with self._lock:
            session = self._clients.get(client_id)
            if session and not session.closed:
                session.touch()
                if connection_state:
                    session.connection_state = connection_state
                if ice_state:
                    session.ice_state = ice_state

    async def cleanup_stale_connections(
        self,
        max_idle_seconds: float = 300.0,
        negotiation_timeout: float = 60.0,
    ) -> int:
        """
        Removes connections that have failed, closed, or timed out during initial signaling.
        CRITICAL: Actively connected WebRTC audio sessions (connection_state == 'connected')
        are NEVER pruned as stale, even if the signaling WebSocket is silent.
        """
        now = time.time()
        to_remove = []
        async with self._lock:
            for cid, session in self._clients.items():
                # 1. Definitively dead states
                is_dead = (
                    session.connection_state in ("failed", "closed")
                    or session.ice_state in ("failed", "closed")
                )
                if is_dead:
                    to_remove.append(cid)
                    continue

                # 2. Active WebRTC media connection: actively streaming, NEVER prune!
                is_webrtc_active = (
                    session.connection_state == "connected"
                    or session.ice_state in ("connected", "completed")
                )
                if is_webrtc_active:
                    continue

                # 3. Negotiation timeout: connected WebSocket but never established WebRTC
                is_negotiating = session.connection_state in ("new", "connecting")
                if is_negotiating and (now - session.connected_at) > negotiation_timeout:
                    logger.warning(
                        "Client %s negotiation timed out after %.1fs",
                        cid,
                        now - session.connected_at,
                    )
                    to_remove.append(cid)
                    continue

                # 4. Inactive signaling channel timeout for non-active peers
                idle_time = now - session.last_seen
                if idle_time > max_idle_seconds:
                    logger.warning("Client %s inactive for %.1fs, marking as stale", cid, idle_time)
                    to_remove.append(cid)

        for cid in to_remove:
            logger.info("Cleaning up stale connection: %s", cid)
            await self.remove_client(cid)

        return len(to_remove)

    async def start_cleanup_loop(
        self,
        interval_seconds: float = 15.0,
        max_idle_seconds: float = 300.0,
        negotiation_timeout: float = 60.0,
    ) -> None:
        """Background coroutine periodically running cleanup_stale_connections."""
        logger.info(
            "Stale connection cleanup loop started (interval=%.1fs, max_idle=%.1fs).",
            interval_seconds,
            max_idle_seconds,
        )
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                await self.cleanup_stale_connections(
                    max_idle_seconds=max_idle_seconds,
                    negotiation_timeout=negotiation_timeout,
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in stale connection cleanup loop: %s", e)

    async def close_all(self) -> None:
        """Gracefully closes all peer connections and shuts down capture."""
        logger.info("Closing all client connections and stopping capture...")
        async with self._lock:
            cids = list(self._clients.keys())

        for cid in cids:
            await self.remove_client(cid)

        await self._sync_capture_state()


# Global singleton connection manager
connection_manager = ConnectionManager()
