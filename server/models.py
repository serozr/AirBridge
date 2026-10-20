"""
AirBridge - Data Models
"""

import time
from dataclasses import dataclass, field
from typing import Dict, Any

from server.config import SAMPLE_RATE, CHANNELS, FRAME_SAMPLES


@dataclass(frozen=True)
class AudioFrameData:
    """
    Immutable representations of a single PCM audio frame.
    """
    data: bytes
    pts: int
    sample_rate: int = SAMPLE_RATE
    channels: int = CHANNELS
    samples: int = FRAME_SAMPLES
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self):
        expected_size = self.samples * self.channels * 2
        if len(self.data) != expected_size:
            raise ValueError(
                f"AudioFrameData payload mismatch: expected {expected_size} bytes, got {len(self.data)}"
            )


@dataclass
class PipelineMetrics:
    """
    Realtime telemetry metrics for the audio pipeline.
    """
    frames_produced: int = 0
    frames_consumed: int = 0
    frames_dropped: int = 0
    buffer_depth: int = 0
    buffer_peak: int = 0
    underruns: int = 0
    overruns: int = 0
    current_pts: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sample_rate": SAMPLE_RATE,
            "channels": CHANNELS,
            "frame_samples": FRAME_SAMPLES,
            "frames_produced": self.frames_produced,
            "frames_consumed": self.frames_consumed,
            "frames_dropped": self.frames_dropped,
            "buffer_depth": self.buffer_depth,
            "buffer_peak": self.buffer_peak,
            "underruns": self.underruns,
            "overruns": self.overruns,
            "current_pts": self.current_pts,
        }
