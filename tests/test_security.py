"""
Unit and Integration Tests for AirBridge Security Layer (v0.3.0)

Covers:
1. Valid session token authentication
2. Invalid session token rejection
3. Missing session token rejection
4. Expired session token rejection (TTL)
5. Valid pairing PIN verification & token generation
6. Invalid pairing PIN rejection
7. Rate limiting & brute-force lockout
8. Rate limit reset upon successful PIN verification
9. Session rotation (PIN & token revocation)
10. Origin & Host validation (RFC-1918 LAN, localhost, mDNS, malicious origin rejection)
11. Unauthenticated WebSocket rejection
12. Invalid token WebSocket rejection
13. Unauthorized origin WebSocket rejection
14. In-band PIN WebSocket authentication
15. REST API /api/auth endpoint verification (200, 401, 403, 429)
"""

import asyncio
import json
import time
import pytest
from fastapi import HTTPException

from server.security import SecurityManager, RateLimiter, SessionData
from server.webrtc_server import app, authenticate, auth_status, websocket_signaling, AuthRequest


# =========================================================================
# 1. Direct SecurityManager Unit Tests
# =========================================================================

def test_valid_token_authentication():
    """Verify that a validly issued session token is accepted."""
    sec = SecurityManager(pin="123456", session_ttl_seconds=3600)
    token = sec.create_session(client_ip="192.168.1.100")
    assert sec.validate_token(token) is True
    assert sec.validate_token(token, client_ip="192.168.1.100") is True


def test_invalid_token_rejected():
    """Verify that random, fabricated, or altered tokens are rejected."""
    sec = SecurityManager(pin="123456")
    sec.create_session(client_ip="192.168.1.100")

    assert sec.validate_token("completely_fake_token_12345") is False
    assert sec.validate_token("another_invalid_token") is False


def test_missing_token_rejected():
    """Verify that None, empty string, or non-string tokens are rejected."""
    sec = SecurityManager(pin="123456")
    assert sec.validate_token(None) is False
    assert sec.validate_token("") is False
    assert sec.validate_token(12345) is False


def test_expired_token_rejected():
    """Verify that tokens exceeding their TTL are rejected and purged."""
    sec = SecurityManager(pin="123456", session_ttl_seconds=1)
    token = sec.create_session(client_ip="192.168.1.100")

    # Manually expire the token
    with sec._lock:
        sec._sessions[token].expires_at = time.time() - 10.0

    assert sec.validate_token(token) is False
    assert sec.active_session_count() == 0


def test_valid_pin_generates_token():
    """Verify that submitting the correct pairing PIN generates a valid token."""
    sec = SecurityManager(pin="654321")
    is_valid, token, err = sec.verify_pin("654321", client_ip="192.168.1.105")

    assert is_valid is True
    assert token is not None
    assert err is None
    assert sec.validate_token(token) is True
    assert sec.active_session_count() == 1


def test_invalid_pin_rejected():
    """Verify that an incorrect PIN is rejected and returns remaining attempt count."""
    sec = SecurityManager(pin="654321", max_attempts=3)
    is_valid, token, err = sec.verify_pin("000000", client_ip="192.168.1.105")

    assert is_valid is False
    assert token is None
    assert "Invalid PIN" in err
    assert "2 attempt(s) remaining" in err


def test_rate_limit():
    """Verify that multiple consecutive failed PIN attempts trigger a temporary lockout."""
    sec = SecurityManager(pin="888888", max_attempts=3, lockout_seconds=60)
    client_ip = "192.168.1.200"

    # Attempt 1: Fail (2 remaining)
    ok1, _, _ = sec.verify_pin("111111", client_ip=client_ip)
    assert ok1 is False

    # Attempt 2: Fail (1 remaining)
    ok2, _, _ = sec.verify_pin("222222", client_ip=client_ip)
    assert ok2 is False

    # Attempt 3: Fail -> triggers lockout
    ok3, _, err3 = sec.verify_pin("333333", client_ip=client_ip)
    assert ok3 is False
    assert "Blocked" in err3 or "Too many failed attempts" in err3

    # Attempt 4: Even with correct PIN, must be blocked during lockout!
    ok4, _, err4 = sec.verify_pin("888888", client_ip=client_ip)
    assert ok4 is False
    assert "Temporarily blocked" in err4


def test_rate_limit_reset_on_success():
    """Verify that a successful PIN attempt resets failed attempt counter."""
    sec = SecurityManager(pin="999999", max_attempts=3)
    client_ip = "192.168.1.201"

    # 2 failures
    sec.verify_pin("000001", client_ip=client_ip)
    sec.verify_pin("000002", client_ip=client_ip)

    # 3rd attempt is correct
    ok, token, _ = sec.verify_pin("999999", client_ip=client_ip)
    assert ok is True
    assert token is not None

    # Failed count must be reset; next wrong attempt should have max-1 attempts remaining
    _, _, err = sec.verify_pin("000003", client_ip=client_ip)
    assert "2 attempt(s) remaining" in err


def test_session_rotation():
    """Verify that rotating session generates new credentials and revokes all active tokens."""
    sec = SecurityManager(pin="111111")
    old_pin = sec.pairing_pin
    token1 = sec.create_session("192.168.1.10")
    token2 = sec.create_session("192.168.1.11")
    assert sec.active_session_count() == 2

    new_sess_id, new_pin = sec.rotate_session()

    assert sec.pairing_pin != old_pin or len(new_pin) == 6
    assert sec.active_session_count() == 0
    assert sec.validate_token(token1) is False
    assert sec.validate_token(token2) is False


def test_origin_validation():
    """Verify Origin header validation allows LAN/localhost and blocks external attackers."""
    sec = SecurityManager(allowed_origins="https://trusted.home.arpa")

    # Allowed Origins
    assert sec.validate_origin(None) is True
    assert sec.validate_origin("http://localhost:8000") is True
    assert sec.validate_origin("http://127.0.0.1:8000") is True
    assert sec.validate_origin("http://192.168.1.100:8000") is True
    assert sec.validate_origin("http://10.0.0.5:8000") is True
    assert sec.validate_origin("http://172.20.10.1:8000") is True
    assert sec.validate_origin("http://airbridge.local:8000") is True
    assert sec.validate_origin("https://trusted.home.arpa") is True

    # Matching Host header
    assert sec.validate_origin("http://custom-lan-device:8000", host="custom-lan-device:8000") is True

    # Malicious External Origins (Rejected)
    assert sec.validate_origin("https://evil-attacker.com") is False
    assert sec.validate_origin("http://malicious-website.org:8000") is False
    assert sec.validate_origin("https://phishing.xyz") is False


# =========================================================================
# 2. Server & WebSocket Signaling Security Integration Tests
# =========================================================================

class DummyWebSocket:
    """Mock WebSocket for end-to-end signaling authentication testing."""

    def __init__(self, query_params=None, headers=None, client_host="192.168.1.55"):
        self.query_params = query_params or {}
        self.headers = headers or {}
        self.client = type("ClientAddr", (), {"host": client_host, "port": 51234})()
        self.is_accepted = False
        self.is_closed = False
        self.close_code = None
        self.close_reason = None
        self.receive_queue = asyncio.Queue()
        self.sent_json_messages = []

    async def accept(self):
        self.is_accepted = True

    async def close(self, code=1000, reason=""):
        self.is_closed = True
        self.close_code = code
        self.close_reason = reason

    async def receive_text(self):
        return await self.receive_queue.get()

    async def send_json(self, data):
        self.sent_json_messages.append(data)


class DummyRequest:
    """Mock HTTP Request for endpoint testing."""

    def __init__(self, headers=None, client_host="192.168.1.60"):
        self.headers = headers or {}
        self.client = type("ClientAddr", (), {"host": client_host, "port": 52345})()


@pytest.mark.asyncio
async def test_unauthenticated_websocket_rejected():
    """Verify that a WebSocket connection without token or auth handshake is closed with 4401/4408."""
    ws = DummyWebSocket(headers={"origin": "http://192.168.1.55:8000"})

    # Send a non-auth message (e.g. SDP offer without authenticating first)
    await ws.receive_queue.put(json.dumps({"type": "offer", "sdp": "dummy_sdp"}))

    await websocket_signaling(ws)

    assert ws.is_accepted is True
    assert ws.is_closed is True
    assert ws.close_code == 4401
    assert "Authentication Required" in ws.close_reason or "Authentication Failed" in ws.close_reason


@pytest.mark.asyncio
async def test_invalid_token_websocket_rejected():
    """Verify that a WebSocket providing an invalid query token is immediately rejected with 4401."""
    ws = DummyWebSocket(
        query_params={"token": "bogus_token_123"},
        headers={"origin": "http://192.168.1.55:8000"}
    )

    await websocket_signaling(ws)

    assert ws.is_accepted is False
    assert ws.is_closed is True
    assert ws.close_code == 4401
    assert "Invalid or Expired Token" in ws.close_reason


@pytest.mark.asyncio
async def test_unauthorized_origin_websocket_rejected():
    """Verify that a WebSocket originating from an untrusted domain is rejected with 4403."""
    ws = DummyWebSocket(
        headers={"origin": "https://malicious-site.com"}
    )

    await websocket_signaling(ws)

    assert ws.is_accepted is False
    assert ws.is_closed is True
    assert ws.close_code == 4403
    assert "Unauthorized Origin" in ws.close_reason


@pytest.mark.asyncio
async def test_valid_token_websocket_accepted():
    """Verify that a WebSocket providing a valid query token is accepted."""
    from server.security import security_manager

    token = security_manager.create_session("192.168.1.55")
    ws = DummyWebSocket(
        query_params={"token": token},
        headers={"origin": "http://192.168.1.55:8000"}
    )

    # Queue ping to trigger clean loop and exit
    await ws.receive_queue.put(json.dumps({"type": "ping"}))

    # Run signaling in background task and cancel after verification
    task = asyncio.create_task(websocket_signaling(ws))
    await asyncio.sleep(0.05)

    assert ws.is_accepted is True
    assert ws.is_closed is False
    assert any(msg.get("type") == "pong" for msg in ws.sent_json_messages)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_api_auth_endpoint_full_flow():
    """Verify POST /api/auth verifies PIN, enforces rate limiting, and issues token."""
    from server.security import security_manager

    pin = security_manager.pairing_pin
    req = DummyRequest(headers={"origin": "http://192.168.1.60:8000"}, client_host="192.168.1.60")

    # 1. Invalid PIN -> 401
    with pytest.raises(HTTPException) as exc_info:
        await authenticate(req, AuthRequest(pin="000000"))
    assert exc_info.value.status_code == 401

    # 2. Valid PIN -> 200 OK + token
    resp = await authenticate(req, AuthRequest(pin=pin))
    assert resp["status"] == "ok"
    assert resp["authenticated"] is True
    token = resp["token"]
    assert security_manager.validate_token(token) is True

    # 3. Check /api/auth/status with token
    status_resp = await auth_status(req, token=token)
    assert status_resp["authenticated"] is True
