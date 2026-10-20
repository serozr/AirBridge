"""
Unit tests for ConnectionManager (server/connection_manager.py)
Tests client registration, lifecycle management, and automatic capture start/stop.
"""

import asyncio
import pytest

from server.connection_manager import ConnectionManager
from server.audio_pipeline import AudioPipeline


class DummyCapture:
    """Mock capture implementation for testing lifecycle without physical sound devices."""
    def __init__(self, on_frame=None):
        self.is_running = False
        self.on_frame = on_frame
        self.start_calls = 0
        self.stop_calls = 0

    def start(self):
        self.is_running = True
        self.start_calls += 1

    def stop(self):
        self.is_running = False
        self.stop_calls += 1


@pytest.mark.asyncio
async def test_client_registration_and_removal():
    """Verify unique client ID generation, registration, and removal."""
    pipeline = AudioPipeline()
    dummy_capture = DummyCapture(on_frame=pipeline.publish_pcm)
    mgr = ConnectionManager(audio_pipeline=pipeline, audio_capture=dummy_capture)

    # 1. Register first client
    session1 = await mgr.register_client("192.168.1.50:50001")
    assert session1.client_id is not None
    assert session1.client_address == "192.168.1.50:50001"
    assert mgr.client_count == 1
    assert dummy_capture.is_running is True
    assert dummy_capture.start_calls == 1

    # 2. Register second client
    session2 = await mgr.register_client("192.168.1.51:50002")
    assert session2.client_id != session1.client_id
    assert mgr.client_count == 2
    # Capture should already be running, no second start call
    assert dummy_capture.start_calls == 1

    # 3. Query clients
    client = await mgr.get_client(session1.client_id)
    assert client is session1
    all_clients = await mgr.get_clients()
    assert len(all_clients) == 2

    # 4. Remove first client (one remains, capture should stay running)
    await mgr.remove_client(session1.client_id)
    assert mgr.client_count == 1
    assert dummy_capture.is_running is True

    # 5. Remove second client (zero remain, capture should automatically stop)
    await mgr.remove_client(session2.client_id)
    assert mgr.client_count == 0
    assert dummy_capture.is_running is False
    assert dummy_capture.stop_calls == 1


@pytest.mark.asyncio
async def test_stale_connection_cleanup():
    """Verify that stale or dead connections are detected and pruned."""
    pipeline = AudioPipeline()
    dummy_capture = DummyCapture(on_frame=pipeline.publish_pcm)
    mgr = ConnectionManager(audio_pipeline=pipeline, audio_capture=dummy_capture)

    session = await mgr.register_client("127.0.0.1:12345")
    # Mark connection as closed
    await mgr.update_states(session.client_id, connection_state="closed")

    cleaned = await mgr.cleanup_stale_connections(max_idle_seconds=10.0)
    assert cleaned == 1
    assert mgr.client_count == 0
    assert dummy_capture.is_running is False
