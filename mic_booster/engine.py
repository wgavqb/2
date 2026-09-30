"""Звуковой движок: микрофон → обработка → виртуальный кабель (+ прослушка в наушники).

Вход и выход — отдельные потоки WASAPI с кольцевым буфером между ними:
так разные часы устройств (AM8 и VB-Cable) не копят задержку и не щёлкают.
"""

from __future__ import annotations

import threading
import time

import numpy as np

from .dsp import Processor, Settings

FS = 48000
SPECTRUM_BANDS = 48


class Ring:
    """Потокобезопасный кольцевой буфер моно-сэмплов с контролем задержки."""

    def __init__(self, capacity: int):
        self.buf = np.zeros(capacity, dtype=np.float32)
        self.capacity = capacity
        self.start = 0
        self.size = 0
        self.lock = threading.Lock()
        self.underruns = 0

    def write(self, x: np.ndarray) -> None:
        with self.lock:
            n = len(x)
            if n >= self.capacity:
                x = x[-self.capacity:]
                n = self.capacity
            overflow = self.size + n - self.capacity
            if overflow > 0:  # выкидываем самое старое
                self.start = (self.start + overflow) % self.capacity
                self.size -= overflow
            end = (self.start + self.size) % self.capacity
            first = min(n, self.capacity - end)
            self.buf[end:end + first] = x[:first]
            self.buf[:n - first] = x[first:]
            self.size += n

    def read(self, n: int, max_fill: int) -> np.ndarray:
        """Читает n сэмплов. Если буфер «разросся» больше max_fill — догоняет
        (часы входа спешат), если данных не хватает — дополняет тишиной."""
        out = np.zeros(n, dtype=np.float32)
        with self.lock:
            if self.size > max_fill + n:
                drop = self.size - max_fill
                self.start = (self.start + drop) % self.capacity
                self.size -= drop
            take = min(n, self.size)
            if take < n:
                self.underruns += 1
            first = min(take, self.capacity - self.start)
            out[:first] = self.buf[self.start:self.start + first]
            out[first:take] = self.buf[:take - first]
            self.start = (self.start + take) % self.capacity
            self.size -= take
        return out

    @property
    def fill(self) -> int:
        return self.size


def _band_edges(fs: int, n_fft: int) -> list[tuple[int, int]]:
    freqs = np.geomspace(60, 16000, SPECTRUM_BANDS + 1)
    bins = np.clip((freqs / fs * n_fft).astype(int), 1, n_fft // 2)
    return [(int(a), int(max(b, a + 1))) for a, b in zip(bins[:-1], bins[1:])]


class AudioEngine:
    def __init__(self):
        self.settings = Settings()
        self.low_latency = False
        self.processor: Processor | None = None
        self.streams = []
        self.rings: list[Ring] = []
        self.monitor_volume = 0.8
        self.error = ""
        self.running = False
        self._hist_in = np.zeros(2048, dtype=np.float32)
        self._hist_out = np.zeros(2048, dtype=np.float32)
        self._win = np.hanning(2048)
        self._bands = _band_edges(FS, 2048)
        self._cpu = 0.0
        self._lock = threading.Lock()
        self.latency_ms = 0.0

    # --- устройства --------------------------------------------------------------

    @staticmethod
    def devices() -> dict:
        import sounddevice as sd

        apis = sd.query_hostapis()
        wasapi = next((i for i, a in enumerate(apis) if "WASAPI" in a["name"]), None)
        inputs, outputs = [], []
        for idx, dev in enumerate(sd.query_devices()):
            if wasapi is not None and dev["hostapi"] != wasapi:
                continue
            item = {"id": idx, "name": dev["name"]}
            if dev["max_input_channels"] > 0:
                inputs.append(item)
            if dev["max_output_channels"] > 0:
                outputs.append(item)
        default_in = default_out = None
        if wasapi is not None:
            default_in = apis[wasapi].get("default_input_device")
            default_out = apis[wasapi].get("default_output_device")
        return {"inputs": inputs, "outputs": outputs,
                "default_in": default_in, "default_out": default_out}

    # --- запуск / остановка -----------------------------------------------------------

    def start(self, in_id: int, out_id: int | None, monitor_id: int | None) -> None:
        import sounddevice as sd

        self.stop()
        block = 240 if self.low_latency else 480
        self.processor = Processor(FS, block, self.settings)
        extra = None
        if hasattr(sd, "WasapiSettings"):
            try:
                extra = sd.WasapiSettings(auto_convert=True)  # Windows сам подгонит частоту
            except TypeError:
                extra = None
        in_info = sd.query_devices(in_id)
        in_ch = min(2, int(in_info["max_input_channels"])) or 1

        # (устройство, это прослушка?) — основной вывод в кабель и, по желанию, в наушники
        outputs = [(d, mon) for d, mon in ((out_id, False), (monitor_id, True)) if d is not None]
        self.rings = [Ring(FS) for _ in outputs]
        target_fill = block * (2 if self.low_latency else 3)
        self.error = ""

        pending = np.zeros(0)

        def on_input(indata, frames, _time, status):
            nonlocal pending
            t0 = time.perf_counter()
            mono = indata[:, 0] if in_ch == 1 else indata.mean(axis=1)
            # обработка идёт ровными блоками; если драйвер прислал кусок другого размера — копим
            pending = np.concatenate([pending, mono.astype(np.float64)])
            usable = len(pending) - len(pending) % block
            y = self.processor.process(pending[:usable]).astype(np.float32)
            pending = pending[usable:]
            for ring in self.rings:
                ring.write(y)
            if not len(y):
                return
            n = min(frames, 2048)
            self._hist_in = np.roll(self._hist_in, -n)
            self._hist_in[-n:] = mono[-n:]
            self._hist_out = np.roll(self._hist_out, -n)
            self._hist_out[-n:] = y[-n:]
            # доля времени блока, потраченная на обработку
            self._cpu = 0.9 * self._cpu + 0.1 * (time.perf_counter() - t0) / (frames / FS)

        def make_output(ring: Ring, is_monitor: bool):
            def on_output(outdata, frames, _time, status):
                data = ring.read(frames, target_fill)
                if is_monitor:
                    data = data * self.monitor_volume
                outdata[:] = data[:, None]
            return on_output

        streams = []
        try:
            streams.append(sd.InputStream(device=in_id, channels=in_ch, samplerate=FS, blocksize=block,
                                      dtype="float32", latency="low", extra_settings=extra,
                                      callback=on_input))
            for ring, (dev, is_monitor) in zip(self.rings, outputs):
                ch = min(2, int(sd.query_devices(dev)["max_output_channels"])) or 1
                streams.append(sd.OutputStream(
                    device=dev, channels=ch, samplerate=FS, blocksize=block, dtype="float32",
                    latency="low", extra_settings=extra, callback=make_output(ring, is_monitor)))
            for s in streams:
                s.start()
        except Exception as exc:  # noqa: BLE001
            for s in streams:
                try:
                    s.close()
                except Exception:  # noqa: BLE001
                    pass
            self.error = str(exc)
            raise
        self.streams = streams
        self.running = True
        in_lat = streams[0].latency
        out_lat = streams[1].latency if len(streams) > 1 else 0.0
        # вход + обработка (1 блок шумодава) + буфер + выход
        self.latency_ms = round((in_lat + out_lat) * 1000 + block / FS * 1000 + target_fill / FS * 1000 / 2)

    def stop(self) -> None:
        for s in self.streams:
            try:
                s.stop()
                s.close()
            except Exception:  # noqa: BLE001
                pass
        self.streams = []
        self.running = False

    # --- управление ----------------------------------------------------------------

    def apply(self, settings: Settings) -> None:
        self.settings = settings
        if self.processor is not None:
            self.processor.apply(settings)

    # --- данные для интерфейса -----------------------------------------------------

    def _bands_db(self, x: np.ndarray) -> list[float]:
        spec = np.abs(np.fft.rfft(x * self._win)) / 1024.0
        power = spec ** 2
        out = []
        for a, b in self._bands:
            db = 10.0 * np.log10(power[a:b].mean() + 1e-12)
            out.append(round(max(0.0, min(1.0, (db + 96.0) / 90.0)), 3))
        return out

    def snapshot(self, with_spectrum: bool = True) -> dict:
        data = {"running": self.running, "latency_ms": self.latency_ms,
                "cpu": round(self._cpu * 100, 2), "error": self.error}
        if self.processor is not None and self.running:
            data.update(self.processor.stats())
            data["underruns"] = sum(r.underruns for r in self.rings)
        if with_spectrum and self.running:
            data["spec_in"] = self._bands_db(self._hist_in.copy())
            data["spec_out"] = self._bands_db(self._hist_out.copy())
        return data
