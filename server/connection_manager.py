"""
AirBridge - Connection Manager

Responsibilities:
    - Generates unique client IDs.
    - Manages peer connection lifecycles (connect, signaling, disconnect, cleanup).
    - Coordinates AudioCapture start/stop race-safely via serialized lifecycle locking.
    - Tracks connection state, heartbeats, and peer telemetry.
    - Manages thread-safe AudioCapture error callbacks via event loop scheduling.
    - Prevents unbounded recovery loops on flapping capture crashes.
    - Accurately tracks and reports capture lifecycle states (running, idle, error).
    - Distinguishes active WebRTC media sessions from stale signaling channels.
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
    STABLE_FRAME_THRESHOLD: int = 5

    def __init__(
        self,
        audio_pipeline: Optional[AudioPipeline] = None,
        audio_capture: Optional[AudioCapture] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self.pipeline = audio_pipeline or pipeline
        self._loop = loop

        # Initialize capture with frame and error dispatchers
        self.capture = audio_capture or AudioCapture(
            on_frame=self._on_capture_frame,
            on_error=self._handle_capture_error,
        )
        if hasattr(self.capture, "on_error") and self.capture.on_error is None:
            self.capture.on_error = self._handle_capture_error
        if hasattr(self.capture, "on_frame") and self.capture.on_frame is None:
            self.capture.on_frame = self._on_capture_frame

        self._clients: Dict[str, ClientSession] = {}
        self._lock = asyncio.Lock()

        # Dedicated serialized lifecycle lock for capture start/stop synchronization
        self._capture_lifecycle_lock = asyncio.Lock()
        self._capture_error: Optional[str] = None
        self._capture_retry_count: int = 0
        self._consecutive_healthy_frames: int = 0

        self._recovery_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Stores the primary application event loop for cross-thread scheduling."""
        self._loop = loop

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def capture_error(self) -> Optional[str]:
        return self._capture_error

    def _on_capture_frame(self, pcm_bytes: bytes) -> None:
        """Called by AudioCapture worker thread for each captured PCM block."""
        self.pipeline.publish_pcm(pcm_bytes)
        self._notify_frame_produced()

    def _notify_frame_produced(self) -> None:
        """
        Tracks consecutive healthy frames to confirm stable capture.
        Only resets retry count after sustained successful operation.
        """
        self._consecutive_healthy_frames += 1
        if self._consecutive_healthy_frames >= self.STABLE_FRAME_THRESHOLD:
            if self._capture_retry_count > 0 or self._capture_error is not None:
                logger.info(
                    "AudioCapture confirmed stable after %d frames. Resetting retry counters.",
                    self._consecutive_healthy_frames,
                )
                self._capture_retry_count = 0
                self._capture_error = None

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
            "max_retries": self.MAX_CAPTURE_RETRIES,
        }

    def _handle_capture_error(self, error: Exception) -> None:
        """
        Thread-safe error callback invoked by the AudioCapture background thread.
        Dispatches recovery scheduling onto the main event loop without calling get_running_loop.
        """
        self._consecutive_healthy_frames = 0
        err_msg = str(error)
        logger.error("AudioCapture failure reported from worker thread: %s", err_msg)
        self._capture_error = err_msg

        loop = self._loop
        if loop is None or loop.is_closed():
            try:
                loop = asyncio.get_running_loop()
                self._loop = loop
            except RuntimeError:
                pass

        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._schedule_recovery_on_loop)
            except RuntimeError as e:
                logger.warning("Could not schedule capture recovery on event loop: %s", e)
        else:
            logger.warning(
                "Event loop unavailable in _handle_capture_error; recovery task not scheduled."
            )

    def _schedule_recovery_on_loop(self) -> None:
        """Executed on the event loop to spawn a single deduplicated recovery task."""
        if self._recovery_task is not None and not self._recovery_task.done():
            # A recovery attempt is already active or in backoff
            return

        if len(self._clients) == 0:
            return

        self._recovery_task = asyncio.create_task(self._attempt_capture_recovery())

    async def _attempt_capture_recovery(self) -> None:
        """Attempts bounded recovery of AudioCapture when listeners are waiting."""
        async with self._capture_lifecycle_lock:
            async with self._lock:
                active_count = len(self._clients)

            if active_count == 0:
                logger.info("AudioCapture recovery skipped: 0 active clients.")
                return

            if self._capture_retry_count >= self.MAX_CAPTURE_RETRIES:
                logger.error(
                    "AudioCapture recovery limit reached (%d attempts). Staying in error state.",
                    self.MAX_CAPTURE_RETRIES,
                )
                self._capture_error = (
                    f"Capture failed permanently after {self.MAX_CAPTURE_RETRIES} attempts"
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

        # Sleep outside capture_lifecycle_lock so concurrent disconnects can proceed
        try:
            await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            logger.info("AudioCapture recovery task cancelled during backoff.")
            return

        async with self._capture_lifecycle_lock:
            async with self._lock:
                if len(self._clients) == 0:
                    logger.info("All clients disconnected during recovery backoff; cancelling restart.")
                    return

            try:
                self.capture.start()
                logger.info(
                    "AudioCapture started on recovery attempt %d (awaiting frame confirmation).",
                    self._capture_retry_count,
                )
            except Exception as retry_err:
                logger.error(
                    "AudioCapture recovery attempt %d failed: %s",
                    self._capture_retry_count,
                    retry_err,
                )
                self._capture_error = str(retry_err)
                if self._capture_retry_count < self.MAX_CAPTURE_RETRIES:
                    self._schedule_recovery_on_loop()

    async def _sync_capture_state(self) -> None:
        """
        Coordinates AudioCapture start/stop with active client count.
        Serialized by _capture_lifecycle_lock to prevent start/stop race conditions.
        Accurately reports stop timeouts and lifecycle errors.
        """
        async with self._capture_lifecycle_lock:
            async with self._lock:
                active_count = len(self._clients)

            if active_count > 0:
                if not self.capture.is_running:
                    logger.info("Active clients present (%d). Starting audio capture.", active_count)
                    try:
                        self.capture.start()
                        self._consecutive_healthy_frames = 0
                        # Clear previous transient stop errors on fresh start
                        if self._capture_error == "AudioCapture thread failed to stop within timeout":
                            self._capture_error = None
                    except Exception as e:
                        logger.error("Failed to start audio capture: %s", e)
                        self._capture_error = str(e)
            else:
                # 0 active clients: cancel pending recovery tasks immediately
                if self._recovery_task and not self._recovery_task.done():
                    self._recovery_task.cancel()
                    self._recovery_task = None

                self._consecutive_healthy_frames = 0

                if self.capture.is_running:
                    logger.info("No active clients remaining (0). Stopping audio capture.")
                    stopped = self.capture.stop(timeout=2.0)
                    if not stopped:
                        logger.error("AudioCapture thread did not stop cleanly within timeout.")
                        self._capture_error = "AudioCapture thread failed to stop within timeout"
                    else:
                        if self._capture_error == "AudioCapture thread failed to stop within timeout":
                            self._capture_error = None
                else:
                    if self._capture_error == "AudioCapture thread failed to stop within timeout":
                        self._capture_error = None

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
        if self._loop is None or self._loop.is_closed():
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

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
        max_idle_seconds: float = 120.0,
        negotiation_timeout: float = 60.0,
    ) -> int:
        """
        Removes connections that have failed, closed, or timed out during initial signaling.
        Distinguishes active WebRTC media sessions from dead or stalled channels:
        - Dead states (failed/closed) are pruned immediately.
        - Incomplete initial negotiations exceeding negotiation_timeout are pruned.
        - Connected WebRTC sessions with active heartbeat are protected.
        - Clients with no heartbeat or media activity for max_idle_seconds are pruned.
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

                # 2. Negotiation timeout: connected WebSocket but never established WebRTC
                is_negotiating = session.connection_state in ("new", "connecting")
                if is_negotiating and (now - session.connected_at) > negotiation_timeout:
                    logger.warning(
                        "Client %s negotiation timed out after %.1fs",
                        cid,
                        now - session.connected_at,
                    )
                    to_remove.append(cid)
                    continue

                # 3. Active WebRTC session check:
                # If connected and heartbeat was refreshed within max_idle_seconds, client is healthy
                idle_time = now - session.last_seen
                if idle_time > max_idle_seconds:
                    logger.warning(
                        "Client %s inactive for %.1fs (connection=%s, ice=%s), marking as stale",
                        cid,
                        idle_time,
                        session.connection_state,
                        session.ice_state,
                    )
                    to_remove.append(cid)

        for cid in to_remove:
            logger.info("Cleaning up stale connection: %s", cid)
            await self.remove_client(cid)

        return len(to_remove)

    async def start_cleanup_loop(
        self,
        interval_seconds: float = 15.0,
        max_idle_seconds: float = 120.0,
        negotiation_timeout: float = 60.0,
    ) -> None:
        """Background coroutine periodically running cleanup_stale_connections."""
        self._loop = asyncio.get_running_loop()
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
        if self._recovery_task and not self._recovery_task.done():
            self._recovery_task.cancel()
            self._recovery_task = None

        async with self._lock:
            cids = list(self._clients.keys())

        for cid in cids:
            await self.remove_client(cid)

        await self._sync_capture_state()


# Global singleton connection manager
connection_manager = ConnectionManager()
