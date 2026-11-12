"""Push a test tone into the RemonteVoice driver and prove the capture endpoint emits it.

Run elevated (the driver accepts PCM only from SYSTEM/administrator):
    .venv\\Scripts\\python.exe host\\driver\\lab\\probe-driver-pcm.py

Requires the driver loaded (after reboot) and the device instance below.
"""
from __future__ import annotations

import sys
import threading
import time

import numpy as np
import soundcard as sc

sys.path.insert(0, r"M:\Projectorium\RemonteVoice\host")

from remontevoice.driver_mic import DriverMicWriter, MIC_SAMPLES  # noqa: E402
from remontevoice.windows_driver_transport import WindowsDriverMicTransport  # noqa: E402

INSTANCE = r"ROOT\MEDIA\0001"
SECONDS = 2.0
FREQUENCY = 440.0


def main() -> int:
    writer = DriverMicWriter(lambda: WindowsDriverMicTransport(INSTANCE))
    writer.start()
    deadline = time.monotonic() + 5
    while not writer.is_available() and time.monotonic() < deadline:
        time.sleep(0.05)
    print("writer available:", writer.is_available(), writer.snapshot)
    if not writer.is_available():
        writer.request_stop()
        return 2

    microphones = [m for m in sc.all_microphones(include_loopback=False) if "remonte" in m.name.casefold()]
    if len(microphones) != 1:
        print("capture endpoints found:", [m.name for m in microphones])
        writer.request_stop()
        return 3
    microphone = microphones[0]
    print("capture endpoint:", microphone.name)

    captured: dict[str, np.ndarray] = {}

    def record() -> None:
        captured["data"] = np.asarray(
            microphone.record(samplerate=48000, numframes=int(48000 * SECONDS)), dtype=np.float32
        )

    reader = threading.Thread(target=record, name="probe-record")
    reader.start()
    time.sleep(0.3)

    packets = int(SECONDS * 100)
    started = time.monotonic()
    for index in range(packets):
        samples = np.arange(MIC_SAMPLES) + index * MIC_SAMPLES
        tone = (0.3 * np.sin(2 * np.pi * FREQUENCY * samples / 48000)).astype(np.float32)
        if not writer.submit(tone.tobytes()):
            print("packet rejected at", index)
            break
        delay = started + (index + 1) * 0.01 - time.monotonic()
        if delay > 0:
            time.sleep(delay)
    reader.join()
    writer.request_stop()

    data = captured.get("data")
    if data is None or not data.size:
        print("no capture data")
        return 4
    peak = float(np.abs(data).max())
    rms = float(np.sqrt(np.mean(data ** 2)))
    print(f"captured: peak={peak:.4f} rms={rms:.5f} samples={data.size}")
    print("RESULT:", "ЗВУК ДОШЁЛ" if peak > 0.01 else "тишина")
    return 0 if peak > 0.01 else 5


if __name__ == "__main__":
    raise SystemExit(main())
