"""
soundtrack.py — своя фоновая музыка и звуковые акценты для роликов 9:16.

Музыка синтезируется здесь же (аддитивный синтез на numpy): никаких чужих треков —
никаких прав и блокировок, звук одинаковый на TikTok/Reels/Shorts. Варианты (аккорды,
темп) выбираются по seed, чтобы ролики не звучали одинаково.

build(duration, seed, cues) -> WAV-байты; cues — [(время_с, 'win'|'loss'|'tick')].
"""
from __future__ import annotations

import io
import wave

import numpy as np

SR = 44100
# прогрессии (полутоны от A2 = 110 Гц): тёмный «тех»-минор, разные настроения
_PROGRESSIONS = [
    [(0, 3, 7, 10), (-4, 0, 3, 7), (3, 7, 10, 14), (-2, 2, 5, 9)],     # Am7 Fmaj7 Cmaj7 G6
    [(0, 3, 7, 12), (-2, 2, 5, 10), (-4, 0, 3, 8), (-5, -1, 2, 7)],    # Am G F E-ish
    [(5, 8, 12, 15), (3, 7, 10, 14), (0, 3, 7, 10), (-2, 2, 5, 9)],    # Dm7 C Am7 G
]


def _hz(semi: float) -> float:
    return 110.0 * 2 ** (semi / 12)


def _env(n: int, attack: float, release: float) -> np.ndarray:
    e = np.ones(n)
    a, r = min(n, int(attack * SR)), min(n, int(release * SR))
    if a:
        e[:a] = np.linspace(0, 1, a)
    if r:
        e[-r:] *= np.linspace(1, 0, r)
    return e


def _tone(freq: float, dur: float, harmonics: int = 6, detune: float = 0.003) -> np.ndarray:
    """Мягкая «пила»: гармоники с 1/n и лёгким расстроем — тёплый пэд без фильтров."""
    t = np.arange(int(dur * SR)) / SR
    out = np.zeros_like(t)
    for d in (-detune, detune):
        f = freq * (1 + d)
        for k in range(1, harmonics + 1):
            out += np.sin(2 * np.pi * f * k * t) / (k ** 1.6)
    return out / 4


def _pluck(freq: float, dur: float = 0.35, bright: int = 5) -> np.ndarray:
    t = np.arange(int(dur * SR)) / SR
    out = np.sum([np.sin(2 * np.pi * freq * k * t) * np.exp(-t * (6 + 5 * k)) / k
                  for k in range(1, bright + 1)], axis=0)
    return out * _env(len(t), 0.003, 0.05)


def _kick(dur: float = 0.32) -> np.ndarray:
    t = np.arange(int(dur * SR)) / SR
    f = 45 + 85 * np.exp(-t * 28)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 9)


def _hat(rng: np.random.Generator, dur: float = 0.045) -> np.ndarray:
    n = rng.standard_normal(int(dur * SR))
    n = np.diff(n, prepend=0.0)                       # грубый хай-пасс
    return n * np.exp(-np.arange(len(n)) / SR * 70) * 0.5


def _add(buf: np.ndarray, sig: np.ndarray, at: float, gain: float) -> None:
    i = int(at * SR)
    if i >= len(buf):
        return
    j = min(len(buf), i + len(sig))
    buf[i:j] += sig[:j - i] * gain


def _sting(kind: str) -> np.ndarray:
    """Акцент на итоге: плюс — восходящее мажорное арпеджио; минус — мягкий нисходящий тон."""
    out = np.zeros(int(1.6 * SR))
    if kind == "win":
        for k, semi in enumerate((12, 16, 19, 24, 28)):
            _add(out, _pluck(_hz(semi + 12), 0.9), k * 0.07, 0.55)
    elif kind == "loss":
        for k, semi in enumerate((7, 0)):
            _add(out, _tone(_hz(semi), 0.9, harmonics=3) * _env(int(0.9 * SR), 0.01, 0.6),
                 k * 0.22, 0.45)
    elif kind == "tw":                                 # плитка-плюс (итог недели)
        out = _pluck(_hz(31), 0.18, bright=3) * 0.35
    elif kind == "tl":                                 # плитка-минус
        out = _pluck(_hz(19), 0.18, bright=2) * 0.30
    else:                                              # tick
        out = _pluck(_hz(31), 0.12, bright=3) * 0.5
    return out


def build(duration: float, seed: int = 0, cues: list | None = None,
          bpm: float | None = None) -> bytes:
    """Фоновая дорожка длиной duration (с): пэд по аккордам, бас, бочка, хэт, арпеджио,
    «сайдчейн» пэда от бочки, мягкий лимитер, фейды. -> WAV (16 бит, стерео)."""
    rng = np.random.default_rng(seed)
    prog = _PROGRESSIONS[seed % len(_PROGRESSIONS)]
    bpm = bpm or (96, 100, 104)[seed % 3]
    beat = 60.0 / bpm
    bar = 4 * beat
    n = int((duration + 0.5) * SR)
    pad, bass, drums, arp = (np.zeros(n) for _ in range(4))
    t_bar, i = 0.0, 0
    while t_bar < duration:
        chord = prog[(i // 2) % len(prog)]           # аккорд держится 2 такта
        if i % 2 == 0:
            ln = 2 * bar + 0.4
            seg = np.sum([_tone(_hz(s + 12), ln) for s in chord], axis=0)
            _add(pad, seg * _env(len(seg), 0.6, 0.8), t_bar, 0.16)
        root = _hz(chord[0] - 12)
        for b in range(4):
            tb = t_bar + b * beat
            bl = _tone(root, beat * 0.95, harmonics=2, detune=0.0)
            _add(bass, bl * _env(len(bl), 0.01, 0.12), tb, 0.30 if b in (0, 2) else 0.18)
            if b in (0, 2) and i >= 1:
                _add(drums, _kick(), tb, 0.50)
        if i >= 2:                                    # хэты и арпеджио со 3-го такта
            for e in range(8):
                _add(drums, _hat(rng), t_bar + e * beat / 2, 0.06 if e % 2 else 0.03)
            for s in range(16):
                note = chord[s % len(chord)] + (24 if (s // 4) % 2 else 12)
                _add(arp, _pluck(_hz(note), 0.25, bright=3), t_bar + s * beat / 4, 0.07)
        t_bar += bar
        i += 1
    # «сайдчейн»: пэд и арпеджио приседают после каждой бочки
    duck = np.ones(n)
    k = 0.0
    while k < duration:
        a, b = int(k * SR), min(n, int((k + 0.28) * SR))
        duck[a:b] = np.minimum(duck[a:b], np.linspace(0.45, 1.0, b - a))
        k += 2 * beat
    mix = (pad + arp) * duck + bass + drums
    for at, kind in cues or []:
        _add(mix, _sting(kind), at, 0.9)
    mix = np.tanh(mix * 1.2)                          # мягкий лимитер
    mix = mix[:int(duration * SR)]
    mix *= _env(len(mix), 0.4, 1.6)
    mix /= max(1e-9, float(np.max(np.abs(mix)))) / 0.89
    # стерео: пэд/арпеджио слегка шире (микро-задержка правого канала)
    right = np.concatenate([mix[:int(0.012 * SR)] * 0, mix[:-int(0.012 * SR)]]) * 0.35 + mix * 0.65
    stereo = np.stack([mix, right], axis=1)
    pcm = (np.clip(stereo, -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()
