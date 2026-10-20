"""
AirBridge - Global Configuration
"""

import os

# Audio specifications
SAMPLE_RATE: int = int(os.getenv("AIRBRIDGE_SAMPLE_RATE", "48000"))
CHANNELS: int = int(os.getenv("AIRBRIDGE_CHANNELS", "2"))
FRAME_DURATION_MS: int = int(os.getenv("AIRBRIDGE_FRAME_DURATION_MS", "20"))
FRAME_SAMPLES: int = int(SAMPLE_RATE * (FRAME_DURATION_MS / 1000.0))  # 960 samples/channel
BYTES_PER_SAMPLE_S16: int = 2
BYTES_PER_FRAME_S16: int = FRAME_SAMPLES * CHANNELS * BYTES_PER_SAMPLE_S16  # 3840 bytes

# Bounded Buffer policy (default: 15 frames = ~300ms maximum buffer depth)
MAX_BUFFER_FRAMES: int = int(os.getenv("AIRBRIDGE_MAX_BUFFER_FRAMES", "15"))

# Server Network settings
DEFAULT_HOST: str = os.getenv("AIRBRIDGE_HOST", "0.0.0.0")
DEFAULT_PORT: int = int(os.getenv("AIRBRIDGE_PORT", "8000"))

# Security settings
ENABLE_SECURITY: bool = os.getenv("AIRBRIDGE_SECURITY_ENABLED", "true").lower() in ("true", "1", "yes")
SESSION_TTL_SECONDS: int = int(os.getenv("AIRBRIDGE_SESSION_TTL_SECONDS", "86400"))  # 24 hours
MAX_PIN_ATTEMPTS: int = int(os.getenv("AIRBRIDGE_MAX_PIN_ATTEMPTS", "3"))
LOCKOUT_DURATION_SECONDS: int = int(os.getenv("AIRBRIDGE_LOCKOUT_SECONDS", "300"))  # 5 minutes
CONFIGURED_PIN: str = os.getenv("AIRBRIDGE_PIN", "")
ALLOWED_ORIGINS: str = os.getenv("AIRBRIDGE_ALLOWED_ORIGINS", "")

