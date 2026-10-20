import soundcard as sc
import numpy as np

SAMPLE_RATE = 48000
CHANNELS = 2
BLOCK_SIZE = 960


def main():
    speakers = sc.all_speakers()

    print("Available speakers:")
    for speaker in speakers:
        print(f"  - {speaker.name}")

    speaker = sc.default_speaker()

    # Windows system audio -> WASAPI loopback microphone
    loopback = sc.get_microphone(
        speaker.name,
        include_loopback=True,
    )

    print()
    print(f"Default speaker: {speaker.name}")
    print(f"Loopback device: {loopback.name}")
    print("Starting WASAPI loopback capture...")
    print("Play some audio on Windows.")
    print("Press Ctrl+C to stop.")
    print()

    with loopback.recorder(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        blocksize=BLOCK_SIZE,
    ) as recorder:

        while True:
            data = recorder.record(numframes=BLOCK_SIZE)

            data = np.asarray(data, dtype=np.float32)

            peak = np.max(np.abs(data))

            print(
                f"\rSamples: {data.shape} | "
                f"Peak: {peak:.4f}",
                end="",
                flush=True,
            )


if __name__ == "__main__":
    main()