"""
AirBridge - Server-side Opus Audio Encoder

Pipeline Flow:
    1. WASAPI Loopback (Windows Audio Session API)
       Captures master output from the default speaker device.
    2. NumPy float32 PCM
       Normalized audio samples [-1.0, 1.0] at 48000 Hz, stereo (2 channels).
       Each 20 ms frame consists of 960 samples per channel (960 * 2 = 1920 float32 values = 7680 bytes).
    3. Opus Encoder (libopus)
       Compresses raw PCM frames into low-latency, bandwidth-efficient Opus packets
       ready for WebRTC transmission.
"""

import os
import sys
from typing import Optional, Union
import numpy as np

# Ensure Windows finds the bundled libopus DLL before importing opuslib.
# Check pyogg package directory, local server folder, and PATH.
def _setup_opus_dll_search_path() -> None:
    dll_dirs = []

    # 1. Check pyogg site-packages directory
    try:
        import pyogg
        pyogg_dir = os.path.dirname(pyogg.__file__)
        if os.path.isfile(os.path.join(pyogg_dir, "opus.dll")):
            dll_dirs.append(pyogg_dir)
    except ImportError:
        pass

    # 2. Check current directory and server directory
    base_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.isfile(os.path.join(base_dir, "opus.dll")):
        dll_dirs.append(base_dir)

    for d in dll_dirs:
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(d)
            except OSError:
                pass
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")

_setup_opus_dll_search_path()

try:
    import opuslib
except Exception as e:
    raise RuntimeError(
        f"Failed to load Opus library: {e}. "
        "Please make sure 'opuslib' and 'pyogg' are installed in requirements.txt."
    ) from e


SAMPLE_RATE = 48000       # 48 kHz standard for Opus
CHANNELS = 2              # Stereo
FRAME_DURATION_MS = 20    # 20 ms frame duration
FRAME_SIZE = int(SAMPLE_RATE * (FRAME_DURATION_MS / 1000.0))  # 960 samples per channel
BYTES_PER_SAMPLE_FLOAT32 = 4
EXPECTED_PCM_BYTES = FRAME_SIZE * CHANNELS * BYTES_PER_SAMPLE_FLOAT32  # 960 * 2 * 4 = 7680 bytes


class OpusEncoder:
    """
    Wraps libopus encoder for 48kHz stereo real-time audio streaming.
    Accepts 20ms float32 PCM frames (960 samples/channel) and outputs raw Opus packets.
    """

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        application: str = "audio",
        bitrate: int = 96000,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_size = int(sample_rate * (FRAME_DURATION_MS / 1000.0))

        # Initialize the underlying libopus encoder
        app_constant = (
            opuslib.APPLICATION_AUDIO if application == "audio"
            else opuslib.APPLICATION_VOIP if application == "voip"
            else opuslib.APPLICATION_RESTRICTED_LOWDELAY
        )

        self._encoder = opuslib.Encoder(self.sample_rate, self.channels, app_constant)
        self._encoder.bitrate = bitrate

    @property
    def bitrate(self) -> int:
        return self._encoder.bitrate

    @bitrate.setter
    def bitrate(self, value: int) -> None:
        self._encoder.bitrate = value

    def reset(self) -> None:
        """Resets the internal codec state."""
        self._encoder.reset_state()

    def encode_frame(self, pcm_data: Union[np.ndarray, bytes]) -> bytes:
        """
        Encodes a single 20ms frame to an Opus packet.

        Parameters:
            pcm_data: NumPy float32 array of shape (960, 2) or bytes of float32 PCM.

        Returns:
            bytes: Encoded Opus packet.
        """
        if isinstance(pcm_data, np.ndarray):
            if pcm_data.dtype != np.float32:
                pcm_data = pcm_data.astype(np.float32)
            # Ensure shape is (frame_size, channels)
            if pcm_data.shape != (self.frame_size, self.channels):
                raise ValueError(
                    f"Expected shape ({self.frame_size}, {self.channels}), got {pcm_data.shape}"
                )
            pcm_bytes = pcm_data.tobytes()
        elif isinstance(pcm_data, bytes):
            pcm_bytes = pcm_data
            if len(pcm_bytes) != self.frame_size * self.channels * BYTES_PER_SAMPLE_FLOAT32:
                raise ValueError(
                    f"Expected {self.frame_size * self.channels * BYTES_PER_SAMPLE_FLOAT32} bytes, "
                    f"got {len(pcm_bytes)} bytes"
                )
        else:
            raise TypeError(f"Unsupported pcm_data type: {type(pcm_data)}")

        # libopus opus_encode_float accepts interleaved 32-bit float PCM
        return self._encoder.encode_float(pcm_bytes, self.frame_size)


def run_live_test() -> None:
    """
    Captures live Windows system audio via WASAPI Loopback,
    encodes each 20ms frame with OpusEncoder, and displays real-time statistics.
    """
    import soundcard as sc

    print("=" * 60)
    print("AirBridge Phase 2 - WASAPI Loopback to Opus Encoding Test")
    print("=" * 60)

    speaker = sc.default_speaker()
    loopback = sc.get_microphone(speaker.name, include_loopback=True)

    print(f"Default speaker: {speaker.name}")
    print(f"Loopback device: {loopback.name}")
    print(f"Sampling Rate:   {SAMPLE_RATE} Hz | Channels: {CHANNELS} (Stereo)")
    print(f"Frame Duration:  {FRAME_DURATION_MS} ms | Frame Size: {FRAME_SIZE} samples")
    print()

    encoder = OpusEncoder(
        sample_rate=SAMPLE_RATE,
        channels=CHANNELS,
        application="audio",
        bitrate=96000,
    )

    print("Starting WASAPI capture & Opus encoding...")
    print("Play some audio on Windows (YouTube, Spotify, etc.)")
    print("Press Ctrl+C to stop.")
    print("-" * 60)

    frame_count = 0
    total_opus_bytes = 0

    try:
        with loopback.recorder(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            blocksize=FRAME_SIZE,
        ) as recorder:
            while True:
                # Capture 20 ms (960 frames) from WASAPI loopback
                data = recorder.record(numframes=FRAME_SIZE)
                data = np.asarray(data, dtype=np.float32)

                pcm_bytes = data.tobytes()
                pcm_size = len(pcm_bytes)

                # Encode to Opus packet
                opus_packet = encoder.encode_frame(pcm_bytes)
                opus_size = len(opus_packet)

                frame_count += 1
                total_opus_bytes += opus_size

                # Bitrate estimate for this 20ms packet:
                # (bytes * 8 bits / 0.02 seconds) / 1000 = kbps
                bitrate_kbps = (opus_size * 8 / (FRAME_DURATION_MS / 1000.0)) / 1000.0

                print(
                    f"Frame: {frame_count:4d} | "
                    f"PCM: {pcm_size} bytes | "
                    f"Opus: {opus_size:3d} bytes | "
                    f"bitrate: {bitrate_kbps:5.1f} kbps",
                    flush=True,
                )

    except KeyboardInterrupt:
        print()
        print("-" * 60)
        print("Capture stopped by user (Ctrl+C).")
        if frame_count > 0:
            avg_packet_size = total_opus_bytes / frame_count
            avg_bitrate = (avg_packet_size * 8 / (FRAME_DURATION_MS / 1000.0)) / 1000.0
            print(f"Total Frames Encoded: {frame_count}")
            print(f"Average Packet Size:  {avg_packet_size:.1f} bytes")
            print(f"Average Bitrate:      {avg_bitrate:.1f} kbps")
        print("=" * 60)


if __name__ == "__main__":
    run_live_test()
