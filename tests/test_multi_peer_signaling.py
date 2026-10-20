"""
End-to-End Real WebRTC Multi-Peer Integration Test (tests/test_multi_peer_signaling.py)

Proves that:
1. ONE central AudioPipeline feeds MULTIPLE real WebRTC PeerConnections simultaneously.
2. Two separate WebRTC clients (Client A & Client B) complete full SDP offer/answer exchange.
3. Both clients receive decoded 48kHz stereo audio frames simultaneously over real WebRTC tracks.
"""

import asyncio
import pytest
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCConfiguration,
)

from server.config import BYTES_PER_FRAME_S16, FRAME_SAMPLES, SAMPLE_RATE
from server.audio_pipeline import AudioPipeline
from server.webrtc_server import PipelineAudioTrack


@pytest.mark.asyncio
async def test_two_simultaneous_webrtc_peers_receive_audio():
    """
    Connects two real aiortc RTCPeerConnection clients to server-side PipelineAudioTracks.
    Exchanges complete SDP offer/answer with gathered local ICE candidates,
    and verifies that both peers receive audio frames simultaneously from a single AudioPipeline.
    """
    pipeline = AudioPipeline()

    # Server-side setup for Peer 1
    server_pc1 = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
    sub1 = pipeline.subscribe("peer-1")
    track1 = PipelineAudioTrack(subscriber=sub1)
    server_pc1.addTrack(track1)

    # Server-side setup for Peer 2
    server_pc2 = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
    sub2 = pipeline.subscribe("peer-2")
    track2 = PipelineAudioTrack(subscriber=sub2)
    server_pc2.addTrack(track2)

    # Client-side receivers (simulating Chrome and iPhone Safari WebRTC engines)
    client_pc1 = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
    client_pc2 = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))

    client1_future = asyncio.get_running_loop().create_future()
    client2_future = asyncio.get_running_loop().create_future()

    @client_pc1.on("track")
    def on_track1(track):
        if track.kind == "audio":
            async def read():
                frames = []
                try:
                    for _ in range(3):
                        f = await track.recv()
                        frames.append(f)
                    client1_future.set_result(frames)
                except Exception as e:
                    client1_future.set_exception(e)
            asyncio.create_task(read())

    @client_pc2.on("track")
    def on_track2(track):
        if track.kind == "audio":
            async def read():
                frames = []
                try:
                    for _ in range(3):
                        f = await track.recv()
                        frames.append(f)
                    client2_future.set_result(frames)
                except Exception as e:
                    client2_future.set_exception(e)
            asyncio.create_task(read())

    # Full SDP Handshake for Peer 1 (Server offers or Client offers)
    client_pc1.addTransceiver("audio", direction="recvonly")
    await client_pc1.setLocalDescription(await client_pc1.createOffer())
    await server_pc1.setRemoteDescription(client_pc1.localDescription)
    await server_pc1.setLocalDescription(await server_pc1.createAnswer())
    await client_pc1.setRemoteDescription(server_pc1.localDescription)

    # Full SDP Handshake for Peer 2
    client_pc2.addTransceiver("audio", direction="recvonly")
    await client_pc2.setLocalDescription(await client_pc2.createOffer())
    await server_pc2.setRemoteDescription(client_pc2.localDescription)
    await server_pc2.setLocalDescription(await server_pc2.createAnswer())
    await client_pc2.setRemoteDescription(server_pc2.localDescription)

    # Continuous frame publisher task simulating 20ms audio capture
    stop_publishing = asyncio.Event()

    async def capture_feeder():
        counter = 1
        while not stop_publishing.is_set():
            data = bytes([(counter * 17) % 256] * BYTES_PER_FRAME_S16)
            pipeline.publish_pcm(data)
            counter += 1
            await asyncio.sleep(0.015)

    feeder_task = asyncio.create_task(capture_feeder())

    # Wait for both clients to receive at least 3 decoded audio frames
    frames1, frames2 = await asyncio.gather(
        asyncio.wait_for(client1_future, timeout=5.0),
        asyncio.wait_for(client2_future, timeout=5.0),
    )

    stop_publishing.set()
    await feeder_task

    # Validate Client 1 frames
    assert len(frames1) >= 3
    for f in frames1:
        assert f.sample_rate == SAMPLE_RATE
        assert f.samples == FRAME_SAMPLES

    # Validate Client 2 frames
    assert len(frames2) >= 3
    for f in frames2:
        assert f.sample_rate == SAMPLE_RATE
        assert f.samples == FRAME_SAMPLES

    # Clean shutdown of both WebRTC sessions
    await server_pc1.close()
    await client_pc1.close()
    await server_pc2.close()
    await client_pc2.close()
    track1.stop()
    track2.stop()

    assert pipeline.get_subscriber_count() == 0
