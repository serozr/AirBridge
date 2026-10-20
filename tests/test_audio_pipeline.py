"""
Unit tests for AudioPipeline (server/audio_pipeline.py)
Tests bounded buffer, drop-oldest frame policy, monotonic PTS, underrun recovery,
and multi-subscriber isolation.
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
async def test_invalid_buffer_size_rejected():
    """Verify that invalid buffer sizes are rejected."""
    pipeline = AudioPipeline()
    with pytest.raises(ValueError):
        pipeline.subscribe("sub-invalid-0", max_buffer_frames=0)
    with pytest.raises(ValueError):
        pipeline.subscribe("sub-invalid-neg", max_buffer_frames=-5)


@pytest.mark.asyncio
async def test_duplicate_subscriber_id_rejected():
    """Verify that registering duplicate subscriber IDs raises ValueError."""
    pipeline = AudioPipeline()
    sub1 = pipeline.subscribe("sub-dup-1")
    assert sub1 is not None

    with pytest.raises(ValueError):
        pipeline.subscribe("sub-dup-1")

    sub1.close()
    # After close, ID can be re-used
    sub2 = pipeline.subscribe("sub-dup-1")
    assert sub2 is not None
    sub2.close()


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
    for i in range(1, 5):
        pipeline.publish_pcm(generate_synthetic_frame(i))

    await asyncio.sleep(0.01)

    metrics = pipeline.get_metrics()
    assert metrics.frames_produced == 4
    assert metrics.frames_dropped == 1
    assert metrics.overruns == 1
    assert subscriber.frames_dropped == 1
    assert subscriber.buffer_depth == 3

    # The buffer should now contain frames 2, 3, 4 (Frame 1 was dropped)
    f2 = await subscriber.get_frame(timeout=0.05)
    assert f2.data[0] == 2

    f3 = await subscriber.get_frame(timeout=0.05)
    assert f3.data[0] == 3

    f4 = await subscriber.get_frame(timeout=0.05)
    assert f4.data[0] == 4

    subscriber.close()


@pytest.mark.asyncio
async def test_consecutive_underruns_advance_pts():
    """
    Verify that consecutive underruns return silence frames with strictly
    advancing PTS (never repeated PTS).
    """
    pipeline = AudioPipeline()
    subscriber = pipeline.subscribe("test-sub-underrun-pts")

    silence1 = await subscriber.get_frame(timeout=0.02)
    assert silence1.data == b"\x00" * BYTES_PER_FRAME_S16
    assert subscriber.underruns == 1

    silence2 = await subscriber.get_frame(timeout=0.02)
    assert silence2.data == b"\x00" * BYTES_PER_FRAME_S16
    assert subscriber.underruns == 2
    assert silence2.pts == silence1.pts + FRAME_SAMPLES

    silence3 = await subscriber.get_frame(timeout=0.02)
    assert silence3.data == b"\x00" * BYTES_PER_FRAME_S16
    assert subscriber.underruns == 3
    assert silence3.pts == silence2.pts + FRAME_SAMPLES

    subscriber.close()


@pytest.mark.asyncio
async def test_frame_arriving_during_wait():
    """
    Verify that a consumer waiting on an empty buffer wakes up immediately
    when a producer publishes a frame.
    """
    pipeline = AudioPipeline()
    subscriber = pipeline.subscribe("test-sub-wait")

    async def producer():
        await asyncio.sleep(0.03)
        pipeline.publish_pcm(generate_synthetic_frame(99))

    producer_task = asyncio.create_task(producer())

    # get_frame waits up to 0.1s; producer publishes at 0.03s
    frame = await subscriber.get_frame(timeout=0.1)
    await producer_task

    assert frame.data[0] == 99
    assert subscriber.underruns == 0
    subscriber.close()


@pytest.mark.asyncio
async def test_dropped_frames_followed_by_silence():
    """
    Verify that when frames are dropped and the buffer is drained,
    subsequent silence frames advance monotonically from the last delivered PTS.
    """
    pipeline = AudioPipeline()
    subscriber = pipeline.subscribe("test-drop-silence", max_buffer_frames=2)

    # Publish 4 frames (frames 0, 1 dropped; 2, 3 in buffer)
    for i in range(4):
        pipeline.publish_pcm(generate_synthetic_frame(i))

    f2 = await subscriber.get_frame(timeout=0.05)
    f3 = await subscriber.get_frame(timeout=0.05)
    assert f3.pts > f2.pts

    # Buffer is now empty; underrun should yield silence with pts > f3.pts
    silence = await subscriber.get_frame(timeout=0.02)
    assert silence.pts >= f3.pts + FRAME_SAMPLES
    assert silence.data == b"\x00" * BYTES_PER_FRAME_S16

    subscriber.close()


@pytest.mark.asyncio
async def test_subscriber_buffer_isolation():
    """
    Verify that a slow subscriber buffer overflow does not affect a fast subscriber.
    """
    pipeline = AudioPipeline()
    fast_sub = pipeline.subscribe("fast-sub", max_buffer_frames=10)
    slow_sub = pipeline.subscribe("slow-sub", max_buffer_frames=2)

    for i in range(5):
        pipeline.publish_pcm(generate_synthetic_frame(i))

    # Slow sub dropped 3 frames
    assert slow_sub.frames_dropped == 3
    assert slow_sub.buffer_depth == 2

    # Fast sub dropped 0 frames
    assert fast_sub.frames_dropped == 0
    assert fast_sub.buffer_depth == 5

    fast_sub.close()
    slow_sub.close()
