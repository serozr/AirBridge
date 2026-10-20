"""
Unit tests for AudioCapture (server/audio_capture.py)
Tests start/stop lifecycle, idempotency, fake recorder execution, error recovery,
and clean thread termination without physical audio hardware.
"""

import time
import pytest
import numpy as np

from server.config import FRAME_SAMPLES, SAMPLE_RATE, CHANNELS
from server.audio_capture import AudioCapture


class FakeRecorder:
    """Mock SoundCard recorder context manager for testing without physical audio hardware."""

    def __init__(self, num_frames_to_yield: int = 5, sleep_per_frame: float = 0.005):
        self.num_frames_to_yield = num_frames_to_yield
        self.sleep_per_frame = sleep_per_frame
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def record(self, numframes: int):
        self.calls += 1
        time.sleep(self.sleep_per_frame)
        # Return 960x2 float32 zeros
        return np.zeros((numframes, 2), dtype=np.float32)


def test_audio_capture_start_stop_idempotent():
    """Verify that start and stop can be called multiple times without error."""
    capture = AudioCapture(recorder_factory=lambda: FakeRecorder(sleep_per_frame=0.01))

    assert not capture.is_running
    capture.start()
    assert capture.is_running

    # Second start call is a no-op
    capture.start()
    assert capture.is_running

    # Stop cleanly
    stopped = capture.stop(timeout=1.0)
    assert stopped is True
    assert not capture.is_running

    # Second stop call is a clean no-op
    stopped_again = capture.stop(timeout=1.0)
    assert stopped_again is True
    assert not capture.is_running


def test_audio_capture_frame_delivery():
    """Verify that fake recorder frames are delivered to the on_frame callback."""
    received_frames = []

    def on_frame(data: bytes):
        received_frames.append(data)

    capture = AudioCapture(
        on_frame=on_frame,
        recorder_factory=lambda: FakeRecorder(sleep_per_frame=0.005),
    )

    capture.start()
    time.sleep(0.05)
    capture.stop(timeout=1.0)

    assert len(received_frames) >= 2
    # Verify s16 byte length
    assert len(received_frames[0]) == FRAME_SAMPLES * CHANNELS * 2


def test_audio_capture_error_handling_and_retry():
    """Verify that an exception in capture worker triggers on_error and allows restart."""
    errors = []

    class FailingRecorder:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

        def record(self, numframes: int):
            raise RuntimeError("Simulated soundcard device disconnection")

    capture = AudioCapture(
        on_error=lambda err: errors.append(err),
        recorder_factory=lambda: FailingRecorder(),
    )

    capture.start()
    # Wait for the exception to fire
    time.sleep(0.05)

    assert len(errors) == 1
    assert "Simulated soundcard device disconnection" in str(errors[0])
    assert not capture.is_running

    # State was cleanly reset; can stop safely and restart with working recorder
    capture.stop(timeout=1.0)
    capture.recorder_factory = lambda: FakeRecorder(sleep_per_frame=0.005)
    capture.start()
    assert capture.is_running
    time.sleep(0.02)
    capture.stop(timeout=1.0)
    assert not capture.is_running
