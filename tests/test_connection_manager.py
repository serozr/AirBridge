"""
Unit tests for ConnectionManager (server/connection_manager.py)
Tests client registration, lifecycle management, duplicate ID rejection,
concurrent removal, capture start/stop synchronization, active WebRTC protection,
thread-safe background error dispatching, flapping crash mitigation,
stop timeout reporting, and heartbeat tracking.
"""

import asyncio
import threading
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
        self.stop_return_value = True

    def start(self):
        self.start_calls += 1
        if self.should_fail_start:
            raise RuntimeError("Dummy capture hardware failure")
        self.is_running = True

    def stop(self, timeout=2.0):
        self.stop_calls += 1
        if self.stop_return_value:
            self.is_running = False
            return True
        else:
            # Simulate thread join timeout
            self.is_running = True
            return False


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

    with pytest.raises(ValueError):
        await mgr.register_client("127.0.0.1:1002", client_id="peer-fixed-1")

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

    await asyncio.gather(*[
        connect_and_immediately_leave(f"rapid-{i}") for i in range(5)
    ])

    assert mgr.client_count == 0
    assert dummy_capture.is_running is False


@pytest.mark.asyncio
async def test_silent_active_webrtc_client_not_pruned_as_stale():
    """
    Verify that an active WebRTC streaming client with recent heartbeat is preserved,
    and pruned if inactive beyond max_idle_seconds or if WebRTC fails.
    """
    pipeline = AudioPipeline()
    dummy_capture = DummyCapture(on_frame=pipeline.publish_pcm)
    mgr = ConnectionManager(audio_pipeline=pipeline, audio_capture=dummy_capture)

    session = await mgr.register_client("192.168.0.50:5000", client_id="active-iphone")
    await mgr.update_states(session.client_id, connection_state="connected", ice_state="completed")

    # Client has recent heartbeat: not pruned
    pruned = await mgr.cleanup_stale_connections(max_idle_seconds=60.0)
    assert pruned == 0
    assert mgr.client_count == 1

    # Client inactive for > max_idle_seconds (e.g. 100s > 60s)
    session.last_seen = time.time() - 100.0
    pruned_idle = await mgr.cleanup_stale_connections(max_idle_seconds=60.0)
    assert pruned_idle == 1
    assert mgr.client_count == 0


@pytest.mark.asyncio
async def test_capture_error_from_background_thread_dispatches_safely():
    """
    Verify that AudioCapture error callback invoked from a non-asyncio OS thread
    uses call_soon_threadsafe and safely triggers recovery without RuntimeError.
    """
    pipeline = AudioPipeline()
    dummy_capture = DummyCapture(on_frame=pipeline.publish_pcm)
    mgr = ConnectionManager(audio_pipeline=pipeline, audio_capture=dummy_capture)

    session = await mgr.register_client("127.0.0.1:6000", client_id="thread-test-client")
    assert mgr.client_count == 1

    error_dispatched = threading.Event()

    def simulate_hardware_worker_crash():
        # Running in pure OS background thread without event loop
        mgr._handle_capture_error(RuntimeError("OS background thread audio crash"))
        error_dispatched.set()

    worker = threading.Thread(target=simulate_hardware_worker_crash)
    worker.start()
    worker.join()

    assert error_dispatched.is_set()
    assert mgr.capture_error == "OS background thread audio crash"

    # Give event loop a cycle to schedule the recovery task via call_soon_threadsafe
    await asyncio.sleep(0.05)
    health = mgr.get_capture_health()
    assert health["status"] == "error"
    assert "OS background thread audio crash" in health["error"]

    await mgr.remove_client(session.client_id)


@pytest.mark.asyncio
async def test_flapping_capture_does_not_reset_retries():
    """
    Verify that a flapping capture crash (starts, but crashes before producing stable frames)
    does NOT reset retry count and terminates at MAX_CAPTURE_RETRIES.
    """
    pipeline = AudioPipeline()
    dummy_capture = DummyCapture(on_frame=pipeline.publish_pcm)
    mgr = ConnectionManager(audio_pipeline=pipeline, audio_capture=dummy_capture)

    session = await mgr.register_client("127.0.0.1:7000", client_id="flapping-client")

    # Simulate 4 consecutive crashes without 5 stable frames
    for i in range(1, mgr.MAX_CAPTURE_RETRIES + 2):
        mgr._handle_capture_error(RuntimeError(f"Flapping crash {i}"))
        await mgr._attempt_capture_recovery()

    assert mgr._capture_retry_count == mgr.MAX_CAPTURE_RETRIES
    health = mgr.get_capture_health()
    assert health["status"] == "error"

    await mgr.remove_client(session.client_id)


@pytest.mark.asyncio
async def test_stop_timeout_reports_error_health():
    """Verify that when capture.stop() returns False (timeout), health reports error."""
    pipeline = AudioPipeline()
    dummy_capture = DummyCapture(on_frame=pipeline.publish_pcm)
    dummy_capture.stop_return_value = False  # Simulate thread hung / timeout
    mgr = ConnectionManager(audio_pipeline=pipeline, audio_capture=dummy_capture)

    session = await mgr.register_client("127.0.0.1:8000", client_id="hung-stop-client")
    assert dummy_capture.is_running is True

    # Removing client triggers stop
    await mgr.remove_client(session.client_id)

    # Capture failed to stop cleanly; must be reported as error
    assert mgr.capture_error == "AudioCapture thread failed to stop within timeout"
    health = mgr.get_capture_health()
    assert health["status"] == "error"
    assert "failed to stop within timeout" in health["error"]


@pytest.mark.asyncio
async def test_recovery_cancelled_when_last_client_leaves():
    """Verify that if the last client leaves while recovery is in progress, capture is not restarted."""
    pipeline = AudioPipeline()
    dummy_capture = DummyCapture(on_frame=pipeline.publish_pcm)
    mgr = ConnectionManager(audio_pipeline=pipeline, audio_capture=dummy_capture)

    session = await mgr.register_client("127.0.0.1:9000", client_id="leaving-during-recovery")

    # Trigger error
    mgr._handle_capture_error(RuntimeError("Hardware glitch"))
    # Client immediately disconnects before recovery backoff completes
    await mgr.remove_client(session.client_id)
    assert mgr.client_count == 0

    await asyncio.sleep(0.1)
    # Capture must remain stopped because 0 clients exist
    assert dummy_capture.is_running is False


@pytest.mark.asyncio
async def test_heartbeat_updates_last_seen():
    """Verify that touch() updates last_seen timestamp."""
    pipeline = AudioPipeline()
    dummy_capture = DummyCapture(on_frame=pipeline.publish_pcm)
    mgr = ConnectionManager(audio_pipeline=pipeline, audio_capture=dummy_capture)

    session = await mgr.register_client("127.0.0.1:9500", client_id="touch-client")
    t0 = session.last_seen
    await asyncio.sleep(0.02)
    session.touch()
    assert session.last_seen > t0

    await mgr.remove_client(session.client_id)
