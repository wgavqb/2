"""Цепочка обработки голоса (как в студийных стрим-процессорах).

Срез низа → шумоподавление → шумовой гейт → эквалайзер → де-эссер →
компрессор → громкость → лимитер.

Всё считается блоками фиксированного размера (обычно 10 мс), векторно на
numpy/scipy, чтобы укладываться в реальное время с большим запасом.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields

import numpy as np
from scipy.signal import sosfilt

EPS = 1e-12


def db2lin(db):
    return np.power(10.0, np.asarray(db) / 20.0)


def lin2db(x) -> float:
    return 20.0 * math.log10(max(float(x), 1e-9))


# --- фильтры (RBJ Audio EQ Cookbook) ------------------------------------------------

IDENTITY = np.array([1.0, 0.0, 0.0, 1.0, 0.0, 0.0])


def biquad(kind: str, fs: float, f0: float, q: float = 0.7071, gain_db: float = 0.0) -> np.ndarray:
    """Одна секция второго порядка в формате scipy SOS: [b0, b1, b2, 1, a1, a2]."""
    f0 = min(f0, fs * 0.45)
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * f0 / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2.0 * q)
    sq = 2.0 * math.sqrt(a) * alpha
    if kind == "highpass":
        b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
        den = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "lowpass":
        b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]
        den = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "peak":
        b = [1 + alpha * a, -2 * cw, 1 - alpha * a]
        den = [1 + alpha / a, -2 * cw, 1 - alpha / a]
    elif kind == "lowshelf":
        b = [a * ((a + 1) - (a - 1) * cw + sq), 2 * a * ((a - 1) - (a + 1) * cw), a * ((a + 1) - (a - 1) * cw - sq)]
        den = [(a + 1) + (a - 1) * cw + sq, -2 * ((a - 1) + (a + 1) * cw), (a + 1) + (a - 1) * cw - sq]
    elif kind == "highshelf":
        b = [a * ((a + 1) + (a - 1) * cw + sq), -2 * a * ((a - 1) + (a + 1) * cw), a * ((a + 1) + (a - 1) * cw - sq)]
        den = [(a + 1) - (a - 1) * cw + sq, 2 * ((a - 1) - (a + 1) * cw), (a + 1) - (a - 1) * cw - sq]
    else:
        raise ValueError(kind)
    return np.array([b[0], b[1], b[2], den[0], den[1], den[2]]) / den[0]


class FilterChain:
    """Каскад биквадов с сохранением состояния между блоками.

    Число секций фиксировано, поэтому при смене настроек состояние не
    сбрасывается и не щёлкает."""

    def __init__(self, sections: list[np.ndarray]):
        self.sos = np.vstack(sections)
        self.zi = np.zeros((len(sections), 2))

    def set(self, sections: list[np.ndarray]) -> None:
        sos = np.vstack(sections)
        if sos.shape != self.sos.shape:
            self.zi = np.zeros((len(sections), 2))
        self.sos = sos

    def process(self, x: np.ndarray) -> np.ndarray:
        y, self.zi = sosfilt(self.sos, x, zi=self.zi)
        return y


# --- настройки ------------------------------------------------------------------

@dataclass
class Settings:
    highpass: bool = True
    highpass_hz: float = 90.0
    denoise: bool = True
    denoise_db: float = 20.0        # насколько глубоко давить шум
    gate: bool = True
    gate_db: float = -52.0          # порог открытия гейта, dBFS
    eq: bool = True
    warmth_db: float = 2.0          # низ 150 Гц — «бархат» голоса
    mud_db: float = -3.0            # 350 Гц — убирает «бубнёж»
    presence_db: float = 4.0        # 4 кГц — разборчивость
    air_db: float = 2.5             # 11 кГц — «воздух», дорогое звучание
    deess: bool = True
    deess_db: float = 6.0           # максимум подавления свиста «с/ш»
    comp: bool = True
    comp_amount: float = 55.0       # 0..100 — плотность голоса
    output_db: float = 0.0
    limiter: bool = True
    ceiling_db: float = -1.0
    bypass: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        s = cls()
        s.update(data)
        return s

    def update(self, data: dict) -> None:
        for f in fields(self):
            if f.name in data:
                value = data[f.name]
                setattr(self, f.name, bool(value) if f.type in (bool, "bool") else float(value))

    def to_dict(self) -> dict:
        return asdict(self)


PRESETS: dict[str, dict] = {
    "stream": {  # плотный «радийный» голос для стримов
        "highpass_hz": 90, "denoise_db": 22, "gate_db": -50, "warmth_db": 2.5, "mud_db": -3.5,
        "presence_db": 4.5, "air_db": 3.0, "deess_db": 7, "comp_amount": 65, "output_db": 1.0,
    },
    "discord": {  # максимум разборчивости и тишина между фразами
        "highpass_hz": 110, "denoise_db": 28, "gate_db": -46, "warmth_db": 0.5, "mud_db": -4.0,
        "presence_db": 5.0, "air_db": 1.5, "deess_db": 6, "comp_amount": 60, "output_db": 2.0,
    },
    "podcast": {  # тёплый, близкий голос
        "highpass_hz": 70, "denoise_db": 16, "gate_db": -56, "warmth_db": 3.5, "mud_db": -2.5,
        "presence_db": 3.0, "air_db": 2.0, "deess_db": 6, "comp_amount": 50, "output_db": 0.0,
    },
    "natural": {  # минимум окраски
        "highpass_hz": 75, "denoise_db": 12, "gate_db": -60, "warmth_db": 0.0, "mud_db": -1.5,
        "presence_db": 1.5, "air_db": 1.0, "deess_db": 4, "comp_amount": 30, "output_db": 0.0,
    },
}


# --- модули -----------------------------------------------------------------------

class Denoiser:
    """Спектральное шумоподавление (Винеровский фильтр + отслеживание минимума шума).

    Окно sqrt-Hann, перекрытие 50 %: задержка ровно один блок."""

    def __init__(self, fs: int, hop: int):
        self.hop = hop
        n = 2 * hop
        self.win = np.sqrt(np.hanning(n + 1)[:-1])
        self.prev = np.zeros(hop)
        self.ola = np.zeros(hop)
        self.psd = None
        self.noise = None
        self.gain = np.ones(hop + 1)
        # шум может «подниматься» не быстрее ~3 дБ/с — речь не принимается за шум
        self.rise = 10 ** (3.0 / 10.0 / (fs / hop))
        self.floor = db2lin(-20.0)
        self.reduction_db = 0.0

    def set_depth(self, depth_db: float) -> None:
        self.floor = float(db2lin(-abs(depth_db)))

    def process(self, x: np.ndarray) -> np.ndarray:
        frame = np.concatenate([self.prev, x])
        self.prev = x.copy()
        spec = np.fft.rfft(frame * self.win)
        p = spec.real ** 2 + spec.imag ** 2
        # сглаженный спектр — для слежения за уровнем шума
        self.psd = p if self.psd is None else 0.8 * self.psd + 0.2 * p
        if self.noise is None:
            self.noise = self.psd + EPS
            self.prev_post = np.ones_like(p)
        else:
            lower = self.psd < self.noise
            self.noise = np.where(lower, 0.85 * self.noise + 0.15 * self.psd, self.noise * self.rise)
        post = p / (self.noise * 2.2 + EPS)  # 2.2 — поправка: минимум ниже среднего уровня шума
        # «decision-directed» оценка SNR (Ephraim–Malah): без музыкального шума
        prio = 0.97 * self.gain ** 2 * self.prev_post + 0.03 * np.maximum(post - 1.0, 0.0)
        self.prev_post = post
        g = np.clip(prio / (1.0 + prio), self.floor, 1.0)
        g = np.convolve(g, (0.25, 0.5, 0.25), mode="same")
        self.gain = g
        self.reduction_db = -lin2db(float(np.mean(g)))
        out = np.fft.irfft(spec * g, 2 * self.hop) * self.win
        y = self.ola + out[: self.hop]
        self.ola = out[self.hop:].copy()
        return y


class Gate:
    """Шумовой гейт с гистерезисом и удержанием: тишина между фразами без обрезанных слов."""

    def __init__(self, fs: int):
        self.fs = fs
        self.threshold_db = -52.0
        self.floor = float(db2lin(-40.0))
        self.g = 1.0
        self.hold_left = 0
        self.hold = int(0.18 * fs)
        self.release = 0.12
        self.open = True

    def process(self, x: np.ndarray) -> np.ndarray:
        level = lin2db(math.sqrt(float(np.mean(x * x)) + EPS))
        close_at = self.threshold_db - 4.0
        if level > self.threshold_db or (self.open and level > close_at):
            self.open = True
            self.hold_left = self.hold
        elif self.hold_left > 0:
            self.hold_left -= len(x)
        else:
            self.open = False
        target = 1.0 if (self.open or self.hold_left > 0) else self.floor
        if target >= self.g:
            new = target  # открываемся мгновенно, чтобы не съесть начало слова
        else:
            new = target + (self.g - target) * math.exp(-len(x) / (self.release * self.fs))
        ramp = np.linspace(self.g, new, len(x), endpoint=False)
        self.g = new
        return x * ramp


def _segment_gain(prev_db: float, targets_db: np.ndarray, n: int, seg: int,
                  attack: float, release: float) -> tuple[np.ndarray, float]:
    """Сглаживает целевое усиление по сегментам и растягивает его на отсчёты."""
    out = np.empty(len(targets_db))
    g = prev_db
    for i, t in enumerate(targets_db.tolist()):
        coef = attack if t < g else release
        g = t + (g - t) * coef
        out[i] = g
    points = np.concatenate([[prev_db], out])
    xs = np.arange(len(points)) * seg
    per_sample = np.interp(np.arange(1, n + 1), xs, points)
    return per_sample, g


class Compressor:
    """Компрессор с мягким коленом и автоматической компенсацией громкости."""

    SEG = 32

    def __init__(self, fs: int):
        self.fs = fs
        self.gain_db = 0.0
        self.gr_db = 0.0
        self.attack = math.exp(-self.SEG / (0.004 * fs))
        self.release = math.exp(-self.SEG / (0.12 * fs))
        self.set_amount(55.0)

    def set_amount(self, amount: float) -> None:
        k = max(0.0, min(100.0, amount)) / 100.0
        self.threshold = -12.0 - 22.0 * k
        self.ratio = 1.5 + 3.5 * k
        self.knee = 8.0
        # компенсируем примерно половину средней компрессии — громкость голоса сохраняется
        self.makeup = -0.5 * self._curve(-12.0)

    def _curve(self, level_db):
        level_db = np.asarray(level_db, dtype=float)
        over = level_db - self.threshold
        slope = 1.0 - 1.0 / self.ratio
        knee = self.knee
        return np.where(over <= -knee / 2, 0.0,
               np.where(over >= knee / 2, -slope * over,
                        -slope * (over + knee / 2) ** 2 / (2 * knee)))

    def process(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        segs = -(-n // self.SEG)
        pad = np.zeros(segs * self.SEG)
        pad[:n] = np.abs(x)
        peaks = pad.reshape(segs, self.SEG).max(axis=1)
        levels = 20.0 * np.log10(peaks + 1e-9)
        per_sample, self.gain_db = _segment_gain(self.gain_db, self._curve(levels), n,
                                                 self.SEG, self.attack, self.release)
        self.gr_db = -float(per_sample.min())
        return x * db2lin(per_sample + self.makeup)


class DeEsser:
    """Приглушает резкие «с», «ш», «ч», которые вылезают после подъёма верха."""

    SEG = 32

    def __init__(self, fs: int):
        self.fs = fs
        self.split = FilterChain([biquad("highpass", fs, 5500.0)])
        self.max_db = 6.0
        self.threshold_db = -34.0
        self.gain_db = 0.0
        self.gr_db = 0.0
        self.attack = math.exp(-self.SEG / (0.001 * fs))
        self.release = math.exp(-self.SEG / (0.06 * fs))

    def process(self, x: np.ndarray) -> np.ndarray:
        high = self.split.process(x)
        low = x - high
        n = len(x)
        segs = -(-n // self.SEG)
        hp = np.zeros(segs * self.SEG)
        fp = np.zeros(segs * self.SEG)
        hp[:n] = high * high
        fp[:n] = x * x
        h = np.sqrt(hp.reshape(segs, self.SEG).mean(axis=1)) + 1e-9
        f = np.sqrt(fp.reshape(segs, self.SEG).mean(axis=1)) + 1e-9
        excess = 20.0 * np.log10(h) - self.threshold_db
        sibilant = (h / f) > 0.45
        targets = np.where(sibilant, -np.clip(excess, 0.0, self.max_db), 0.0)
        per_sample, self.gain_db = _segment_gain(self.gain_db, targets, n, self.SEG,
                                                 self.attack, self.release)
        self.gr_db = -float(per_sample.min())
        return low + high * db2lin(per_sample)


class Limiter:
    """Лимитер-«потолок»: голос никогда не хрипит и не перегружает Discord/OBS."""

    SEG = 16

    def __init__(self, fs: int):
        self.g = 1.0
        self.ceiling = float(db2lin(-1.0))
        self.release = math.exp(-self.SEG / (0.05 * fs))
        self.gr_db = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        segs = -(-n // self.SEG)
        pad = np.zeros(segs * self.SEG)
        pad[:n] = np.abs(x)
        peaks = pad.reshape(segs, self.SEG).max(axis=1) + 1e-9
        targets = np.minimum(1.0, self.ceiling / peaks)
        seg_gain = np.empty(segs)
        g = self.g
        for i, t in enumerate(targets.tolist()):  # ~30 скалярных шагов на блок
            g = t if t < g else t + (g - t) * self.release
            seg_gain[i] = g
        # плавный подъём между сегментами, но на атаке — сразу нужное усиление
        ramp = np.interp(np.arange(1, n + 1), np.arange(segs + 1) * self.SEG,
                         np.concatenate([[self.g], seg_gain]))
        gains = np.minimum(ramp, np.repeat(seg_gain, self.SEG)[:n])
        self.g = g
        self.gr_db = -lin2db(float(gains.min()))
        return np.clip(x * gains, -self.ceiling, self.ceiling)


# --- цепочка целиком ----------------------------------------------------------------

class Processor:
    def __init__(self, fs: int = 48000, block: int = 480, settings: Settings | None = None):
        self.fs, self.block = fs, block
        self.settings = settings or Settings()
        self.pre = FilterChain([IDENTITY])
        self.eq = FilterChain([IDENTITY] * 4)
        self.denoiser = Denoiser(fs, block)
        self.gate = Gate(fs)
        self.deesser = DeEsser(fs)
        self.comp = Compressor(fs)
        self.limiter = Limiter(fs)
        self.meters = {"in_db": -120.0, "out_db": -120.0}
        self.apply(self.settings)

    def apply(self, s: Settings) -> None:
        self.settings = s
        fs = self.fs
        self.pre.set([biquad("highpass", fs, s.highpass_hz) if s.highpass else IDENTITY])
        self.eq.set([
            biquad("lowshelf", fs, 150.0, 0.7, s.warmth_db),
            biquad("peak", fs, 350.0, 1.0, s.mud_db),
            biquad("peak", fs, 4000.0, 0.9, s.presence_db),
            biquad("highshelf", fs, 11000.0, 0.7, s.air_db),
        ])
        self.denoiser.set_depth(s.denoise_db)
        self.gate.threshold_db = s.gate_db
        self.deesser.max_db = s.deess_db
        self.comp.set_amount(s.comp_amount)
        self.limiter.ceiling = float(db2lin(s.ceiling_db))

    def process(self, x: np.ndarray) -> np.ndarray:
        """x — моно float, длина кратна block. Возвращает обработанный сигнал той же длины."""
        x = np.asarray(x, dtype=np.float64)
        if len(x) % self.block:
            raise ValueError(f"длина блока должна быть кратна {self.block}")
        return np.concatenate([self._block(x[i:i + self.block])
                               for i in range(0, len(x), self.block)]) if len(x) else x

    def _block(self, x: np.ndarray) -> np.ndarray:
        s = self.settings
        self.meters["in_db"] = lin2db(float(np.max(np.abs(x))) if len(x) else 0.0)
        y = self.pre.process(x)
        # шумодав всегда работает (держит задержку постоянной), при выключении просто без подавления
        y = self.denoiser.process(y) if s.denoise else self._delay(y)
        if s.bypass:
            out = self._dry(x)
            self.meters["out_db"] = lin2db(float(np.max(np.abs(out))))
            return out
        if s.gate:
            y = self.gate.process(y)
        if s.eq:
            y = self.eq.process(y)
        if s.deess and s.deess_db > 0:
            y = self.deesser.process(y)
        if s.comp:
            y = self.comp.process(y)
        y = y * float(db2lin(s.output_db))
        if s.limiter:
            y = self.limiter.process(y)
        self.meters["out_db"] = lin2db(float(np.max(np.abs(y))))
        return y

    # задержки, чтобы режим «оригинал» (A/B) не прыгал по времени
    def _delay(self, y: np.ndarray) -> np.ndarray:
        prev = getattr(self, "_dly", np.zeros(self.block))
        self._dly = y.copy()
        return prev

    def _dry(self, x: np.ndarray) -> np.ndarray:
        prev = getattr(self, "_dry_prev", np.zeros(self.block))
        self._dry_prev = x.copy()
        return prev

    def stats(self) -> dict:
        s = self.settings
        return {
            **self.meters,
            "gate_open": bool(self.gate.open) if s.gate else True,
            "denoise_db": round(self.denoiser.reduction_db, 1) if s.denoise else 0.0,
            "comp_db": round(self.comp.gr_db, 1) if s.comp else 0.0,
            "deess_db": round(self.deesser.gr_db, 1) if s.deess else 0.0,
            "limit_db": round(self.limiter.gr_db, 1) if s.limiter else 0.0,
        }
