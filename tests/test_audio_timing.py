"""
Unit tests for audio timing and PTS progression (tests/test_audio_timing.py)
Verifies sample-based monotonic PTS math and 20ms frame cadence integrity.
"""

from server.config import (
    FRAME_DURATION_MS,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    BYTES_PER_FRAME_S16,
)
from server.audio_pipeline import AudioPipeline


def test_frame_samples_math():
    """Verify that 48000 Hz at 20 ms gives exactly 960 samples."""
    expected_samples = int(48000 * (20 / 1000.0))
    assert FRAME_SAMPLES == expected_samples
    assert FRAME_SAMPLES == 960

    # 960 samples * 2 channels * 2 bytes/sample (s16le) = 3840 bytes
    expected_bytes = expected_samples * 2 * 2
    assert BYTES_PER_FRAME_S16 == expected_bytes
    assert BYTES_PER_FRAME_S16 == 3840


def test_pts_monotonic_integrity_under_load():
    """Verify that PTS never moves backwards or drifts across 1000 continuous frames."""
    pipeline = AudioPipeline()
    last_pts = -1

    for frame_idx in range(1000):
        expected_pts = frame_idx * 960
        frame = pipeline.publish_pcm(b"\x00" * BYTES_PER_FRAME_S16)

        assert frame.pts == expected_pts
        assert frame.pts > last_pts
        last_pts = frame.pts

    # Total samples processed after 1000 frames of 20ms:
    # 1000 * 960 = 960,000 samples = exactly 20.0 seconds of audio timeline
    assert pipeline.current_pts == 960_000
