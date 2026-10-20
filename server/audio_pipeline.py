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
from typing import Dict, Optional, List

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
    Maintains a bounded deque and async signaling event with thread-safe synchronization.
    """

    def __init__(
        self,
        subscriber_id: str,
        pipeline: "AudioPipeline",
        max_buffer_frames: int = MAX_BUFFER_FRAMES,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        if max_buffer_frames <= 0:
            raise ValueError(
                f"max_buffer_frames must be positive, got {max_buffer_frames}"
            )

        self.subscriber_id = subscriber_id
        self._pipeline = pipeline
        self.max_buffer_frames = max_buffer_frames
        try:
            self._loop = loop or asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        # Thread-safe buffer protected by threading.Lock
        self._buffer: collections.deque[AudioFrameData] = collections.deque(
            maxlen=max_buffer_frames
        )
        self._thread_lock = threading.Lock()

        # Async notification event and bounded scheduling state
        self._event = asyncio.Event()
        self._notify_pending: bool = False

        # Monotonic track PTS tracking to guarantee no backwards jumps or repeats
        self._last_delivered_pts: Optional[int] = None

        self.active: bool = True
        self.frames_consumed: int = 0
        self.frames_dropped: int = 0
        self.underruns: int = 0
        self.overruns: int = 0

    @property
    def buffer_depth(self) -> int:
        """Thread-safe inspection of current subscriber buffer length."""
        with self._thread_lock:
            return len(self._buffer)

    def push_frame(self, frame: AudioFrameData) -> None:
        """
        Pushes a new frame into the subscriber's buffer.
        Thread-safe: called by background capture worker thread.
        Limits pending event-loop notification callbacks to at most ONE at any time.
        """
        with self._thread_lock:
            if not self.active:
                return

            if len(self._buffer) >= self.max_buffer_frames:
                # Buffer is full: Drop oldest frame to maintain low latency
                self._buffer.popleft()
                self.frames_dropped += 1
                self.overruns += 1
                self._pipeline.record_dropped_frame()

            self._buffer.append(frame)

            # Bound event-loop scheduling: at most one wake callback is in flight
            if not self._notify_pending:
                self._notify_pending = True
                should_schedule = True
            else:
                should_schedule = False

        if should_schedule:
            if self._loop is None:
                try:
                    self._loop = asyncio.get_running_loop()
                except RuntimeError:
                    pass

            if self._loop is not None:
                try:
                    self._loop.call_soon_threadsafe(self._wake_consumer)
                except RuntimeError as e:
                    with self._thread_lock:
                        self._notify_pending = False
                    logger.debug(
                        "Event loop unavailable in push_frame for subscriber %s: %s",
                        self.subscriber_id,
                        e,
                    )
            else:
                with self._thread_lock:
                    self._notify_pending = False

    def _wake_consumer(self) -> None:
        """Executed on the asyncio event loop thread to set the notification event."""
        with self._thread_lock:
            self._notify_pending = False
        self._event.set()

    def _ensure_monotonic_pts(self, frame: AudioFrameData) -> AudioFrameData:
        """Ensures that the delivered frame PTS strictly advances."""
        if self._last_delivered_pts is None:
            self._last_delivered_pts = frame.pts
            return frame

        if frame.pts <= self._last_delivered_pts:
            forced_pts = self._last_delivered_pts + self._pipeline.frame_samples
            self._last_delivered_pts = forced_pts
            return AudioFrameData(
                data=frame.data,
                pts=forced_pts,
                sample_rate=frame.sample_rate,
                channels=frame.channels,
                samples=frame.samples,
            )

        self._last_delivered_pts = frame.pts
        return frame

    def _generate_silence_frame(self) -> AudioFrameData:
        """Generates a silence frame upon underrun with strictly advancing PTS."""
        if self._last_delivered_pts is None:
            pts = self._pipeline.current_pts
        else:
            pts = max(
                self._last_delivered_pts + self._pipeline.frame_samples,
                self._pipeline.current_pts,
            )
        self._last_delivered_pts = pts
        silence_bytes = b"\x00" * BYTES_PER_FRAME_S16
        return AudioFrameData(
            data=silence_bytes,
            pts=pts,
            sample_rate=self._pipeline.sample_rate,
            channels=self._pipeline.channels,
            samples=self._pipeline.frame_samples,
        )

    async def get_frame(self, timeout: float = 0.08) -> AudioFrameData:
        """
        Asynchronously retrieves the next available audio frame.
        Waits for a new frame or generates a silence frame upon underrun/timeout.
        """
        # Fast path: check if buffer has data immediately
        with self._thread_lock:
            if self._buffer:
                frame = self._buffer.popleft()
                self.frames_consumed += 1
                if not self._buffer:
                    self._event.clear()
                self._pipeline.record_consumed_frame()
                return self._ensure_monotonic_pts(frame)
            else:
                self._event.clear()

        # Buffer is empty: wait for next published frame
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            # Underrun: Frame deadline missed, generate silence fallback with advancing PTS
            self.underruns += 1
            self._pipeline.record_underrun()
            return self._generate_silence_frame()

        with self._thread_lock:
            if self._buffer:
                frame = self._buffer.popleft()
                self.frames_consumed += 1
                if not self._buffer:
                    self._event.clear()
                self._pipeline.record_consumed_frame()
                return self._ensure_monotonic_pts(frame)
            else:
                self._event.clear()
                self.underruns += 1
                self._pipeline.record_underrun()
                return self._generate_silence_frame()

    def close(self) -> None:
        """Closes the subscriber and detaches from the pipeline."""
        with self._thread_lock:
            self.active = False
            self._buffer.clear()
            self._event.set()
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
        Called by the background WASAPI audio capture thread.
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
            subs = list(self._subscribers.values())

        current_max_depth = 0
        for sub in subs:
            sub.push_frame(frame)
            depth = sub.buffer_depth
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
        """Registers a new subscriber with unique subscriber ID and validated buffer size."""
        buffer_size = self.max_buffer_frames if max_buffer_frames is None else max_buffer_frames
        if buffer_size <= 0:
            raise ValueError(
                f"Invalid max_buffer_frames: {buffer_size}. Must be greater than 0."
            )

        with self._sub_lock:
            if subscriber_id in self._subscribers:
                raise ValueError(
                    f"Subscriber ID '{subscriber_id}' is already registered."
                )

            sub = AudioSubscriber(
                subscriber_id=subscriber_id,
                pipeline=self,
                max_buffer_frames=buffer_size,
                loop=loop,
            )
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
        """Returns a snapshot of pipeline metrics with thread-safe subscriber buffer depth."""
        with self._sub_lock:
            subs = list(self._subscribers.values())
        active_depth = max((s.buffer_depth for s in subs), default=0)

        with self._metrics_lock:
            return PipelineMetrics(
                frames_produced=self._metrics.frames_produced,
                frames_consumed=self._metrics.frames_consumed,
                frames_dropped=self._metrics.frames_dropped,
                buffer_depth=active_depth,
                buffer_peak=max(self._metrics.buffer_peak, active_depth),
                underruns=self._metrics.underruns,
                overruns=self._metrics.overruns,
                current_pts=self.current_pts,
            )

    def reset_metrics(self) -> None:
        with self._metrics_lock:
            self._metrics = PipelineMetrics(current_pts=self.current_pts)


# Global singleton pipeline instance
pipeline = AudioPipeline()
