"""Compare what each render endpoint carries: the driver one and the physical one."""
import numpy as np
import soundcard as sc

ENDPOINTS = {
    "RemonteVoice (драйвер)": "{0.0.0.00000000}.{df7a8a8e-f68b-4f7f-9b39-717fb31cb8a1}",
    "Realtek (колонки)": "{0.0.0.00000000}.{dc7b79bd-d147-4657-a072-8d13e4c094d9}",
}
RATE = 48000
SECONDS = 3


def peak(endpoint: str) -> float:
    microphone = sc.get_microphone(endpoint, include_loopback=True)
    data = np.asarray(microphone.record(samplerate=RATE, numframes=RATE * SECONDS), dtype=np.float32)
    return float(np.abs(data).max()) if data.size else 0.0


for label, identifier in ENDPOINTS.items():
    try:
        value = peak(identifier)
        print(f"{label:26} peak={value:.4f}  {'звук есть' if value > 0.005 else 'тишина'}")
    except Exception as exc:  # noqa: BLE001 - diagnostic only
        print(f"{label:26} ошибка: {exc}")
