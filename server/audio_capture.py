"""
AirBridge - Windows WASAPI Audio Capture

Responsibility:
    ONLY Windows system audio capture via SoundCard WASAPI loopback.
    Decoupled from WebRTC, WebSockets, networking, and clients.
"""

import logging
import os
import sys
import threading
from typing import Callable, Optional, Any

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import numpy as np
import soundcard as sc

from server.config import (
    CHANNELS,
    FRAME_SAMPLES,
    SAMPLE_RATE,
)

logger = logging.getLogger("AirBridge.AudioCapture")


class AudioCapture:
    """
    Manages continuous loopback audio capture from the Windows default output device.
    Converts 48kHz float32 audio to normalized 16-bit signed PCM (s16le).
    Thread-safe and race-safe lifecycle control.
    """

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        frame_samples: int = FRAME_SAMPLES,
        on_frame: Optional[Callable[[bytes], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
        recorder_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_samples = frame_samples
        self.on_frame = on_frame
        self.on_error = on_error
        self.recorder_factory = recorder_factory

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._speaker = None
        self._loopback = None
        self._last_error: Optional[Exception] = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running and self._thread is not None and self._thread.is_alive()

    @property
    def last_error(self) -> Optional[Exception]:
        with self._lock:
            return self._last_error

    @property
    def device_name(self) -> str:
        try:
            if not self._speaker:
                self._speaker = sc.default_speaker()
            return self._speaker.name
        except Exception:
            return "Unknown Device"

    def read_frame(self) -> bytes:
        """
        Synchronously captures a single audio frame (20ms) from WASAPI loopback.
        Useful for testing and single-frame pull workflows.
        """
        if self.recorder_factory:
            recorder_cm = self.recorder_factory()
        else:
            speaker = sc.default_speaker()
            loopback = sc.get_microphone(speaker.name, include_loopback=True)
            recorder_cm = loopback.recorder(
                samplerate=self.sample_rate,
                channels=self.channels,
                blocksize=self.frame_samples,
            )

        with recorder_cm as recorder:
            data = recorder.record(numframes=self.frame_samples)
            data = np.asarray(data, dtype=np.float32)
            data_s16 = np.clip(data * 32767.0, -32768.0, 32767.0).astype(np.int16)
            return data_s16.tobytes()

    def start(self) -> None:
        """Starts continuous loopback capture in a background thread."""
        with self._lock:
            if self._running and self._thread is not None and self._thread.is_alive():
                return

            # If previous thread is still alive, wait briefly
            if self._thread is not None and self._thread.is_alive():
                logger.warning("Previous capture thread is still alive; waiting before restart.")
                self._thread.join(timeout=1.0)
                if self._thread.is_alive():
                    raise RuntimeError("Cannot start capture: previous capture thread is still running")

            self._stop_event.clear()
            self._last_error = None
            self._running = True

            try:
                self._thread = threading.Thread(
                    target=self._capture_worker,
                    daemon=True,
                    name="AirBridgeAudioCaptureWorker",
                )
                self._thread.start()
                logger.info("AudioCapture background thread started.")
            except Exception as e:
                self._running = False
                self._thread = None
                self._last_error = e
                logger.error("Failed to start AudioCapture thread: %s", e)
                raise

    def stop(self, timeout: float = 2.0) -> bool:
        """
        Stops loopback capture cleanly.
        Returns True if the thread terminated, or False if the join timed out.
        """
        with self._lock:
            if not self._running and (self._thread is None or not self._thread.is_alive()):
                return True

            self._running = False
            self._stop_event.set()
            thread = self._thread

        stopped = True
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning(
                    "AudioCapture thread did not terminate within %.1f second timeout.",
                    timeout,
                )
                stopped = False
            else:
                logger.info("AudioCapture background thread stopped cleanly.")

        with self._lock:
            if stopped:
                self._thread = None

        return stopped

    def _capture_worker(self) -> None:
        """Dedicated background loop capturing continuous WASAPI loopback blocks."""
        try:
            if self.recorder_factory:
                recorder_cm = self.recorder_factory()
            else:
                self._speaker = sc.default_speaker()
                self._loopback = sc.get_microphone(
                    self._speaker.name, include_loopback=True
                )
                logger.info("Capturing loopback from: %s", self._speaker.name)
                recorder_cm = self._loopback.recorder(
                    samplerate=self.sample_rate,
                    channels=self.channels,
                    blocksize=self.frame_samples,
                )

            with recorder_cm as recorder:
                while self._running and not self._stop_event.is_set():
                    # Synchronous blocking read of 20ms (960 frames)
                    data = recorder.record(numframes=self.frame_samples)
                    data = np.asarray(data, dtype=np.float32)

                    # Convert float32 [-1.0, 1.0] to int16 [-32768, 32767]
                    data_s16 = np.clip(
                        data * 32767.0, -32768.0, 32767.0
                    ).astype(np.int16)
                    pcm_bytes = data_s16.tobytes()

                    if self.on_frame and not self._stop_event.is_set():
                        self.on_frame(pcm_bytes)

        except Exception as e:
            logger.error("Exception in AudioCapture loop: %s", e)
            with self._lock:
                self._last_error = e
            if self.on_error:
                try:
                    self.on_error(e)
                except Exception as cb_err:
                    logger.debug("Error in on_error callback: %s", cb_err)
        finally:
            with self._lock:
                self._running = False


def main():
    """Standalone live capture test (verifies WASAPI loopback)."""
    speakers = sc.all_speakers()
    print("Available speakers:")
    for speaker in speakers:
        print(f"  - {speaker.name}")

    capture = AudioCapture()
    print()
    print(f"Default speaker: {capture.device_name}")
    print("Starting WASAPI loopback capture test...")
    print("Play some audio on Windows. Press Ctrl+C to stop.")
    print()

    try:
        speaker = sc.default_speaker()
        loopback = sc.get_microphone(speaker.name, include_loopback=True)
        with loopback.recorder(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            blocksize=FRAME_SAMPLES,
        ) as recorder:
            while True:
                data = recorder.record(numframes=FRAME_SAMPLES)
                data = np.asarray(data, dtype=np.float32)
                peak = np.max(np.abs(data))
                print(
                    f"\rSamples: {data.shape} | Peak: {peak:.4f}",
                    end="",
                    flush=True,
                )
    except KeyboardInterrupt:
        print("\nCapture stopped by user.")


if __name__ == "__main__":
    main()