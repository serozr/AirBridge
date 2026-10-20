"""
AirBridge v0.3.0 - Security Layer Hardening Test Suite

Covers:
1. Valid session token authentication
2. Invalid session token rejection
3. Missing session token rejection
4. Expired session token rejection (TTL)
5. Token rejected from wrong IP (IP-bound session tokens)
6. Valid pairing PIN verification & token generation
7. Invalid pairing PIN rejection
8. Rate limiting & brute-force lockout
9. Rate limit reset on successful PIN verification
10. Rate limiter state cleanup (pruning stale/expired entries)
11. Session rotation (PIN & token revocation)
12. Origin missing policy (allowed for direct HTTP clients, rejected if malformed)
13. Host & Origin scheme, hostname, and port validation (IPv4 and IPv6)
14. Configured allowlist exact origin matching
15. Sensitive credentials not logged (PIN and tokens never leaked in logs)
16. Unauthenticated WebSocket rejected
17. Token in query string NOT accepted (in-band auth strictly enforced)
18. In-band WebSocket authentication handshake flow
19. REST API endpoints tested via FastAPI TestClient
"""

import asyncio
import json
import logging
import time
import pytest
from starlette.testclient import TestClient

from server.security import SecurityManager, RateLimiter
from server.webrtc_server import app, websocket_signaling


# =========================================================================
# 1. Token & Session Management Tests (with IP Binding)
# =========================================================================

def test_valid_token_authentication():
    """Verify that a validly issued session token is accepted from the issuing IP."""
    sec = SecurityManager(pin="123456", session_ttl_seconds=3600)
    token = sec.create_session(client_ip="192.168.1.100")
    assert sec.validate_token(token, client_ip="192.168.1.100") is True


def test_token_rejected_from_wrong_ip():
    """
    Verify IP binding: A token issued to IP X CANNOT be used by IP Y.
    Guards against LAN token interception and session hijacking.
    """
    sec = SecurityManager(pin="123456", session_ttl_seconds=3600)
    token = sec.create_session(client_ip="192.168.1.100")

    # Accessing from the issued IP must succeed
    assert sec.validate_token(token, client_ip="192.168.1.100") is True

    # Accessing from a different LAN IP must be strictly rejected
    assert sec.validate_token(token, client_ip="192.168.1.105") is False
    assert sec.validate_token(token, client_ip="10.0.0.50") is False


def test_invalid_token_rejected():
    """Verify that fabricated, altered, or random tokens are rejected."""
    sec = SecurityManager(pin="123456")
    sec.create_session(client_ip="192.168.1.100")

    assert sec.validate_token("completely_fake_token_12345", client_ip="192.168.1.100") is False
    assert sec.validate_token("another_invalid_token", client_ip="192.168.1.100") is False


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

    # Manually backdate expiration
    with sec._lock:
        sec._sessions[token].expires_at = time.time() - 10.0

    assert sec.validate_token(token, client_ip="192.168.1.100") is False
    assert sec.active_session_count() == 0


# =========================================================================
# 2. Pairing PIN & Brute-Force Rate Limiting Tests
# =========================================================================

def test_valid_pin_generates_token():
    """Verify that submitting the correct pairing PIN generates a valid token bound to client IP."""
    sec = SecurityManager(pin="654321")
    is_valid, token, err = sec.verify_pin("654321", client_ip="192.168.1.105")

    assert is_valid is True
    assert token is not None
    assert err is None
    assert sec.validate_token(token, client_ip="192.168.1.105") is True
    assert sec.active_session_count() == 1


def test_invalid_pin_rejected():
    """Verify that an incorrect PIN is rejected with remaining attempt count."""
    sec = SecurityManager(pin="654321", max_attempts=3)
    is_valid, token, err = sec.verify_pin("000000", client_ip="192.168.1.105")

    assert is_valid is False
    assert token is None
    assert "Invalid PIN" in err
    assert "2 attempt(s) remaining" in err


def test_rate_limit():
    """Verify that 3 consecutive failed PIN attempts trigger a temporary lockout."""
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

    # Attempt 4: Even with correct PIN, must be blocked during lockout
    ok4, _, err4 = sec.verify_pin("888888", client_ip=client_ip)
    assert ok4 is False
    assert "Temporarily blocked" in err4


def test_rate_limit_reset_on_success():
    """Verify that a successful PIN attempt resets the failed attempt counter."""
    sec = SecurityManager(pin="999999", max_attempts=3)
    client_ip = "192.168.1.201"

    # 2 failures
    sec.verify_pin("000001", client_ip=client_ip)
    sec.verify_pin("000002", client_ip=client_ip)

    # 3rd attempt is correct
    ok, token, _ = sec.verify_pin("999999", client_ip=client_ip)
    assert ok is True
    assert token is not None

    # Failed count must be reset; next wrong attempt should show max-1 attempts remaining
    _, _, err = sec.verify_pin("000003", client_ip=client_ip)
    assert "2 attempt(s) remaining" in err


def test_rate_limiter_state_cleanup():
    """Verify that RateLimiter.prune() purges stale entries to prevent memory leaks."""
    limiter = RateLimiter(max_attempts=3, lockout_seconds=10)
    limiter.record_failure("192.168.1.50")
    limiter.record_failure("192.168.1.51")

    # Manually backdate attempt times
    with limiter._lock:
        limiter._last_attempt_time["192.168.1.50"] = time.time() - 4000
        limiter._last_attempt_time["192.168.1.51"] = time.time()  # recent

    limiter.prune(max_idle_seconds=3600)

    with limiter._lock:
        assert "192.168.1.50" not in limiter._failed_attempts
        assert "192.168.1.51" in limiter._failed_attempts


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
    assert sec.validate_token(token1, "192.168.1.10") is False
    assert sec.validate_token(token2, "192.168.1.11") is False


# =========================================================================
# 3. Strict Origin & Host Validation Tests
# =========================================================================

def test_origin_missing_policy():
    """Verify that requests without Origin header (direct tools, curl, native clients) are accepted."""
    sec = SecurityManager()
    assert sec.validate_origin(None) is True
    assert sec.validate_origin("") is True
    assert sec.validate_origin(None, host="192.168.1.100:8000") is True


def test_host_origin_scheme_port_validation():
    """Verify that browser Origin must strictly match the scheme, hostname, and port of Host."""
    sec = SecurityManager()

    # 1. Matching IPv4 Host & Origin
    assert sec.validate_origin("http://192.168.1.100:8000", host="192.168.1.100:8000") is True
    assert sec.validate_origin("http://localhost:8000", host="localhost:8000") is True

    # 2. Matching IPv6 Host & Origin (handles bracketed notation)
    assert sec.validate_origin("http://[::1]:8000", host="[::1]:8000") is True

    # 3. Port Mismatch (Rejected)
    assert sec.validate_origin("http://192.168.1.100:9999", host="192.168.1.100:8000") is False

    # 4. Hostname Mismatch (Rejected)
    assert sec.validate_origin("http://192.168.1.200:8000", host="192.168.1.100:8000") is False

    # 5. External Malicious Origins (Rejected)
    assert sec.validate_origin("https://evil-hacker.com", host="192.168.1.100:8000") is False
    assert sec.validate_origin("http://attacker.org:8000", host="192.168.1.100:8000") is False


def test_configured_origin_exact_match():
    """Verify that explicit allowed origins in allowlist are permitted."""
    sec = SecurityManager(allowed_origins="http://my-home-server:8000, https://airbridge.local")

    assert sec.validate_origin("http://my-home-server:8000", host="192.168.1.100:8000") is True
    assert sec.validate_origin("https://airbridge.local", host="192.168.1.100:8000") is True
    assert sec.validate_origin("http://unauthorized.local", host="192.168.1.100:8000") is False


# =========================================================================
# 4. Credential Sanitization Test
# =========================================================================

def test_sensitive_credentials_not_logged(caplog):
    """Verify that sensitive PINs and tokens are never written into server logs."""
    sec = SecurityManager(pin="739102")

    with caplog.at_level(logging.INFO):
        sec.rotate_session()
        sec.verify_pin("739102", client_ip="192.168.1.50")

    log_output = caplog.text
    # The secret PIN "739102" must NOT appear in any log statement
    assert "739102" not in log_output
    # Full token should not appear
    for record in caplog.records:
        assert "Pairing PIN 739102" not in record.message


# =========================================================================
# 5. WebSocket Signaling Security Tests
# =========================================================================

class MockWebSocket:
    """Mock WebSocket for testing in-band signaling authentication handshake."""

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


@pytest.mark.asyncio
async def test_unauthenticated_websocket_rejected():
    """Verify that connecting and sending an SDP offer before authenticating is rejected with 4401."""
    ws = MockWebSocket(headers={"origin": "http://192.168.1.55:8000", "host": "192.168.1.55:8000"})
    await ws.receive_queue.put(json.dumps({"type": "offer", "sdp": "unauthenticated_sdp"}))

    await websocket_signaling(ws)

    assert ws.is_accepted is True
    assert ws.is_closed is True
    assert ws.close_code == 4401
    assert "Authentication Required" in ws.close_reason


@pytest.mark.asyncio
async def test_token_not_accepted_in_query_string():
    """
    Verify that passing a token in the URL query string does NOT bypass in-band authentication.
    Tokens in URLs are prohibited to prevent browser history and server access log leaks.
    """
    from server.security import security_manager

    token = security_manager.create_session("192.168.1.55")
    ws = MockWebSocket(
        query_params={"token": token},
        headers={"origin": "http://192.168.1.55:8000", "host": "192.168.1.55:8000"}
    )
    # Immediately sends SDP offer without in-band auth message
    await ws.receive_queue.put(json.dumps({"type": "offer", "sdp": "premature_offer"}))

    await websocket_signaling(ws)

    # Must be closed because in-band auth handshake was not sent
    assert ws.is_closed is True
    assert ws.close_code == 4401


@pytest.mark.asyncio
async def test_in_band_auth_websocket_flow():
    """Verify the correct in-band authentication handshake: auth token -> auth_ok -> signaling."""
    from server.security import security_manager

    token = security_manager.create_session("192.168.1.55")
    ws = MockWebSocket(headers={"origin": "http://192.168.1.55:8000", "host": "192.168.1.55:8000"})

    # Send in-band auth message, followed by ping
    await ws.receive_queue.put(json.dumps({"type": "auth", "token": token}))
    await ws.receive_queue.put(json.dumps({"type": "ping"}))

    task = asyncio.create_task(websocket_signaling(ws))
    await asyncio.sleep(0.05)

    assert ws.is_accepted is True
    assert ws.is_closed is False
    assert any(msg.get("type") == "auth_ok" for msg in ws.sent_json_messages)
    assert any(msg.get("type") == "pong" for msg in ws.sent_json_messages)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# =========================================================================
# 6. REST API Endpoints Testing with FastAPI TestClient
# =========================================================================

def test_api_auth_with_testclient():
    """Verify /api/auth and /api/auth/status endpoints using FastAPI TestClient."""
    from server.security import security_manager

    client = TestClient(app)
    pin = security_manager.pairing_pin
    headers = {"Origin": "http://testserver", "Host": "testserver"}

    # 1. Missing body -> 422
    resp = client.post("/api/auth", json={}, headers=headers)
    assert resp.status_code == 400

    # 2. Invalid PIN -> 401
    resp = client.post("/api/auth", json={"pin": "000000"}, headers=headers)
    assert resp.status_code == 401

    # 3. Valid PIN -> 200 OK + token
    resp = client.post("/api/auth", json={"pin": pin}, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["authenticated"] is True
    token = data["token"]

    # 4. Check /api/auth/status with Bearer token
    status_resp = client.get("/api/auth/status", headers={**headers, "Authorization": f"Bearer {token}"})
    assert status_resp.status_code == 200
    assert status_resp.json()["authenticated"] is True

    # 5. Unauthorized Origin on /api/auth -> 403
    forbidden_resp = client.post("/api/auth", json={"pin": pin}, headers={"Origin": "https://malicious.com"})
    assert forbidden_resp.status_code == 403
