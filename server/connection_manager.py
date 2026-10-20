"""
AirBridge - Connection Manager

Responsibilities:
    - Generates unique client IDs.
    - Manages peer connection lifecycles (connect, signaling, disconnect, cleanup).
    - Coordinates AudioCapture start/stop based on active subscriber count.
    - Tracks connection state, heartbeats, and peer telemetry.
    - Supports multiple simultaneous listeners on a single audio capture pipeline.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

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

    def __init__(
        self,
        audio_pipeline: Optional[AudioPipeline] = None,
        audio_capture: Optional[AudioCapture] = None,
    ) -> None:
        self.pipeline = audio_pipeline or pipeline
        self.capture = audio_capture or AudioCapture(
            on_frame=self.pipeline.publish_pcm
        )
        self._clients: Dict[str, ClientSession] = {}
        self._lock = asyncio.Lock()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def register_client(
        self,
        client_address: str,
        websocket: Optional[object] = None,
        client_id: Optional[str] = None,
    ) -> ClientSession:
        """
        Registers a new client session.
        If this is the first active client, automatically initiates audio capture.
        """
        async with self._lock:
            cid = client_id or str(uuid.uuid4())[:8]
            session = ClientSession(
                client_id=cid,
                client_address=client_address,
                websocket=websocket,
            )
            self._clients[cid] = session

            # Start audio capture when the first client connects
            if len(self._clients) == 1 and not self.capture.is_running:
                logger.info("First client connected (%s). Starting audio capture.", cid)
                self.capture.start()

            logger.info(
                "Client registered: %s (%s). Total clients: %d",
                cid,
                client_address,
                len(self._clients),
            )
            return session

    async def remove_client(self, client_id: str) -> None:
        """
        Closes and removes a client session.
        If no clients remain, automatically stops audio capture to preserve CPU.
        """
        async with self._lock:
            session = self._clients.pop(client_id, None)
            if not session:
                return

            # Cleanup audio subscriber
            if session.subscriber:
                session.subscriber.close()

            # Cleanup peer connection
            if session.pc:
                try:
                    await session.pc.close()
                except Exception as e:
                    logger.debug("Error closing peer connection for %s: %s", client_id, e)

            logger.info(
                "Client removed: %s. Remaining clients: %d",
                client_id,
                len(self._clients),
            )

            # Stop capture when no clients remain
            if len(self._clients) == 0 and self.capture.is_running:
                logger.info("No active clients. Stopping audio capture.")
                self.capture.stop()

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
            if session:
                session.touch()
                if connection_state:
                    session.connection_state = connection_state
                if ice_state:
                    session.ice_state = ice_state

    async def cleanup_stale_connections(self, max_idle_seconds: float = 300.0) -> int:
        """Removes connections that have failed, closed, or timed out."""
        now = time.time()
        to_remove = []
        async with self._lock:
            for cid, session in self._clients.items():
                is_dead = session.connection_state in ("failed", "closed")
                is_stale = (now - session.last_seen) > max_idle_seconds
                if is_dead or is_stale:
                    to_remove.append(cid)

        for cid in to_remove:
            logger.info("Cleaning up stale connection: %s", cid)
            await self.remove_client(cid)

        return len(to_remove)

    async def close_all(self) -> None:
        """Gracefully closes all peer connections and shuts down capture."""
        logger.info("Closing all client connections and stopping capture...")
        cids = list(self._clients.keys())
        for cid in cids:
            await self.remove_client(cid)
        if self.capture.is_running:
            self.capture.stop()


# Global singleton connection manager
connection_manager = ConnectionManager()
