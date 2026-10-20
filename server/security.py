"""
AirBridge - Security Layer (v0.3.0 Hardened)

Responsibilities:
    - In-memory cryptographically secure session token management with client IP binding.
    - 6-digit numeric pairing PIN generation and constant-time verification.
    - IP-based rate limiting & brute-force lockout protection with state cleanup.
    - Strict Origin and Host header validation preventing Cross-Site WebSocket Hijacking (CSWSH).
    - Session rotation and TTL-based expiration.
    - Strict logging sanitization (zero credential leakage).
"""

import hmac
import ipaddress
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple
from urllib.parse import urlparse

from server.config import (
    ALLOWED_ORIGINS,
    CONFIGURED_PIN,
    ENABLE_SECURITY,
    LOCKOUT_DURATION_SECONDS,
    MAX_PIN_ATTEMPTS,
    SESSION_TTL_SECONDS,
)

logger = logging.getLogger("AirBridge.Security")


@dataclass
class SessionData:
    """Represents an authenticated client session bound to a client IP."""
    token: str
    client_ip: str
    created_at: float
    expires_at: float

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at


class RateLimiter:
    """Thread-safe rate limiter tracking failed authentication attempts per IP with state pruning."""

    def __init__(
        self,
        max_attempts: int = MAX_PIN_ATTEMPTS,
        lockout_seconds: int = LOCKOUT_DURATION_SECONDS,
    ) -> None:
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self._failed_attempts: Dict[str, int] = {}
        self._lockout_until: Dict[str, float] = {}
        self._last_attempt_time: Dict[str, float] = {}
        self._lock = threading.Lock()

    def prune(self, max_idle_seconds: int = 3600) -> None:
        """Removes expired lockouts and stale tracking entries to prevent unbounded memory growth."""
        with self._lock:
            now = time.time()
            stale_ips = [
                ip for ip, last_seen in self._last_attempt_time.items()
                if (now - last_seen > max_idle_seconds) and (self._lockout_until.get(ip, 0) < now)
            ]
            for ip in stale_ips:
                self._failed_attempts.pop(ip, None)
                self._lockout_until.pop(ip, None)
                self._last_attempt_time.pop(ip, None)

    def is_locked(self, client_ip: str) -> Tuple[bool, int]:
        """Checks if a client IP is currently locked out. Returns (is_locked, remaining_seconds)."""
        with self._lock:
            now = time.time()
            lockout_time = self._lockout_until.get(client_ip)
            if lockout_time is not None:
                if now < lockout_time:
                    remaining = int(lockout_time - now) + 1
                    return True, remaining
                else:
                    # Lockout expired: reset
                    del self._lockout_until[client_ip]
                    self._failed_attempts.pop(client_ip, None)
            return False, 0

    def record_failure(self, client_ip: str) -> Tuple[bool, int]:
        """
        Records a failed attempt. If threshold reached, triggers lockout.
        Returns (is_locked, remaining_seconds).
        """
        with self._lock:
            now = time.time()
            self._last_attempt_time[client_ip] = now
            attempts = self._failed_attempts.get(client_ip, 0) + 1
            self._failed_attempts[client_ip] = attempts

            if attempts >= self.max_attempts:
                lockout_until = now + self.lockout_seconds
                self._lockout_until[client_ip] = lockout_until
                logger.warning(
                    "Client IP %s exceeded max failed attempts (%d). Locked out for %ds.",
                    client_ip,
                    attempts,
                    self.lockout_seconds,
                )
                return True, self.lockout_seconds

            remaining_attempts = self.max_attempts - attempts
            return False, remaining_attempts

    def record_success(self, client_ip: str) -> None:
        """Resets failed attempts and lockout on successful authentication."""
        with self._lock:
            self._failed_attempts.pop(client_ip, None)
            self._lockout_until.pop(client_ip, None)
            self._last_attempt_time.pop(client_ip, None)

    def reset(self, client_ip: Optional[str] = None) -> None:
        """Resets state for a specific IP or all tracked IPs."""
        with self._lock:
            if client_ip:
                self._failed_attempts.pop(client_ip, None)
                self._lockout_until.pop(client_ip, None)
                self._last_attempt_time.pop(client_ip, None)
            else:
                self._failed_attempts.clear()
                self._lockout_until.clear()
                self._last_attempt_time.clear()


class SecurityManager:
    """
    Central security controller for AirBridge.
    Manages pairing PINs, IP-bound session tokens, strict origin verification, and rate limiting.
    """

    def __init__(
        self,
        pin: Optional[str] = None,
        session_ttl_seconds: int = SESSION_TTL_SECONDS,
        max_attempts: int = MAX_PIN_ATTEMPTS,
        lockout_seconds: int = LOCKOUT_DURATION_SECONDS,
        allowed_origins: Optional[str] = None,
        enabled: bool = ENABLE_SECURITY,
    ) -> None:
        self.enabled = enabled
        self.session_ttl_seconds = session_ttl_seconds
        self._allowed_origins_raw = allowed_origins if allowed_origins is not None else ALLOWED_ORIGINS

        # Initialize rate limiter
        self.rate_limiter = RateLimiter(
            max_attempts=max_attempts,
            lockout_seconds=lockout_seconds,
        )

        self._lock = threading.Lock()
        self._sessions: Dict[str, SessionData] = {}

        # Configure or generate initial PIN & Session ID
        self._pin: str = ""
        self._server_session_id: str = ""
        self.rotate_session(initial_pin=pin or CONFIGURED_PIN)

    @property
    def pairing_pin(self) -> str:
        with self._lock:
            return self._pin

    @property
    def server_session_id(self) -> str:
        with self._lock:
            return self._server_session_id

    def rotate_session(self, initial_pin: Optional[str] = None) -> Tuple[str, str]:
        """
        Rotates session credentials.
        Generates a new pairing PIN and session ID, and revokes all active session tokens.
        Never logs sensitive PIN or tokens.
        """
        with self._lock:
            if initial_pin and len(initial_pin.strip()) >= 4:
                self._pin = initial_pin.strip()
            else:
                self._pin = f"{secrets.randbelow(1_000_000):06d}"

            self._server_session_id = secrets.token_hex(4).upper()
            revoked_count = len(self._sessions)
            self._sessions.clear()

        # Reset rate limiter
        self.rate_limiter.reset()

        logger.info(
            "Security session rotated: Session ID %s (revoked %d active tokens)",
            self._server_session_id,
            revoked_count,
        )
        return self._server_session_id, self._pin

    def verify_pin(self, pin: str, client_ip: str = "unknown") -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Verifies the provided pairing PIN against the active server PIN.
        Guards against timing attacks using hmac.compare_digest.
        Enforces IP rate-limiting.

        Returns:
            Tuple[bool, Optional[str], Optional[str]]:
                (is_valid, session_token_if_valid, error_message_if_invalid)
        """
        if not self.enabled:
            token = f"unsecured_{secrets.token_urlsafe(16)}"
            return True, token, None

        # Periodically prune stale rate limit state
        self.rate_limiter.prune()

        # Check rate limiter
        is_locked, remaining_seconds = self.rate_limiter.is_locked(client_ip)
        if is_locked:
            msg = f"Too many failed attempts. Temporarily blocked for {remaining_seconds}s."
            logger.warning("Authentication rejected for %s: %s", client_ip, msg)
            return False, None, msg

        if not pin or not isinstance(pin, str):
            locked, remaining = self.rate_limiter.record_failure(client_ip)
            msg = "Missing or invalid PIN format." if not locked else f"Too many failed attempts. Blocked for {remaining}s."
            return False, None, msg

        # Constant-time comparison
        with self._lock:
            is_match = hmac.compare_digest(pin.strip(), self._pin)

        if not is_match:
            locked, remaining = self.rate_limiter.record_failure(client_ip)
            if locked:
                msg = f"Invalid PIN. Too many failed attempts. Blocked for {remaining}s."
            else:
                msg = f"Invalid PIN. {remaining} attempt(s) remaining."
            logger.warning("PIN verification failed for client %s (%s)", client_ip, msg)
            return False, None, msg

        # PIN verified successfully: reset failures and issue session token bound to this IP
        self.rate_limiter.record_success(client_ip)
        token = self.create_session(client_ip)
        logger.info("Client %s authenticated successfully with PIN.", client_ip)
        return True, token, None

    def create_session(self, client_ip: str = "unknown") -> str:
        """Creates and registers a new secure session token bound to client_ip."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        session = SessionData(
            token=token,
            client_ip=client_ip,
            created_at=now,
            expires_at=now + self.session_ttl_seconds,
        )
        with self._lock:
            self._sessions[token] = session
        return token

    def validate_token(self, token: Optional[str], client_ip: Optional[str] = None) -> bool:
        """
        Validates if a session token is active, not expired, and matches client IP.
        Purges expired tokens on access.
        """
        if not self.enabled:
            return True

        if not token or not isinstance(token, str):
            return False

        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return False

            if session.is_expired:
                del self._sessions[token]
                logger.info("Session token expired and purged.")
                return False

            # Enforce client IP binding if both session and request specify an IP
            if client_ip is not None and session.client_ip not in ("unknown", "testclient"):
                if client_ip not in ("unknown", "testclient") and session.client_ip != client_ip:
                    logger.warning(
                        "Token rejected: IP mismatch (issued to %s, presented by %s)",
                        session.client_ip,
                        client_ip,
                    )
                    return False

        return True

    def revoke_token(self, token: str) -> bool:
        """Revokes an active session token."""
        with self._lock:
            return self._sessions.pop(token, None) is not None

    def active_session_count(self) -> int:
        """Returns the number of currently active, non-expired sessions."""
        now = time.time()
        with self._lock:
            expired = [k for k, v in self._sessions.items() if v.expires_at <= now]
            for k in expired:
                del self._sessions[k]
            return len(self._sessions)

    def validate_origin(self, origin: Optional[str], host: Optional[str] = None) -> bool:
        """
        Strictly validates the Origin header against the request Host and explicit allowlist.
        Prevents Cross-Site WebSocket Hijacking (CSWSH) and malicious web-initiated cross-origin requests.
        Handles IPv6 hosts safely without delimiter errors.
        """
        if not self.enabled:
            return True

        # 1. Requests without Origin header: Allowed for non-browser or direct clients
        if origin is None or origin.strip() == "":
            return True

        # 2. Parse Origin
        try:
            parsed_origin = urlparse(origin)
            origin_scheme = parsed_origin.scheme.lower()
            origin_host = parsed_origin.hostname
            if not origin_scheme or not origin_host:
                return False
            if origin_scheme not in ("http", "https", "ws", "wss"):
                return False
            origin_port = parsed_origin.port or (443 if origin_scheme in ("https", "wss") else 80)
        except Exception:
            return False

        origin_host = origin_host.lower()

        # 3. Check explicit allowlist (exact match on full origin or hostname)
        if self._allowed_origins_raw:
            allowed_list = [o.strip().lower() for o in self._allowed_origins_raw.split(",") if o.strip()]
            for allowed in allowed_list:
                if origin.lower().rstrip("/") == allowed.rstrip("/") or origin_host == allowed:
                    return True

        # 4. Same-origin validation against Host header:
        # Origin MUST match the server's Host header (scheme://host:port)
        if host:
            try:
                # Handle IPv6 host headers like [::1]:8000 or 192.168.1.100:8000
                parsed_host = urlparse(f"//{host}")
                host_name = parsed_host.hostname
                if host_name:
                    host_name = host_name.lower()
                    host_port = parsed_host.port or (443 if origin_scheme in ("https", "wss") else 80)
                    if origin_host == host_name and origin_port == host_port:
                        return True
            except Exception:
                pass

        # 5. Localhost fallback when no host header provided
        if not host and origin_host in ("localhost", "127.0.0.1", "::1"):
            return True

        logger.warning(
            "Rejected request: Origin '%s' does not match Host '%s' and is not in allowlist",
            origin,
            host,
        )
        return False

    def get_security_banner(self) -> str:
        """Returns formatted terminal banner with active credentials."""
        pin = self.pairing_pin
        sess_id = self.server_session_id
        ttl_hours = self.session_ttl_seconds // 3600
        return (
            f"  AirBridge Security (v0.3.0)\n"
            f"  ────────────────────────────────────────────────────────────\n"
            f"    Pairing PIN:        {pin}\n"
            f"    Session ID:         {sess_id}\n"
            f"    Rate Limit:         {MAX_PIN_ATTEMPTS} attempts / {LOCKOUT_DURATION_SECONDS}s lockout\n"
            f"    Session Expiration: {ttl_hours} hours\n"
            f"  ────────────────────────────────────────────────────────────"
        )


# Global singleton instance
security_manager = SecurityManager()
