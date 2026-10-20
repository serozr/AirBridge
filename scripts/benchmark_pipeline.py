"""
AirBridge - Comprehensive Performance & Latency Benchmark

Benchmarks:
1. Opus Encoding Performance (libopus @ 48 kHz stereo 96 kbps, 20 ms frames)
   - Real-time factor (RTF), mean, median, p95, p99 encode latency
   - Packet size and bitrate across speech/tone, complex audio, and silence
2. AudioPipeline Throughput & Multi-Subscriber Fanout
   - Ring buffer insertion & retrieval latency
   - Scaling across 1, 2, 5, and 10 simultaneous listeners
   - Monotonic PTS progression precision
3. End-to-End In-Process Latency Budget
   - Breakdown of capture dispatch, pipeline queuing, and encoder execution
"""

import asyncio
import os
import sys
import time
import statistics
import numpy as np

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server.opus_encoder import OpusEncoder, SAMPLE_RATE, CHANNELS, FRAME_SIZE
from server.audio_pipeline import AudioPipeline
from server.config import (
    BYTES_PER_FRAME_S16,
    FRAME_SAMPLES,
    MAX_BUFFER_FRAMES,
)

TARGET_BITRATE_BPS: int = 96000


def generate_float32_frame(audio_type: str, frame_index: int) -> np.ndarray:
    """Generates synthetic 20 ms (960 sample stereo float32) audio frames for OpusEncoder."""
    t = (np.arange(FRAME_SIZE) + frame_index * FRAME_SIZE) / SAMPLE_RATE

    if audio_type == "silence":
        samples = np.zeros((FRAME_SIZE, CHANNELS), dtype=np.float32)
    elif audio_type == "tone_440hz":
        # 440 Hz stereo sine wave @ -6 dBFS (amplitude 0.5)
        sine = (np.sin(2 * np.pi * 440.0 * t) * 0.5).astype(np.float32)
        samples = np.column_stack((sine, sine))
    elif audio_type == "complex_noise":
        # Complex wideband audio / rich music simulation
        samples = np.random.uniform(-0.8, 0.8, size=(FRAME_SIZE, CHANNELS)).astype(np.float32)
    else:
        raise ValueError(f"Unknown audio_type: {audio_type}")

    return samples


def generate_s16_frame(audio_type: str, frame_index: int) -> bytes:
    """Generates synthetic 20 ms (3840 bytes s16le stereo) for AudioPipeline."""
    float_samples = generate_float32_frame(audio_type, frame_index)
    s16_samples = (float_samples * 32767).astype(np.int16)
    return s16_samples.tobytes()


def benchmark_opus_encoder(num_frames: int = 1000):
    """Measures libopus encoding latency, real-time factor, and bitrate."""
    print("=" * 72)
    print(f" 1. OPUS ENCODER BENCHMARK ({num_frames} frames = {num_frames * 0.020:.1f}s of audio)")
    print(f"    Configuration: 48 kHz Stereo | 20 ms (960 samples/ch) | 96 kbps Target")
    print("=" * 72)

    encoder = OpusEncoder(bitrate=TARGET_BITRATE_BPS)
    test_cases = ["tone_440hz", "complex_noise", "silence"]
    results = {}

    for audio_type in test_cases:
        latencies_us = []
        packet_sizes = []

        # Warm-up (50 frames)
        for i in range(50):
            pcm = generate_float32_frame(audio_type, i)
            encoder.encode_frame(pcm)

        # Timed benchmark
        for i in range(num_frames):
            pcm = generate_float32_frame(audio_type, i)
            t0 = time.perf_counter_ns()
            packet = encoder.encode_frame(pcm)
            t1 = time.perf_counter_ns()

            latencies_us.append((t1 - t0) / 1000.0)
            packet_sizes.append(len(packet))

        avg_packet_bytes = statistics.mean(packet_sizes)
        avg_bitrate_kbps = (avg_packet_bytes * 8 * 50) / 1000.0  # 50 frames/s

        mean_us = statistics.mean(latencies_us)
        median_us = statistics.median(latencies_us)
        p95_us = np.percentile(latencies_us, 95)
        p99_us = np.percentile(latencies_us, 99)
        max_us = max(latencies_us)

        # Real-Time Factor (RTF = processing_time / audio_duration)
        # 20 ms frame = 20,000 microseconds
        rtf = mean_us / 20000.0

        results[audio_type] = {
            "mean_ms": mean_us / 1000.0,
            "median_ms": median_us / 1000.0,
            "p95_ms": p95_us / 1000.0,
            "p99_ms": p99_us / 1000.0,
            "max_ms": max_us / 1000.0,
            "rtf": rtf,
            "bitrate_kbps": avg_bitrate_kbps,
            "avg_bytes": avg_packet_bytes,
        }

        print(f"\n[*] Audio Type: {audio_type.upper()}")
        print(f"    - Avg Encode Time:    {mean_us:6.1f} us ({mean_us/1000.0:.3f} ms)")
        print(f"    - Median Encode Time: {median_us:6.1f} us ({median_us/1000.0:.3f} ms)")
        print(f"    - 95th Percentile:    {p95_us:6.1f} us ({p95_us/1000.0:.3f} ms)")
        print(f"    - 99th Percentile:    {p99_us:6.1f} us ({p99_us/1000.0:.3f} ms)")
        print(f"    - Real-Time Factor:   {rtf * 100.0:.2f}% of 20 ms budget ({1.0/rtf:.0f}x faster than real-time)")
        print(f"    - Effective Bitrate:  {avg_bitrate_kbps:.1f} kbps ({avg_packet_bytes:.1f} bytes/packet)")

    return results


async def benchmark_audio_pipeline(num_frames: int = 1500):
    """Measures AudioPipeline publish, subscriber fanout, and queuing overhead."""
    print("\n" + "=" * 72)
    print(f" 2. AUDIO PIPELINE & MULTI-SUBSCRIBER FANOUT BENCHMARK")
    print(f"    Configuration: Monotonic PTS | Ring Buffer ({MAX_BUFFER_FRAMES} frames max) | Drop-Oldest")
    print("=" * 72)

    subscriber_counts = [1, 2, 5, 10]
    fanout_results = {}

    for count in subscriber_counts:
        pipeline = AudioPipeline(max_buffer_frames=MAX_BUFFER_FRAMES)
        subscribers = [pipeline.subscribe(f"peer-{i}") for i in range(count)]

        publish_latencies_us = []
        retrieve_latencies_us = []
        pts_deltas = []

        # Warm up
        for i in range(20):
            pcm = generate_s16_frame("tone_440hz", i)
            pipeline.publish_pcm(pcm)
            for sub in subscribers:
                await sub.get_frame(timeout=0.05)

        last_pts = {sub.subscriber_id: None for sub in subscribers}

        # Timed fanout benchmark
        for i in range(num_frames):
            pcm = generate_s16_frame("tone_440hz", i)

            # Measure publish latency across all subscribers
            t0 = time.perf_counter_ns()
            pipeline.publish_pcm(pcm)
            t1 = time.perf_counter_ns()
            publish_latencies_us.append((t1 - t0) / 1000.0)

            # Measure frame retrieval latency
            for sub in subscribers:
                t2 = time.perf_counter_ns()
                frame = await sub.get_frame(timeout=0.05)
                t3 = time.perf_counter_ns()
                retrieve_latencies_us.append((t3 - t2) / 1000.0)

                assert frame is not None
                if last_pts[sub.subscriber_id] is not None:
                    pts_deltas.append(frame.pts - last_pts[sub.subscriber_id])
                last_pts[sub.subscriber_id] = frame.pts

        # Clean up
        for sub in subscribers:
            pipeline.unsubscribe(sub.subscriber_id)

        mean_pub = statistics.mean(publish_latencies_us)
        p95_pub = np.percentile(publish_latencies_us, 95)
        mean_ret = statistics.mean(retrieve_latencies_us)
        p95_ret = np.percentile(retrieve_latencies_us, 95)

        # PTS validation
        pts_correct = all(delta == FRAME_SAMPLES for delta in pts_deltas)

        fanout_results[count] = {
            "subscribers": count,
            "mean_publish_us": mean_pub,
            "p95_publish_us": p95_pub,
            "mean_retrieve_us": mean_ret,
            "p95_retrieve_us": p95_ret,
            "pts_monotonic": pts_correct,
        }

        print(f"\n[*] Simultaneous Listeners: {count}")
        print(f"    - Fanout Publish Latency:  {mean_pub:6.2f} us (p95: {p95_pub:6.2f} us)")
        print(f"    - Queue Retrieval Latency: {mean_ret:6.2f} us (p95: {p95_ret:6.2f} us)")
        print(f"    - Monotonic PTS Advance:   {'100% PERFECT (strictly +960 samples / 20.00 ms)' if pts_correct else 'JITTER DETECTED'}")

    return fanout_results


def benchmark_latency_budget(opus_results, pipeline_results):
    """Calculates the end-to-end server processing latency budget."""
    print("\n" + "=" * 72)
    print(" 3. AIRBRIDGE SERVER-SIDE PROCESSING LATENCY BUDGET")
    print("=" * 72)

    pub_time_ms = pipeline_results[1]["mean_publish_us"] / 1000.0
    ret_time_ms = pipeline_results[1]["mean_retrieve_us"] / 1000.0
    enc_time_ms = opus_results["tone_440hz"]["mean_ms"]
    est_rtp_ms = 0.080  # ~80 us estimated for PyAV AudioFrame RTP encapsulation

    total_server_processing_ms = pub_time_ms + ret_time_ms + enc_time_ms + est_rtp_ms
    available_budget_ms = 20.000
    headroom_pct = ((available_budget_ms - total_server_processing_ms) / available_budget_ms) * 100.0

    print(f"| Component                                | Mean Duration    | % of 20ms Slot |")
    print(f"|------------------------------------------|------------------|----------------|")
    print(f"| WASAPI Loopback Frame Window             | 20.000 ms        | 100.00%        |")
    print(f"| AudioPipeline Publish & Peer Fanout (1)  |  {pub_time_ms:6.3f} ms       |  {pub_time_ms/20.0*100.0:5.2f}%        |")
    print(f"| Ring Buffer Retrieval                    |  {ret_time_ms:6.3f} ms       |  {ret_time_ms/20.0*100.0:5.2f}%        |")
    print(f"| libopus Compression (48 kHz Stereo @ 96k)|  {enc_time_ms:6.3f} ms       |  {enc_time_ms/20.0*100.0:5.2f}%        |")
    print(f"| WebRTC RTP Packetization & Media Queue   |  {est_rtp_ms:6.3f} ms       |  {est_rtp_ms/20.0*100.0:5.2f}%        |")
    print(f"|------------------------------------------|------------------|----------------|")
    print(f"| TOTAL SERVER OVERHEAD ADDED              |  {total_server_processing_ms:6.3f} ms       |  {total_server_processing_ms/20.0*100.0:5.2f}%        |")
    print(f"| CPU Headroom / Spare Processing Time     | {available_budget_ms - total_server_processing_ms:6.3f} ms       |  {headroom_pct:5.2f}%        |")
    print("=" * 72)


async def async_main():
    print("Starting AirBridge Performance Benchmark...\n")
    opus_results = benchmark_opus_encoder(num_frames=1000)
    pipeline_results = await benchmark_audio_pipeline(num_frames=1500)
    benchmark_latency_budget(opus_results, pipeline_results)
    print("\nBenchmark completed successfully.")


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
