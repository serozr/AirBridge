"""
Unit tests for ConnectionManager (server/connection_manager.py)
Tests client registration, lifecycle management, duplicate ID rejection,
concurrent removal, capture start/stop synchronization, active WebRTC protection,
and capture error reporting with bounded recovery.
"""

import asyncio
import time
import pytest

from server.connection_manager import ConnectionManager
from server.audio_pipeline import AudioPipeline


class DummyCapture:
    """Mock capture implementation for testing lifecycle without physical sound devices."""
    def __init__(self, on_frame=None, on_error=None):
        self.is_running = False
        self.on_frame = on_frame
        self.on_error = on_error
        self.start_calls = 0
        self.stop_calls = 0
        self.device_name = "Dummy Loopback Speaker"
        self.should_fail_start = False

    def start(self):
        self.start_calls += 1
        if self.should_fail_start:
            raise RuntimeError("Dummy capture hardware failure")
        self.is_running = True

    def stop(self, timeout=2.0):
        self.is_running = False
        self.stop_calls += 1
        return True


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
async def test_duplicate_client_id_rejected():
    """Verify that registering a duplicate explicit client ID raises ValueError."""
    pipeline = AudioPipeline()
    dummy_capture = DummyCapture(on_frame=pipeline.publish_pcm)
    mgr = ConnectionManager(audio_pipeline=pipeline, audio_capture=dummy_capture)

    session1 = await mgr.register_client("127.0.0.1:1001", client_id="peer-fixed-1")
    assert session1.client_id == "peer-fixed-1"

    # Attempting to register another client with the exact same ID must fail
    with pytest.raises(ValueError):
        await mgr.register_client("127.0.0.1:1002", client_id="peer-fixed-1")

    # Original session is preserved
    assert mgr.client_count == 1
    existing = await mgr.get_client("peer-fixed-1")
    assert existing is session1

    await mgr.remove_client("peer-fixed-1")
    assert mgr.client_count == 0


@pytest.mark.asyncio
async def test_concurrent_client_removal():
    """Verify that concurrent calls to remove_client operate safely and remove once."""
    pipeline = AudioPipeline()
    dummy_capture = DummyCapture(on_frame=pipeline.publish_pcm)
    mgr = ConnectionManager(audio_pipeline=pipeline, audio_capture=dummy_capture)

    session = await mgr.register_client("127.0.0.1:2001", client_id="concurrent-peer")
    assert mgr.client_count == 1

    # Simulate simultaneous disconnect from WebSocket and WebRTC state change
    await asyncio.gather(
        mgr.remove_client("concurrent-peer"),
        mgr.remove_client("concurrent-peer"),
        mgr.remove_client("concurrent-peer"),
    )

    assert mgr.client_count == 0
    assert dummy_capture.stop_calls == 1


@pytest.mark.asyncio
async def test_rapid_connect_disconnect_race_condition():
    """
    Verify that rapid registration and immediate removal in parallel never leaves
    the audio capture running when zero clients remain.
    """
    pipeline = AudioPipeline()
    dummy_capture = DummyCapture(on_frame=pipeline.publish_pcm)
    mgr = ConnectionManager(audio_pipeline=pipeline, audio_capture=dummy_capture)

    async def connect_and_immediately_leave(cid: str):
        await mgr.register_client("127.0.0.1:3000", client_id=cid)
        await mgr.remove_client(cid)

    # Launch 5 concurrent rapid connect-disconnect operations
    await asyncio.gather(*[
        connect_and_immediately_leave(f"rapid-{i}") for i in range(5)
    ])

    assert mgr.client_count == 0
    # Capture must NOT be running after all rapid clients have left
    assert dummy_capture.is_running is False


@pytest.mark.asyncio
async def test_silent_active_webrtc_client_not_pruned_as_stale():
    """
    CRITICAL: Verify that an active WebRTC streaming client (connection_state == 'connected')
    is NEVER pruned as stale by cleanup_stale_connections, even if last_seen is far in the past.
    """
    pipeline = AudioPipeline()
    dummy_capture = DummyCapture(on_frame=pipeline.publish_pcm)
    mgr = ConnectionManager(audio_pipeline=pipeline, audio_capture=dummy_capture)

    session = await mgr.register_client("192.168.0.50:5000", client_id="active-iphone")
    # Simulate active WebRTC streaming session
    await mgr.update_states(session.client_id, connection_state="connected", ice_state="completed")

    # Artificially age the last_seen timestamp past max_idle_seconds
    session.last_seen = time.time() - 1000.0

    # Run cleanup with short max_idle_seconds
    pruned = await mgr.cleanup_stale_connections(max_idle_seconds=10.0)
    assert pruned == 0
    assert mgr.client_count == 1
    assert dummy_capture.is_running is True

    # Now simulate WebRTC connection failure/disconnect
    await mgr.update_states(session.client_id, connection_state="failed", ice_state="failed")
    pruned_dead = await mgr.cleanup_stale_connections(max_idle_seconds=10.0)
    assert pruned_dead == 1
    assert mgr.client_count == 0
    assert dummy_capture.is_running is False


@pytest.mark.asyncio
async def test_capture_error_reporting_and_health_exposure():
    """Verify that AudioCapture errors update manager state and are exposed in health."""
    pipeline = AudioPipeline()
    dummy_capture = DummyCapture(on_frame=pipeline.publish_pcm)
    mgr = ConnectionManager(audio_pipeline=pipeline, audio_capture=dummy_capture)

    # Initial health
    health_initial = mgr.get_capture_health()
    assert health_initial["status"] == "idle"
    assert health_initial["error"] is None

    # Simulate error callback from capture worker
    simulated_err = RuntimeError("WASAPI loopback endpoint unavailable")
    mgr._handle_capture_error(simulated_err)

    assert mgr.capture_error == "WASAPI loopback endpoint unavailable"
    health_error = mgr.get_capture_health()
    assert health_error["status"] == "error"
    assert "WASAPI loopback endpoint unavailable" in health_error["error"]


@pytest.mark.asyncio
async def test_bounded_capture_recovery():
    """Verify that capture recovery attempts are bounded by MAX_CAPTURE_RETRIES and do not loop."""
    pipeline = AudioPipeline()
    dummy_capture = DummyCapture(on_frame=pipeline.publish_pcm)
    dummy_capture.should_fail_start = True  # Always fails
    mgr = ConnectionManager(audio_pipeline=pipeline, audio_capture=dummy_capture)

    session = await mgr.register_client("127.0.0.1:4000", client_id="listener-during-error")
    assert mgr.client_count == 1

    # Trigger recovery attempts
    for _ in range(mgr.MAX_CAPTURE_RETRIES + 2):
        await mgr._attempt_capture_recovery()

    # Retries must not exceed MAX_CAPTURE_RETRIES
    assert mgr._capture_retry_count == mgr.MAX_CAPTURE_RETRIES
    assert mgr.get_capture_health()["status"] == "error"

    await mgr.remove_client(session.client_id)
