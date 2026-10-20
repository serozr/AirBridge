"""
Integration test: AudioPipeline -> PipelineAudioTrack -> av.AudioFrame
Verifies WebRTC audio track consumption, PTS progression, underrun recovery,
and clean teardown.
"""

import asyncio
import av
import pytest

from server.config import BYTES_PER_FRAME_S16, FRAME_SAMPLES, SAMPLE_RATE
from server.audio_pipeline import AudioPipeline
from server.webrtc_server import PipelineAudioTrack


@pytest.mark.asyncio
async def test_pipeline_to_webrtc_track_integration():
    """
    Verifies that AudioPipeline produces frames and PipelineAudioTrack converts them
    to av.AudioFrame instances with exact 48kHz stereo 20ms specifications.
    """
    pipeline = AudioPipeline()
    subscriber = pipeline.subscribe("test-track-sub")
    track = PipelineAudioTrack(subscriber=subscriber)

    # 1. Publish 3 synthetic audio frames into the pipeline
    for i in range(3):
        pcm = bytes([(i + 1) * 10] * BYTES_PER_FRAME_S16)
        pipeline.publish_pcm(pcm)

    # 2. Consume from WebRTC audio track
    frame0 = await track.recv()
    assert isinstance(frame0, av.AudioFrame)
    assert frame0.format.name == "s16"
    assert frame0.layout.name == "stereo"
    assert frame0.samples == FRAME_SAMPLES
    assert frame0.sample_rate == SAMPLE_RATE
    assert frame0.pts == 0

    frame1 = await track.recv()
    assert isinstance(frame1, av.AudioFrame)
    assert frame1.samples == FRAME_SAMPLES
    assert frame1.pts == FRAME_SAMPLES

    frame2 = await track.recv()
    assert isinstance(frame2, av.AudioFrame)
    assert frame2.samples == FRAME_SAMPLES
    assert frame2.pts == FRAME_SAMPLES * 2

    # 3. Clean stop
    track.stop()
    assert subscriber.active is False
    assert pipeline.get_subscriber_count() == 0


@pytest.mark.asyncio
async def test_webrtc_track_consecutive_underruns_and_recovery():
    """
    Verifies that PipelineAudioTrack maintains strictly monotonic PTS across
    consecutive underruns (silence) and recovers when normal audio frames resume.
    """
    pipeline = AudioPipeline()
    subscriber = pipeline.subscribe("test-track-underrun")
    track = PipelineAudioTrack(subscriber=subscriber)

    # Empty buffer: first 2 frames will underrun with short timeout
    silence1 = await track.recv()
    assert silence1.pts is not None

    silence2 = await track.recv()
    assert silence2.pts > silence1.pts
    assert silence2.pts == silence1.pts + FRAME_SAMPLES

    # Now real audio resumes
    pcm = bytes([55] * BYTES_PER_FRAME_S16)
    pipeline.publish_pcm(pcm)

    audio_frame = await track.recv()
    assert audio_frame.pts > silence2.pts
    assert bytes(audio_frame.planes[0]) == pcm

    track.stop()
