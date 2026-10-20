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
