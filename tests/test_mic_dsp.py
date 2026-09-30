import time

import numpy as np
import pytest

from mic_booster.dsp import (
    PRESETS, Compressor, Limiter, Processor, Settings, biquad, db2lin, lin2db,
)
from scipy.signal import sosfreqz

FS = 48000
BLOCK = 480


def blocks(x):
    return x[: len(x) // BLOCK * BLOCK]


def tone(freq, seconds=1.0, amp=0.1):
    t = np.arange(int(FS * seconds)) / FS
    return blocks(amp * np.sin(2 * np.pi * freq * t))


def gain_at(sos, freq):
    _, h = sosfreqz(sos[None, :], worN=[freq], fs=FS)
    return 20 * np.log10(abs(h[0]))


@pytest.mark.parametrize("kind,f0,gain,probe,expect", [
    ("peak", 4000, 4.0, 4000, 4.0),
    ("peak", 350, -3.0, 350, -3.0),
    ("lowshelf", 150, 2.0, 40, 2.0),
    ("highshelf", 11000, 2.5, 20000, 2.5),
    ("highpass", 90, 0, 20, -26.1),
])
def test_biquads(kind, f0, gain, probe, expect):
    sos = biquad(kind, FS, f0, 0.7071 if kind != "peak" else 1.0, gain)
    assert gain_at(sos, probe) == pytest.approx(expect, abs=1.0)
    # фильтр устойчив: полюса внутри единичного круга
    assert np.all(np.abs(np.roots(sos[3:])) < 1)


def test_denoiser_removes_steady_noise_keeps_voice():
    rng = np.random.default_rng(1)
    noise = blocks(0.003 * rng.standard_normal(FS * 3))
    p = Processor(FS, BLOCK, Settings(gate=False, comp=False, eq=False, deess=False))
    out = p.process(noise)
    assert lin2db(out[FS:].std()) < lin2db(noise[FS:].std()) - 12

    # после «обучения» на шуме громкий голос почти не теряется
    voice = tone(220, 1.0, 0.2)
    out = p.process(np.concatenate([noise[: FS // 2 // BLOCK * BLOCK], voice + noise[: len(voice)]]))
    tail = out[-FS // 2:]
    assert lin2db(tail.std()) > lin2db(voice.std()) - 3


def test_gate_silences_between_phrases():
    rng = np.random.default_rng(2)
    quiet = blocks(0.0005 * rng.standard_normal(FS))
    p = Processor(FS, BLOCK, Settings(denoise=False, comp=False, eq=False, deess=False, gate_db=-50))
    out = p.process(np.concatenate([tone(300, 0.5, 0.2), quiet]))
    assert lin2db(out[-FS // 2:].std()) < lin2db(quiet.std()) - 25


def test_compressor_evens_out_loud_parts():
    c = Compressor(FS)
    c.set_amount(80)
    loud = c.process(tone(200, 0.5, 0.9))
    quiet_c = Compressor(FS)
    quiet_c.set_amount(80)
    quiet = quiet_c.process(tone(200, 0.5, 0.05))
    in_diff = lin2db(0.9) - lin2db(0.05)
    out_diff = lin2db(np.abs(loud[-2400:]).max()) - lin2db(np.abs(quiet[-2400:]).max())
    assert out_diff < in_diff - 6


def test_limiter_never_clips():
    lim = Limiter(FS)
    x = tone(150, 0.5, 3.0)
    y = lim.process(x)
    assert np.abs(y).max() <= db2lin(-1.0) + 1e-9


def test_full_chain_presets_are_safe_and_fast():
    rng = np.random.default_rng(3)
    x = blocks(sum(np.sin(2 * np.pi * 150 * k * np.arange(FS * 4) / FS) / k for k in range(1, 12)) * 0.3
               + 0.002 * rng.standard_normal(FS * 4))
    for name, preset in PRESETS.items():
        p = Processor(FS, BLOCK, Settings.from_dict(preset))
        start = time.perf_counter()
        y = p.process(x)
        elapsed = time.perf_counter() - start
        assert len(y) == len(x) and np.all(np.isfinite(y)), name
        assert np.abs(y).max() <= db2lin(-1.0) + 1e-9, name
        assert elapsed < 4.0 * 0.25, f"{name}: обработка медленнее реального времени"


def test_bypass_is_time_aligned_dry_signal():
    p = Processor(FS, BLOCK, Settings(bypass=True))
    x = tone(500, 0.1)
    y = p.process(x)
    assert np.allclose(y[BLOCK:], x[:-BLOCK])


def test_settings_roundtrip():
    s = Settings.from_dict({"gate": 0, "comp_amount": "70", "unknown": 1})
    assert s.gate is False and s.comp_amount == 70.0
    assert Settings.from_dict(s.to_dict()) == s


def test_ring_buffer_order_drift_and_underrun():
    from mic_booster.engine import Ring

    r = Ring(10)
    r.write(np.arange(4, dtype=np.float32))
    assert list(r.read(2, max_fill=100)) == [0, 1]
    r.write(np.arange(4, 12, dtype=np.float32))  # переполнение: старое выбрасывается
    assert r.fill == 10
    # буфер разросся больше целевого — читатель догоняет до свежих данных
    assert list(r.read(2, max_fill=3)) == [9, 10]
    out = r.read(4, max_fill=100)
    assert list(out) == [11, 0, 0, 0] and r.underruns == 1
