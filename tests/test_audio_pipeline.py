"""
Unit tests for AudioPipeline (server/audio_pipeline.py)
Tests bounded buffer, drop-oldest frame policy, monotonic PTS, and metrics.
"""

import asyncio
import pytest

from server.config import (
    BYTES_PER_FRAME_S16,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    CHANNELS,
)
from server.models import AudioFrameData
from server.audio_pipeline import AudioPipeline


def generate_synthetic_frame(fill_byte: int = 0) -> bytes:
    """Creates a deterministic synthetic PCM frame (3840 bytes)."""
    return bytes([fill_byte % 256] * BYTES_PER_FRAME_S16)


def test_audio_frame_data_validation():
    """Verify that invalid payload sizes are rejected immediately."""
    valid_data = generate_synthetic_frame(1)
    frame = AudioFrameData(data=valid_data, pts=0)
    assert frame.pts == 0
    assert frame.samples == FRAME_SAMPLES
    assert len(frame.data) == BYTES_PER_FRAME_S16

    with pytest.raises(ValueError):
        AudioFrameData(data=b"\x00" * 100, pts=0)


def test_monotonic_pts_progression():
    """Verify that PTS advances monotonically by exactly FRAME_SAMPLES on every frame."""
    pipeline = AudioPipeline()
    assert pipeline.current_pts == 0

    frame1 = pipeline.publish_pcm(generate_synthetic_frame(1))
    assert frame1.pts == 0
    assert pipeline.current_pts == FRAME_SAMPLES

    frame2 = pipeline.publish_pcm(generate_synthetic_frame(2))
    assert frame2.pts == FRAME_SAMPLES
    assert pipeline.current_pts == FRAME_SAMPLES * 2

    frame3 = pipeline.publish_pcm(generate_synthetic_frame(3))
    assert frame3.pts == FRAME_SAMPLES * 2
    assert pipeline.current_pts == FRAME_SAMPLES * 3


@pytest.mark.asyncio
async def test_bounded_buffer_and_oldest_frame_drop():
    """
    Verify that when buffer reaches max_buffer_frames,
    the oldest frame is dropped, newest frame is preserved,
    and dropped_frames metric is incremented.
    """
    max_frames = 3
    pipeline = AudioPipeline(max_buffer_frames=max_frames)
    subscriber = pipeline.subscribe("test-sub-1", max_buffer_frames=max_frames)

    # Push 4 frames into a buffer of max_frames=3
    # Frame 1: fill_byte = 1
    # Frame 2: fill_byte = 2
    # Frame 3: fill_byte = 3
    # Frame 4: fill_byte = 4 (should cause Frame 1 to be dropped)
    for i in range(1, 5):
        pipeline.publish_pcm(generate_synthetic_frame(i))

    # Give event loop a cycle to schedule the threadsafe pushes
    await asyncio.sleep(0.02)

    metrics = pipeline.get_metrics()
    assert metrics.frames_produced == 4
    assert metrics.frames_dropped == 1
    assert metrics.overruns == 1
    assert subscriber.frames_dropped == 1
    assert len(subscriber._buffer) == 3

    # The buffer should now contain frames 2, 3, 4 (Frame 1 was dropped)
    f2 = await subscriber.get_frame(timeout=0.05)
    assert f2.data[0] == 2

    f3 = await subscriber.get_frame(timeout=0.05)
    assert f3.data[0] == 3

    f4 = await subscriber.get_frame(timeout=0.05)
    assert f4.data[0] == 4

    subscriber.close()


@pytest.mark.asyncio
async def test_underrun_detection_and_silence_fallback():
    """
    Verify that consuming from an empty buffer records an underrun
    and returns a silence frame if timeout expires.
    """
    pipeline = AudioPipeline()
    subscriber = pipeline.subscribe("test-sub-underrun")

    # Buffer is empty: get_frame with short timeout should detect underrun and fallback to silence
    frame = await subscriber.get_frame(timeout=0.03)
    assert len(frame.data) == BYTES_PER_FRAME_S16
    assert frame.data == b"\x00" * BYTES_PER_FRAME_S16
    assert subscriber.underruns >= 1

    metrics = pipeline.get_metrics()
    assert metrics.underruns >= 1
    subscriber.close()


@pytest.mark.asyncio
async def test_multi_subscriber_fanout():
    """
    Verify that a single published frame is dispatched to multiple independent subscribers.
    """
    pipeline = AudioPipeline()
    sub_a = pipeline.subscribe("client-a")
    sub_b = pipeline.subscribe("client-b")

    pipeline.publish_pcm(generate_synthetic_frame(42))
    await asyncio.sleep(0.01)

    frame_a = await sub_a.get_frame(timeout=0.05)
    frame_b = await sub_b.get_frame(timeout=0.05)

    assert frame_a.data == frame_b.data
    assert frame_a.pts == frame_b.pts
    assert frame_a.data[0] == 42

    sub_a.close()
    sub_b.close()
    assert pipeline.get_subscriber_count() == 0
