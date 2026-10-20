"""
AirBridge - Central Audio Pipeline

Responsibilities:
    - Normalizes PCM audio frames.
    - Manages monotonic PTS (Presentation Time Stamp) clock.
    - Bounded subscriber buffers with drop-oldest policy (Low Latency First).
    - Multi-peer fan-out: ONE capture source -> MULTIPLE WebRTC consumers.
    - Thread-safe publishing from background capture worker to asyncio consumers.
    - Telemetry metrics tracking (produced, consumed, dropped, overruns, underruns).
"""

import asyncio
import collections
import logging
import threading
from typing import Dict, Optional, Set

from server.config import (
    BYTES_PER_FRAME_S16,
    FRAME_SAMPLES,
    MAX_BUFFER_FRAMES,
    SAMPLE_RATE,
    CHANNELS,
)
from server.models import AudioFrameData, PipelineMetrics

logger = logging.getLogger("AirBridge.AudioPipeline")


class AudioSubscriber:
    """
    Consumer handle for a single WebRTC audio track or listener.
    Maintains a bounded deque and async signaling event.
    """

    def __init__(
        self,
        subscriber_id: str,
        pipeline: "AudioPipeline",
        max_buffer_frames: int = MAX_BUFFER_FRAMES,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self.subscriber_id = subscriber_id
        self._pipeline = pipeline
        self.max_buffer_frames = max_buffer_frames
        self._loop = loop or asyncio.get_running_loop()
        self._buffer: collections.deque[AudioFrameData] = collections.deque(
            maxlen=max_buffer_frames
        )
        self._event = asyncio.Event()
        self._lock = asyncio.Lock()
        self.active = True
        self.frames_consumed = 0
        self.frames_dropped = 0
        self.underruns = 0
        self.overruns = 0

    def push_frame(self, frame: AudioFrameData) -> None:
        """
        Pushes a new frame into the subscriber's buffer.
        Thread-safe: scheduled on the event loop.
        """
        def _apply():
            if not self.active:
                return

            if len(self._buffer) >= self.max_buffer_frames:
                # Buffer is full: Drop oldest frame to maintain ultra-low latency
                self._buffer.popleft()
                self.frames_dropped += 1
                self.overruns += 1
                self._pipeline.record_dropped_frame()

            self._buffer.append(frame)
            self._event.set()

        try:
            self._loop.call_soon_threadsafe(_apply)
        except RuntimeError:
            pass

    async def get_frame(self, timeout: float = 0.08) -> AudioFrameData:
        """
        Asynchronously retrieves the next available audio frame.
        Waits for a new frame or generates a silence frame upon underrun/timeout.
        """
        async with self._lock:
            # If buffer has data, pop immediately
            if self._buffer:
                self.frames_consumed += 1
                self._pipeline.record_consumed_frame()
                if not self._buffer:
                    self._event.clear()
                return self._buffer.popleft()

            # Buffer is currently empty: wait for next published frame
            try:
                await asyncio.wait_for(self._event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                # Real underrun: Frame deadline missed, generate silence fallback
                self.underruns += 1
                self._pipeline.record_underrun()
                pts = self._pipeline.current_pts
                silence_bytes = b"\x00" * BYTES_PER_FRAME_S16
                return AudioFrameData(data=silence_bytes, pts=pts)

            if self._buffer:
                self.frames_consumed += 1
                self._pipeline.record_consumed_frame()
                frame = self._buffer.popleft()
                if not self._buffer:
                    self._event.clear()
                return frame
            else:
                pts = self._pipeline.current_pts
                return AudioFrameData(data=b"\x00" * BYTES_PER_FRAME_S16, pts=pts)

    def close(self) -> None:
        """Closes the subscriber and detaches from the pipeline."""
        self.active = False
        self._pipeline.unsubscribe(self.subscriber_id)


class AudioPipeline:
    """
    Central audio distribution pipeline.
    Maintains a single capture source and distributes frames to all subscribers.
    """

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        frame_samples: int = FRAME_SAMPLES,
        max_buffer_frames: int = MAX_BUFFER_FRAMES,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_samples = frame_samples
        self.max_buffer_frames = max_buffer_frames

        self._subscribers: Dict[str, AudioSubscriber] = {}
        self._sub_lock = threading.Lock()

        # Monotonic sample-based PTS counter
        self._current_pts: int = 0
        self._pts_lock = threading.Lock()

        # Metrics telemetry
        self._metrics = PipelineMetrics()
        self._metrics_lock = threading.Lock()

    @property
    def current_pts(self) -> int:
        with self._pts_lock:
            return self._current_pts

    def advance_pts(self) -> int:
        """Atomically advances and returns the current monotonic PTS."""
        with self._pts_lock:
            pts = self._current_pts
            self._current_pts += self.frame_samples
            self._metrics.current_pts = self._current_pts
            return pts

    def record_dropped_frame(self) -> None:
        with self._metrics_lock:
            self._metrics.frames_dropped += 1
            self._metrics.overruns += 1

    def record_consumed_frame(self) -> None:
        with self._metrics_lock:
            self._metrics.frames_consumed += 1

    def record_underrun(self) -> None:
        with self._metrics_lock:
            self._metrics.underruns += 1

    def publish_pcm(self, pcm_bytes: bytes) -> AudioFrameData:
        """
        Publishes a new raw PCM frame into the pipeline.
        Typically called by the background WASAPI audio capture thread.
        """
        pts = self.advance_pts()
        frame = AudioFrameData(
            data=pcm_bytes,
            pts=pts,
            sample_rate=self.sample_rate,
            channels=self.channels,
            samples=self.frame_samples,
        )

        with self._metrics_lock:
            self._metrics.frames_produced += 1

        # Dispatch frame to all active subscribers
        with self._sub_lock:
            current_max_depth = 0
            for sub in list(self._subscribers.values()):
                sub.push_frame(frame)
                depth = len(sub._buffer)
                if depth > current_max_depth:
                    current_max_depth = depth

            with self._metrics_lock:
                self._metrics.buffer_depth = current_max_depth
                if current_max_depth > self._metrics.buffer_peak:
                    self._metrics.buffer_peak = current_max_depth

        return frame

    def subscribe(
        self,
        subscriber_id: str,
        max_buffer_frames: Optional[int] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> AudioSubscriber:
        """Registers a new subscriber."""
        buffer_size = max_buffer_frames or self.max_buffer_frames
        sub = AudioSubscriber(
            subscriber_id=subscriber_id,
            pipeline=self,
            max_buffer_frames=buffer_size,
            loop=loop,
        )
        with self._sub_lock:
            self._subscribers[subscriber_id] = sub
            logger.info(
                "Subscriber attached: %s. Active subscribers: %d",
                subscriber_id,
                len(self._subscribers),
            )
        return sub

    def unsubscribe(self, subscriber_id: str) -> None:
        """Unregisters a subscriber."""
        with self._sub_lock:
            if subscriber_id in self._subscribers:
                del self._subscribers[subscriber_id]
                logger.info(
                    "Subscriber detached: %s. Remaining subscribers: %d",
                    subscriber_id,
                    len(self._subscribers),
                )

    def get_subscriber_count(self) -> int:
        with self._sub_lock:
            return len(self._subscribers)

    def get_metrics(self) -> PipelineMetrics:
        with self._metrics_lock:
            return PipelineMetrics(
                frames_produced=self._metrics.frames_produced,
                frames_consumed=self._metrics.frames_consumed,
                frames_dropped=self._metrics.frames_dropped,
                buffer_depth=self._metrics.buffer_depth,
                buffer_peak=self._metrics.buffer_peak,
                underruns=self._metrics.underruns,
                overruns=self._metrics.overruns,
                current_pts=self.current_pts,
            )

    def reset_metrics(self) -> None:
        with self._metrics_lock:
            self._metrics = PipelineMetrics(current_pts=self.current_pts)


# Global singleton pipeline instance
pipeline = AudioPipeline()
