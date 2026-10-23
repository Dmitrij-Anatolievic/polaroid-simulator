#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║  POLAROID SIMULATOR v6 — Reclaimed Series                        ║
║──────────────────────────────────────────────────────────────────║
║  • Curva Green 600: 6237 punti da 2 scansioni reali (PCHIP)      ║
║  • Curve misurate da stampe reali: Reclaimed Blue/Purple,        ║
║    Duochrome Yellow/Green/Blue/Pink · Color 600 · B&N 600        ║
║  • Ottica plastica, aberrazione, alone sulle luci                ║
║  • Sviluppo irregolare, bordi chimici, lingue di reagente,       ║
║    polvere, grana fisica legata alla luminanza                   ║
║  • Geometria reale: 600/i-Type 88×107 mm (finestra 79×79),       ║
║    Go 53,9×66,6 mm (finestra 47×46)                              ║
║  • Cornici: classiche, Color Frame, pastello, metallic,          ║
║    gradienti, finestra rotonda, colore libero, PNG esterno       ║
║  • Inquadratura interattiva: rotella = zoom, trascina = sposta,   ║
║    l'immagine non esce mai dalla finestra della cornice          ║
║                                                                  ║
║  Autore: Dmitrij Musella                                         ║
║  Engine: NumPy + Pillow + PyQt6                                  ║
╚══════════════════════════════════════════════════════════════════╝

Requisiti:  pip install PyQt6 Pillow numpy
Uso:        python polaroid_green600.py
"""

import sys, os, math, zlib, random
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

if sys.platform == "win32":
    _wf = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    if os.path.isdir(_wf):
        os.environ["QT_QPA_FONTDIR"] = _wf

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QSlider, QCheckBox, QLineEdit, QFileDialog,
    QGroupBox, QComboBox, QSplitter, QListWidget, QSizePolicy,
    QProgressBar, QMessageBox, QAbstractItemView, QScrollArea, QColorDialog,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QRectF
from PyQt6.QtGui import QImage, QPixmap, QAction, QColor, QPainter, QPen, QBrush

Image.MAX_IMAGE_PIXELS = 300_000_000

# ═══════════════════════════════════════════════════════════════
# DATI PELLICOLA
# ═══════════════════════════════════════════════════════════════

FILM_DATA = [
    # lum,  R,   G,   B  — verde saturo + gialli saturi (scansioni reali)
    (  0,   0,   5,   0),
    ( 20,   4,  22,   4),
    ( 35,   7,  45,   6),
    ( 50,  10,  68,   8),
    ( 65,  13,  92,  10),
    ( 80,  16, 115,  12),
    ( 95,  20, 135,  15),
    (110,  26, 150,  18),
    (125,  35, 158,  22),
    (135,  48, 162,  26),
    (145,  65, 164,  30),
    (155,  95, 165,  32),
    (165, 140, 162,  34),
    (175, 178, 165,  38),
    (185, 205, 175,  42),
    (195, 222, 188,  48),
    (210, 236, 210,  58),
    (225, 242, 228,  70),
    (255, 250, 242,  85),
]

PAL = [(196,196,22),(174,177,36),(132,154,46),(100,132,56),(62,118,55),
       (56,121,63),(13,107,57),(26,87,56),(30,72,48),(26,43,37)]


@dataclass(frozen=True)
class FilmProfile:
    mode: str                         # "lut" | "color" | "bw"
    points: tuple = ()
    sat: float = 1.0
    contrast: float = 0.0             # pendenza S-curve (0 = lineare)
    black: tuple = (0.0, 0.0, 0.0)    # densità minima (neri mai neri)
    white: tuple = (1.0, 1.0, 1.0)    # bianchi cremosi
    shadow_tint: tuple = (0, 0, 0)
    high_tint: tuple = (0, 0, 0)
    halation: tuple = (1.0, 0.85, 0.6)
    rim: tuple = (40, 40, 40)         # colore del bordo chimico
    blob: tuple = (225, 220, 210)     # colore del reagente non steso
    default_lift: int = 0
    frame: str = "Classica · Bianca"   # cornice originale dell'edizione


def _duo(pts, lift=6, blob=None, frame="Classica · Bianca"):
    """Profilo LUT da punti (lum_ingresso, R, G, B). Bordi e alone derivati dalla curva."""
    pts = tuple(tuple(int(v) for v in p) for p in pts)
    hi = np.array(pts[-3][1:], float)
    hal = tuple(float(v) for v in hi / max(hi.max(), 1))
    xs = [p[0] for p in pts]
    if blob is None:
        blob = tuple(int(np.interp(205, xs, [p[c] for p in pts])) for c in (1, 2, 3))
    return FilmProfile(mode="lut", points=pts, halation=hal, rim=tuple(pts[1][1:]),
                       blob=blob, default_lift=lift, frame=frame)


# Curve MISURATE (ottobre 2026) campionando i pixel di stampe reali:
#  · Green 600   — 6237 punti da 2 scansioni (dato originale)
#  · Blue 600    — 3 scansioni Analog Cafe + stampa ufficiale Polaroid + 2 stampe r/Polaroid (media)
#  · Purple 600  — 7 scansioni Analog Cafe + 4 stampe r/Polaroid (media)
#  · Duochrome Black & Green  — 3 stampe ufficiali Polaroid (PetaPixel)
#  · Duochrome Black & Yellow — 9 scansioni EchoEcho Studio
#  · Duochrome Black & Blue / Pink — foto di stampe, Casual Photophile
#    (Pink: nero profondo stimato, il campione non aveva ombre dense)
# Ogni curva: luminanza stampa → ingresso 0–255, lisciata e resa monotona.
FILM_GROUPS = [
    ("Reclaimed", "Green 600", FilmProfile(
        mode="lut", points=tuple(FILM_DATA),
        halation=(0.80, 1.0, 0.30), rim=(16, 44, 30), blob=(206, 198, 90), default_lift=10)),
    ("Reclaimed", "Blue 600",  _duo(((0, 1, 20, 57), (21, 8, 41, 67), (42, 9, 67, 97), (64, 9, 93, 121), (85, 17, 117, 139), (106, 36, 138, 154), (128, 62, 154, 167), (149, 91, 169, 177), (170, 124, 184, 187), (191, 158, 196, 196), (212, 184, 211, 206), (234, 210, 228, 222), (255, 242, 239, 229)))),
    ("Reclaimed", "Purple 600", _duo(((0, 2, 1, 55), (21, 16, 10, 75), (42, 31, 20, 95), (64, 43, 30, 121), (85, 55, 41, 146), (106, 75, 50, 162), (128, 104, 56, 174), (149, 130, 62, 187), (170, 148, 72, 199), (191, 158, 86, 207), (212, 168, 102, 216), (234, 181, 116, 226), (255, 193, 127, 238)))),
    ("Duochrome", "Black & Yellow", _duo(((0, 61, 48, 16), (21, 75, 59, 20), (42, 89, 72, 25), (64, 104, 86, 32), (85, 121, 101, 39), (106, 137, 116, 44), (128, 153, 130, 50), (149, 169, 144, 56), (170, 186, 159, 62), (191, 203, 174, 68), (212, 219, 188, 73), (234, 235, 202, 78), (255, 249, 215, 83)),
        lift=0, blob=(224, 160, 42), frame="Classica · Nera")),
    ("Duochrome", "Black & Green",  _duo(((0, 23, 32, 12), (21, 28, 51, 12), (42, 30, 73, 12), (64, 30, 96, 13), (85, 37, 113, 21), (106, 50, 128, 33), (128, 57, 149, 42), (149, 67, 165, 53), (170, 79, 179, 59), (191, 90, 198, 59), (212, 104, 218, 59), (234, 115, 235, 59), (255, 125, 252, 62)), lift=0, frame="Classica · Nera")),
    ("Duochrome", "Black & Blue",   _duo(((0, 2, 10, 11), (21, 19, 37, 44), (42, 25, 53, 63), (64, 30, 67, 80), (85, 34, 93, 111), (106, 53, 117, 134), (128, 76, 134, 145), (149, 76, 167, 182), (170, 76, 193, 209), (191, 77, 214, 231), (212, 111, 226, 242), (234, 163, 231, 242), (255, 200, 242, 247)), lift=0, frame="Classica · Nera")),
    ("Duochrome", "Black & Pink",   _duo(((0, 14, 10, 13), (21, 29, 22, 28), (42, 45, 33, 42), (64, 74, 48, 67), (85, 102, 57, 90), (106, 127, 67, 110), (128, 154, 78, 132), (149, 181, 87, 153), (170, 207, 96, 175), (191, 230, 106, 197), (212, 248, 122, 218), (234, 255, 142, 236), (255, 255, 162, 249)), lift=0, frame="Classica · Nera")),
    ("Classica", "Color 600", FilmProfile(
        mode="color", sat=0.80, contrast=4.0,
        black=(0.10, 0.105, 0.125), white=(0.975, 0.955, 0.905),
        shadow_tint=(-0.035, 0.005, 0.03), high_tint=(0.035, 0.015, -0.035),
        halation=(1.0, 0.78, 0.55), rim=(62, 42, 34), blob=(236, 214, 162))),
    ("Classica", "B&N 600", FilmProfile(
        mode="bw", contrast=5.0,
        black=(0.085, 0.085, 0.09), white=(0.955, 0.948, 0.925),
        halation=(1.0, 1.0, 0.97), rim=(35, 35, 36), blob=(228, 226, 220))),
    ("Libera", "Duotono personalizzato…", None),
]
FILMS = {f"{g} · {n}": prof for g, n, prof in FILM_GROUPS}
CUSTOM_FILM = "Libera · Duotono personalizzato…"


def film_profile(s):
    if s["film"] != CUSTOM_FILM:
        return FILMS[s["film"]]
    sh, hi = s.get("film_colors") or ((20, 30, 90), (250, 230, 200))
    mix = tuple((a + b) // 2 for a, b in zip(sh, hi))
    deep = tuple(int(c * 0.22) for c in sh)
    return _duo([(0, *deep), (90, *sh), (175, *mix), (255, *hi)])

# ═══════════════════════════════════════════════════════════════
# FORMATI E CORNICI
# ═══════════════════════════════════════════════════════════════

# larghezza, altezza, finestra L, finestra H, margine sx, margine alto (mm)
FORMATS = {
    "600 / i-Type / SX-70 (88×107 mm)": (88.0, 107.0, 79.0, 79.0, 4.5, 6.0),
    "Go (53,9×66,6 mm)":                (53.9, 66.6, 47.0, 46.0, 3.45, 4.6),
}

FRAME_STYLES = [
    ("Classica",    "Bianca",            dict(kind="solid", c=(244, 243, 238))),
    ("Classica",    "Nera",              dict(kind="solid", c=(26, 26, 28))),
    ("Classica",    "Avorio Reclaimed",  dict(kind="solid", c=(235, 230, 216))),
    ("Color Frame", "Blu",               dict(kind="solid", c=(28, 92, 168))),
    ("Color Frame", "Rosso",             dict(kind="solid", c=(206, 38, 44))),
    ("Color Frame", "Giallo",            dict(kind="solid", c=(246, 198, 46))),
    ("Color Frame", "Verde",             dict(kind="solid", c=(24, 150, 92))),
    ("Color Frame", "Arancio",           dict(kind="solid", c=(238, 118, 38))),
    ("Color Frame", "Verde Bosco",       dict(kind="solid", c=(62, 118, 55))),
    ("Pastello",    "Rosa",              dict(kind="solid", c=(240, 190, 196))),
    ("Pastello",    "Menta",             dict(kind="solid", c=(176, 222, 200))),
    ("Pastello",    "Lavanda",           dict(kind="solid", c=(196, 184, 226))),
    ("Pastello",    "Cielo",             dict(kind="solid", c=(170, 206, 236))),
    ("Pastello",    "Crema",             dict(kind="solid", c=(246, 232, 190))),
    ("Metallic",    "Oro",               dict(kind="metal", c=(204, 166, 86))),
    ("Metallic",    "Argento",           dict(kind="metal", c=(190, 193, 198))),
    ("Metallic",    "Rame",              dict(kind="metal", c=(186, 112, 70))),
    ("Metallic",    "Noir",              dict(kind="metal", c=(52, 52, 56))),
    ("Gradiente",   "Tramonto",          dict(kind="gradient", stops=[(255, 96, 80), (255, 160, 64), (255, 214, 110)])),
    ("Gradiente",   "Oceano",            dict(kind="gradient", stops=[(24, 58, 120), (38, 132, 178), (142, 214, 218)])),
    ("Gradiente",   "Aurora",            dict(kind="gradient", stops=[(60, 40, 120), (40, 140, 150), (150, 230, 160)])),
    ("Gradiente",   "Green 600",         dict(kind="gradient", stops=[PAL[0], PAL[3], PAL[6], PAL[9]])),
    ("Libera",      "Colore personalizzato…", dict(kind="solid", c=(244, 243, 238), custom=True)),
    ("Libera",      "Da file PNG…",      dict(kind="file")),
]
FRAME_MAP = {f"{g} · {n}": spec for g, n, spec in FRAME_STYLES}
SHAPES = ["Quadrata", "Rotonda"]
MAX_ZOOM = 8.0


# ═══════════════════════════════════════════════════════════════
# UTILITÀ NUMERICHE (niente SciPy)
# ═══════════════════════════════════════════════════════════════

def _box1d(a, r, axis):
    if r < 1:
        return a
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r + 1, r)
    c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis, dtype=np.float32)
    n = a.shape[axis]
    hi = np.take(c, np.arange(2 * r + 1, 2 * r + 1 + n), axis=axis)
    lo = np.take(c, np.arange(0, n), axis=axis)
    return (hi - lo) / np.float32(2 * r + 1)


def _gauss(a, sigma):
    """Blur gaussiano separabile (3 box per σ grandi, kernel 5-tap per σ piccoli)."""
    a = a.astype(np.float32, copy=False)
    if sigma < 0.3:
        return a
    if sigma < 1.3:
        xs = np.arange(-2, 3)
        k = np.exp(-(xs ** 2) / (2 * sigma * sigma)); k /= k.sum()
        for axis in (0, 1):
            pad = [(0, 0)] * a.ndim; pad[axis] = (2, 2)
            p = np.pad(a, pad, mode="edge"); n = a.shape[axis]
            a = sum(np.float32(k[i]) * np.take(p, np.arange(i, i + n), axis=axis) for i in range(5))
        return a
    r = max(1, int(round((math.sqrt(4 * sigma * sigma + 1) - 1) / 2)))
    for _ in range(3):
        a = _box1d(_box1d(a, r, 0), r, 1)
    return a


def _lum(a):
    return a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114


def _radial(h, w):
    y = (np.arange(h, dtype=np.float32) + 0.5) / h * 2 - 1
    x = (np.arange(w, dtype=np.float32) + 0.5) / w * 2 - 1
    return np.sqrt(x[None, :] ** 2 + y[:, None] ** 2) / math.sqrt(2)


def _resize_f(a2d, size):
    return np.asarray(Image.fromarray(a2d.astype(np.float32)).resize(size, Image.BICUBIC), dtype=np.float32)


def _noise_field(rng, gh, gw, h, w):
    """Campo di rumore a bassa frequenza, indipendente dalla risoluzione."""
    return _resize_f(rng.standard_normal((gh, gw)).astype(np.float32), (w, h))


def _smooth1d(rng, n, knots=48):
    k = np.convolve(rng.standard_normal(knots + 4), np.ones(3) / 3, mode="same")
    v = np.interp(np.linspace(2, knots + 1, n), np.arange(knots + 4), k)
    return (v / (np.abs(v).max() + 1e-6)).astype(np.float32)


def _sdf_round_rect(px, py, cx, cy, hx, hy, r):
    qx = np.abs(px - cx) - (hx - r)
    qy = np.abs(py - cy) - (hy - r)
    outside = np.sqrt(np.maximum(qx, 0) ** 2 + np.maximum(qy, 0) ** 2)
    return outside + np.minimum(np.maximum(qx, qy), 0) - r


def _pchip(xk, yk, x):
    """Interpolazione cubica monotona (Fritsch–Carlson): niente overshoot né plateau."""
    xk = np.asarray(xk, float); yk = np.asarray(yk, float)
    h = np.diff(xk); d = np.diff(yk) / h
    m = np.zeros_like(yk); m[0] = d[0]; m[-1] = d[-1]
    for i in range(1, len(xk) - 1):
        if d[i - 1] * d[i] <= 0:
            m[i] = 0
        else:
            w1 = 2 * h[i] + h[i - 1]; w2 = h[i] + 2 * h[i - 1]
            m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])
    idx = np.clip(np.searchsorted(xk, x) - 1, 0, len(xk) - 2)
    t = (x - xk[idx]) / h[idx]
    t2, t3 = t * t, t * t * t
    return ((2 * t3 - 3 * t2 + 1) * yk[idx] + (t3 - 2 * t2 + t) * h[idx] * m[idx]
            + (-2 * t3 + 3 * t2) * yk[idx + 1] + (t3 - t2) * h[idx] * m[idx + 1])


@lru_cache(maxsize=8)
def _film_lut(points):
    xs = [p[0] for p in points]
    x = np.linspace(0, 255, 1024)
    lut = np.stack([np.clip(_pchip(xs, [p[c] for p in points], x), 0, 255) for c in (1, 2, 3)], 1)
    return (lut / 255).astype(np.float32)


def _scurve(x, k):
    if k < 1e-3:
        return x
    s = lambda t: 1 / (1 + np.exp(-t))
    a, b = s(-k / 2), s(k / 2)
    return ((s(k * (x - 0.5)) - a) / (b - a)).astype(np.float32)


# ═══════════════════════════════════════════════════════════════
# SVILUPPO (tutto in coordinate della finestra)
# ═══════════════════════════════════════════════════════════════

def apply_film(arr, prof, strength, lift):
    src = arr
    if prof.mode == "lut":
        idx = np.clip(_lum(arr) * 1023 + 0.5, 0, 1023).astype(np.int32)
        out = _film_lut(prof.points)[idx]
    elif prof.mode == "color":
        L = _lum(arr)[..., None]
        out = np.clip(L + (arr - L) * prof.sat, 0, 1)
        out = _scurve(out, prof.contrast)
        Ln = _lum(out)[..., None]
        out = out + np.array(prof.shadow_tint, np.float32) * (1 - Ln) ** 2 \
                  + np.array(prof.high_tint, np.float32) * Ln ** 2
    else:
        out = np.repeat(_scurve(_lum(arr), prof.contrast)[..., None], 3, axis=2)
    if prof.mode != "lut":
        b = np.array(prof.black, np.float32); w = np.array(prof.white, np.float32)
        out = b + (w - b) * np.clip(out, 0, 1)
    out = out * strength + src * (1 - strength)
    if lift > 0:
        out = lift + out * (1 - lift)
    return out.astype(np.float32)


def _chromatic(arr, k):
    h, w = arr.shape[:2]
    out = arr.copy()
    for c, s in ((0, 1 + k), (1, 1 + k * 0.5)):
        nw, nh = int(round(w * s)), int(round(h * s))
        if nw <= w:
            continue
        big = _resize_f(arr[..., c], (nw, nh))
        x0, y0 = (nw - w) // 2, (nh - h) // 2
        out[..., c] = big[y0:y0 + h, x0:x0 + w]
    return out


def _edge_chem(arr, ppmm, rng, amt, prof):
    """Bordo chimico irregolare + lingue di reagente non steso (lato opposto al pod)."""
    h, w = arr.shape[:2]
    y = np.arange(h, dtype=np.float32)[:, None]
    x = np.arange(w, dtype=np.float32)[None, :]
    soft = max(0.6, 0.12 * ppmm)
    base = 0.35 * ppmm

    def band(width, dist):
        return np.clip((width - dist) / soft + 0.5, 0, 1)

    wt = base * (1 + 0.55 * _smooth1d(rng, w))[None, :]
    wb = base * (1 + 0.55 * _smooth1d(rng, w))[None, :]
    wl = base * (1 + 0.55 * _smooth1d(rng, h))[:, None]
    wr = base * (1 + 0.55 * _smooth1d(rng, h))[:, None]
    rim = np.maximum.reduce([band(wt, y), band(wb, h - 1 - y), band(wl, x), band(wr, w - 1 - x)])

    t = np.clip(_smooth1d(rng, w, 36) - 0.35, 0, None) / 0.65
    blob = band(((0.15 + 2.2 * t ** 1.5) * ppmm * amt)[None, :], y)
    tb = np.clip(_smooth1d(rng, w, 30) - 0.6, 0, None) / 0.4
    blob = np.maximum(blob, band((1.2 * tb ** 1.5 * ppmm * amt)[None, :], h - 1 - y))

    rimc = np.array(prof.rim, np.float32) / 255
    blobc = np.array(prof.blob, np.float32) / 255
    a = (rim * (0.25 + 0.45 * amt))[..., None]
    arr = arr * (1 - a) + rimc * a
    b = (blob * 0.85)[..., None]
    return arr * (1 - b) + blobc * b


def _dust(arr, ppmm, rng, amt):
    h, w = arr.shape[:2]
    dark = Image.new("L", (w, h), 0); light = Image.new("L", (w, h), 0)
    dd, dl = ImageDraw.Draw(dark), ImageDraw.Draw(light)
    for _ in range(int(50 * amt) + 1):
        cx, cy = rng.random() * w, rng.random() * h
        r = (0.03 + 0.22 * rng.random() ** 3) * ppmm
        ex = r * (0.6 + 0.8 * rng.random())
        tgt = dl if rng.random() < 0.65 else dd
        tgt.ellipse([cx - ex, cy - r, cx + ex, cy + r], fill=int(150 + 105 * rng.random()))
    for _ in range(int(round(3 * amt * rng.random())) + (1 if amt > 0.6 else 0)):
        px, py, ang = rng.random() * w, rng.random() * h, rng.random() * math.tau
        L = (2 + 6 * rng.random()) * ppmm
        pts = []
        for _ in range(12):
            pts.append((px, py))
            ang += rng.normal(0, 0.35)
            px += math.cos(ang) * L / 12; py += math.sin(ang) * L / 12
        (dl if rng.random() < 0.5 else dd).line(pts, fill=200, width=max(1, int(0.03 * ppmm)))
    bl = ImageFilter.GaussianBlur(max(0.4, 0.03 * ppmm))
    D = np.asarray(dark.filter(bl), np.float32) / 255
    Lm = np.asarray(light.filter(bl), np.float32) / 255
    arr = arr * (1 - 0.75 * D)[..., None]
    return arr + (1 - arr) * (0.8 * Lm)[..., None]


def _grain(arr, ppmm, rng, amount):
    h, w = arr.shape[:2]
    n = rng.standard_normal((h, w), dtype=np.float32)
    sig = 0.025 * ppmm          # dimensione fisica del grano (mm → px)
    if sig >= 0.3:
        n = _gauss(n, sig); n /= (n.std() + 1e-6)
    L = _lum(arr)
    wgt = 0.35 + 2.6 * L * (1 - L)     # più grana nei mezzitoni
    amp = amount / 255.0
    arr = arr + (n * wgt * amp)[..., None]
    c = rng.standard_normal((h, w, 3), dtype=np.float32) * (amp * 0.18)
    return arr + c * wgt[..., None]


def develop(arr, s, ppmm, seed, fast=False):
    h, w = arr.shape[:2]
    prof = film_profile(s)
    R = lambda k: np.random.default_rng([seed & 0xFFFFFFFF, k])

    if not fast:
        soft = s["softness"]
        if soft > 0:                      # obiettivo in plastica: morbido, più ai bordi
            arr = _gauss(arr, 0.04 * ppmm * soft)
            blurred = _gauss(arr, 0.16 * ppmm * soft)
            m = (np.clip(_radial(h, w) ** 2 * 1.3, 0, 1) * soft)[..., None]
            arr = arr + (blurred - arr) * m
        if s["aberration"] > 0:
            arr = _chromatic(arr, 0.003 * s["aberration"])

    arr = apply_film(np.clip(arr, 0, 1), prof, s["strength"], s["lift"])

    if s["halation"] > 0 and not fast:
        hi = np.clip((_lum(arr) - 0.70) / 0.30, 0, 1) ** 2
        glow = _gauss(hi, 0.8 * ppmm)
        arr = arr + glow[..., None] * np.array(prof.halation, np.float32) * (0.45 * s["halation"])
        arr = np.where(arr > 0.9, 0.9 + 0.1 * np.tanh((arr - 0.9) / 0.1), arr)

    if s["uneven"] > 0:
        rng = R(1)
        f = _noise_field(rng, 5, 5, h, w)
        fc = np.stack([_noise_field(rng, 4, 4, h, w) for _ in range(3)], 2)
        arr = arr * (1 + 0.045 * s["uneven"] * f[..., None]) + 0.018 * s["uneven"] * fc

    if s["vignette"] > 0:
        t = np.clip((_radial(h, w) - 0.25) / 0.8, 0, 1)
        arr = arr * (1 - s["vignette"] * t * t * (3 - 2 * t))[..., None]

    if s["edges"] > 0:
        arr = _edge_chem(arr, ppmm, R(2), s["edges"], prof)
    if s["dust"] > 0:
        arr = _dust(arr, ppmm, R(3), s["dust"])
    if s["grain"] > 0:
        arr = _grain(arr, ppmm, R(4), s["grain"])
    return np.clip(arr, 0, 1).astype(np.float32)


# ═══════════════════════════════════════════════════════════════
# CORNICE
# ═══════════════════════════════════════════════════════════════

@dataclass
class FrameData:
    rgb: np.ndarray        # H×W×3
    alpha: np.ndarray      # H×W (angoli arrotondati)
    ap: tuple              # x, y, w, h della finestra
    ap_mask: np.ndarray    # h×w: 1 = immagine visibile
    ppmm: float
    ink: tuple


def _frame_from_file(path, out_long):
    im = Image.open(path).convert("RGBA")
    sc = out_long / max(im.size)
    im = im.resize((max(1, round(im.width * sc)), max(1, round(im.height * sc))), Image.LANCZOS)
    a = np.asarray(im, np.float32) / 255
    alpha, rgb = a[..., 3], a[..., :3].copy()
    H, W = alpha.shape
    mx, my = int(W * 0.04), int(H * 0.04)
    inner = alpha[my:H - my, mx:W - mx] < 0.5
    if not inner.any():
        raise ValueError("Il PNG non ha una finestra trasparente al centro.")
    ys, xs = np.where(inner)
    y0, y1 = ys.min() + my, ys.max() + my + 1
    x0, x1 = xs.min() + mx, xs.max() + mx + 1
    ap_mask = 1 - alpha[y0:y1, x0:x1]
    outer = alpha.copy(); outer[y0:y1, x0:x1] = 1.0
    lum = float(np.mean(_lum(rgb[outer > 0.5]))) if (outer > 0.5).any() else 1.0
    ink = (236, 234, 228) if lum < 0.45 else (38, 40, 58)
    return FrameData(rgb, outer, (x0, y0, x1 - x0, y1 - y0), ap_mask, W / 88.0, ink)


@lru_cache(maxsize=6)
def build_frame(style, fmt, shape, out_long, rounded, custom_color, custom_path):
    spec = FRAME_MAP[style]
    if spec["kind"] == "file":
        if not custom_path:
            raise ValueError("Nessun file PNG di cornice selezionato.")
        return _frame_from_file(custom_path, out_long)

    fw, fh, aw, ah, left, top = FORMATS[fmt]
    ppmm = out_long / max(fw, fh)
    W, H = int(round(fw * ppmm)), int(round(fh * ppmm))
    yy = np.linspace(0, 1, H, dtype=np.float32)[:, None]
    xx = np.linspace(0, 1, W, dtype=np.float32)[None, :]
    rng = np.random.default_rng(1234)

    kind = spec["kind"]
    if kind == "gradient":
        stops = np.array(spec["stops"], np.float32) / 255
        t = np.clip(yy * 0.8 + xx * 0.2, 0, 1)
        pos = np.linspace(0, 1, len(stops))
        rgb = np.stack([np.interp(t, pos, stops[:, c]) for c in range(3)], 2).astype(np.float32)
    else:
        col = custom_color if (spec.get("custom") and custom_color) else spec["c"]
        c = np.array(col, np.float32) / 255
        if kind == "metal":
            band = 0.5 + 0.5 * np.sin((xx * 0.9 + yy * 0.6) * math.pi * 2.2 + 0.7)
            br = rng.standard_normal((H, W)).astype(np.float32)
            br = _box1d(_box1d(br, max(2, int(1.5 * ppmm)), 1), 1, 0)
            br /= br.std() + 1e-6
            shade = 0.70 + 0.42 * band + 0.035 * br
            rgb = c * shade[..., None] + (0.22 * band ** 10)[..., None]
        else:
            rgb = np.broadcast_to(c, (H, W, 3)).astype(np.float32).copy()

    # texture carta/plastica + luce radente
    n = _gauss(rng.standard_normal((H, W)).astype(np.float32), max(0.35, 0.03 * ppmm))
    n /= n.std() + 1e-6
    f = _noise_field(rng, 8, 7, H, W)
    rgb = rgb + (0.008 * n + 0.006 * f)[..., None]
    rgb = rgb * (1.0 + 0.025 * (0.5 - (xx * 0.6 + yy * 0.4)))[..., None]
    rgb = np.clip(rgb, 0, 1).astype(np.float32)

    ax, ay = int(round(left * ppmm)), int(round(top * ppmm))
    aw_px, ah_px = int(round(aw * ppmm)), int(round(ah * ppmm))
    py = np.arange(ah_px, dtype=np.float32)[:, None] + 0.5
    px = np.arange(aw_px, dtype=np.float32)[None, :] + 0.5
    if shape == "Rotonda":
        d = np.sqrt((px - aw_px / 2) ** 2 + (py - ah_px / 2) ** 2) - min(aw_px, ah_px) * 0.46
    else:
        d = _sdf_round_rect(px, py, aw_px / 2, ah_px / 2, aw_px / 2, ah_px / 2, 0.35 * ppmm)
    ap_mask = np.clip(0.5 - d, 0, 1).astype(np.float32)

    if rounded:
        X = np.arange(W, dtype=np.float32)[None, :] + 0.5
        Y = np.arange(H, dtype=np.float32)[:, None] + 0.5
        alpha = np.clip(0.5 - _sdf_round_rect(X, Y, W / 2, H / 2, W / 2, H / 2, 1.0 * ppmm), 0, 1)
    else:
        alpha = np.ones((H, W), np.float32)

    lum = float(np.mean(_lum(rgb)))
    ink = (236, 234, 228) if lum < 0.45 else (38, 40, 58)
    return FrameData(rgb, alpha.astype(np.float32), (ax, ay, aw_px, ah_px), ap_mask, ppmm, ink)


def frame_args(s, out_long):
    return (s["style"], s["format"], s["shape"], int(out_long), s["rounded"],
            s.get("custom_color"), s.get("custom_frame"))


def _lip_shadow(cover, ppmm):
    """Il bordo della cornice è in rilievo sull'immagine: ombra in alto/sinistra."""
    p = int(1.2 * ppmm) + 4
    c = np.pad(cover, p, mode="constant", constant_values=1.0)
    d = max(1, int(round(0.2 * ppmm)))
    sh = np.ones_like(c); sh[d:, d:] = c[:-d, :-d]
    sh = _gauss(sh, 0.28 * ppmm)
    amb = _gauss(c, 0.12 * ppmm)
    sl = (slice(p, -p), slice(p, -p))
    return np.clip(0.40 * sh[sl] + 0.25 * amb[sl], 0, 0.6)


# ═══════════════════════════════════════════════════════════════
# DIDASCALIA
# ═══════════════════════════════════════════════════════════════

def caption_fonts():
    cands = []
    if sys.platform == "win32":
        dirs = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts")]
        names = [("Segoe Print (a mano)", "segoepr.ttf"), ("Segoe Script", "segoesc.ttf"),
                 ("Ink Free (pennarello)", "Inkfree.ttf"), ("Comic Sans", "comic.ttf"),
                 ("Segoe UI", "segoeui.ttf"), ("Georgia", "georgia.ttf")]
        for label, fn in names:
            for d in dirs:
                cands.append((label, os.path.join(d, fn)))
    elif sys.platform == "darwin":
        cands = [("Bradley Hand", "/System/Library/Fonts/Supplemental/Bradley Hand Bold.ttf"),
                 ("Noteworthy", "/System/Library/Fonts/Noteworthy.ttc"),
                 ("Marker Felt", "/System/Library/Fonts/MarkerFelt.ttc"),
                 ("Chalkboard", "/System/Library/Fonts/Supplemental/Chalkboard.ttc"),
                 ("Helvetica", "/System/Library/Fonts/Helvetica.ttc")]
    else:
        cands = [("DejaVu Sans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                 ("Liberation Serif", "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf"),
                 ("Liberation Sans", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf")]
    out, seen = [], set()
    for label, p in cands:
        if label not in seen and os.path.exists(p):
            out.append((label, p)); seen.add(label)
    return out


def _load_font(path, size):
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _caption(canvas, fd, text, font_path, seed):
    H, W = canvas.shape[:2]
    top = fd.ap[1] + fd.ap[3]
    area_h = H - top
    if area_h < 12:
        return
    size = max(10, int(area_h * 0.26))
    layer = Image.new("L", (W, area_h), 0)
    d = ImageDraw.Draw(layer)
    font = _load_font(font_path, size)
    bb = d.textbbox((0, 0), text, font=font)
    tw = bb[2] - bb[0]
    if tw > W * 0.86:
        size = max(8, int(size * W * 0.86 / tw))
        font = _load_font(font_path, size)
        bb = d.textbbox((0, 0), text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    cy = area_h * 0.44
    d.text(((W - tw) / 2 - bb[0], cy - th / 2 - bb[1]), text, fill=255, font=font)
    ang = random.Random(seed).uniform(-1.6, 1.0)
    layer = layer.rotate(ang, resample=Image.BICUBIC, center=(W / 2, cy))
    layer = layer.filter(ImageFilter.GaussianBlur(max(0.3, 0.02 * fd.ppmm)))
    a = (np.asarray(layer, np.float32) / 255 * 0.9)[..., None]
    ink = np.array(fd.ink, np.float32) / 255
    canvas[top:] = canvas[top:] * (1 - a) + ink * a


def _drop_shadow(rgba, ppmm):
    H, W = rgba.shape[:2]
    m = int(4 * ppmm)
    dx, dy = int(0.6 * ppmm), int(1.0 * ppmm)
    A = np.zeros((H + 2 * m, W + 2 * m), np.float32)
    A[m + dy:m + dy + H, m + dx:m + dx + W] = rgba[..., 3]
    A = _gauss(A, 1.2 * ppmm) * 0.45
    out = np.zeros((H + 2 * m, W + 2 * m, 4), np.float32)
    out[m:m + H, m:m + W] = rgba
    af = out[..., 3]
    a = af + A * (1 - af)
    out[..., :3] = out[..., :3] * (af / np.maximum(a, 1e-6))[..., None]
    out[..., 3] = a
    return out, m


# ═══════════════════════════════════════════════════════════════
# INQUADRATURA (zoom / pan sempre dentro la finestra)
# ═══════════════════════════════════════════════════════════════

@dataclass
class View:
    zoom: float = 1.0
    cx: float = 0.5      # centro normalizzato nella sorgente
    cy: float = 0.5
    rot: int = 0         # 0 / 90 / 180 / 270
    seed: int = 0


def base_window(W, H, aspect):
    return (H * aspect, H) if W / H > aspect else (W, W / aspect)


def clamp_view(W, H, aspect, v):
    v.zoom = min(max(v.zoom, 1.0), MAX_ZOOM)
    bw, bh = base_window(W, H, aspect)
    hx, hy = bw / v.zoom / 2 / W, bh / v.zoom / 2 / H
    v.cx = min(max(v.cx, hx), 1 - hx)
    v.cy = min(max(v.cy, hy), 1 - hy)
    return v


def crop_box(size, aspect, v):
    W, H = size
    clamp_view(W, H, aspect, v)
    bw, bh = base_window(W, H, aspect)
    cw, ch = bw / v.zoom, bh / v.zoom
    x0, y0 = v.cx * W - cw / 2, v.cy * H - ch / 2
    return (x0, y0, x0 + cw, y0 + ch)


def oriented(img, rot):
    return {0: img, 90: img.transpose(Image.Transpose.ROTATE_270),
            180: img.transpose(Image.Transpose.ROTATE_180),
            270: img.transpose(Image.Transpose.ROTATE_90)}[rot % 360]


def load_image(path):
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    return im.convert("RGB")


def render_polaroid(src, s, view, out_long=2400, fast=False):
    """src: PIL RGB già ruotata. Ritorna (PIL RGBA, box finestra nell'output)."""
    fd = build_frame(*frame_args(s, out_long))
    ax, ay, aw, ah = fd.ap
    box = crop_box(src.size, aw / ah, view)
    if fast:
        img = src.resize((aw, ah), Image.BILINEAR, box=box)
    else:
        img = src.resize((aw, ah), Image.LANCZOS, box=box, reducing_gap=3.0)
    arr = np.asarray(img, np.float32) / 255
    arr = develop(arr, s, fd.ppmm, view.seed, fast)
    if not fast:
        arr = arr * (1 - _lip_shadow(1 - fd.ap_mask, fd.ppmm))[..., None]

    canvas = fd.rgb.copy()
    m = fd.ap_mask[..., None]
    reg = canvas[ay:ay + ah, ax:ax + aw]
    canvas[ay:ay + ah, ax:ax + aw] = arr * m + reg * (1 - m)
    if s["text"].strip():
        _caption(canvas, fd, s["text"].strip(), s.get("font_path"), view.seed)
    out = np.dstack([canvas, fd.alpha])
    off = 0
    if s["shadow"]:
        out, off = _drop_shadow(out, fd.ppmm)
    img = Image.fromarray((np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8))
    return img, (ax + off, ay + off, aw, ah)


def save_image(img, path):
    ext = Path(path).suffix.lower()
    if ext in (".jpg", ".jpeg"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        bg.save(path, "JPEG", quality=96, subsampling=0)
    elif ext in (".tif", ".tiff"):
        img.save(path, "TIFF", compression="tiff_lzw")
    else:
        img.save(path, "PNG")


# ═══════════════════════════════════════════════════════════════
# WORKER ESPORTAZIONE
# ═══════════════════════════════════════════════════════════════

class ExportWorker(QThread):
    progress = pyqtSignal(str, int)
    done = pyqtSignal(int, list)

    def __init__(self, jobs, settings, out_long):
        super().__init__()
        self.jobs, self.s, self.out_long = jobs, settings, out_long

    def run(self):
        errs, n = [], len(self.jobs)
        for i, (path, view, out) in enumerate(self.jobs):
            self.progress.emit(f"[{i+1}/{n}] {Path(path).name}…", int(i / n * 100))
            try:
                src = oriented(load_image(path), view.rot)
                img, _ = render_polaroid(src, self.s, view, self.out_long)
                save_image(img, out)
            except Exception as e:
                errs.append(f"{Path(path).name}: {e}")
        self.progress.emit("Esportazione completata", 100)
        self.done.emit(n, errs)


# ═══════════════════════════════════════════════════════════════
# GUI
# ═══════════════════════════════════════════════════════════════

PREVIEW_FULL = 1100
PREVIEW_FAST = 640
IMG_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp")


def pil2pm(pil):
    i = pil.convert("RGBA")
    return QPixmap.fromImage(QImage(i.tobytes("raw", "RGBA"), i.width, i.height,
                                    i.width * 4, QImage.Format.Format_RGBA8888).copy())


def _drop_files(e):
    return [u.toLocalFile() for u in e.mimeData().urls()
            if u.toLocalFile().lower().endswith(IMG_EXTS)]


class OrigView(QWidget):
    """Originale con il riquadro che finisce nella Polaroid."""
    dropped = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self._pm = None; self._box = None
        self.setAcceptDrops(True)
        self.setMinimumSize(200, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_data(self, pm, box_norm):
        self._pm, self._box = pm, box_norm; self.update()

    def clear(self):
        self._pm = None; self.update()

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor(30, 30, 30))
        if not self._pm:
            p.setPen(QColor(120, 120, 120))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "📷\nTrascina immagini qui\no usa 📂 Apri")
            return
        pw, ph = self._pm.width(), self._pm.height()
        s = min(self.width() / pw, self.height() / ph) * 0.96
        r = QRectF((self.width() - pw * s) / 2, (self.height() - ph * s) / 2, pw * s, ph * s)
        p.drawPixmap(r, self._pm, QRectF(0, 0, pw, ph))
        if self._box:
            x0, y0, x1, y1 = self._box
            b = QRectF(r.x() + x0 * r.width(), r.y() + y0 * r.height(),
                       (x1 - x0) * r.width(), (y1 - y0) * r.height())
            dim = QColor(0, 0, 0, 120)
            p.fillRect(QRectF(r.x(), r.y(), r.width(), b.y() - r.y()), dim)
            p.fillRect(QRectF(r.x(), b.bottom(), r.width(), r.bottom() - b.bottom()), dim)
            p.fillRect(QRectF(r.x(), b.y(), b.x() - r.x(), b.height()), dim)
            p.fillRect(QRectF(b.right(), b.y(), r.right() - b.right(), b.height()), dim)
            p.setPen(QPen(QColor(196, 196, 22), 1.5)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(b)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls(): e.acceptProposedAction()

    def dropEvent(self, e):
        f = _drop_files(e)
        if f: self.dropped.emit(f)


class PolaroidView(QWidget):
    """Anteprima interattiva: rotella = zoom, trascina = sposta, doppio clic = reimposta."""
    panned = pyqtSignal(float, float)
    zoomed = pyqtSignal(float, float, float)
    reset = pyqtSignal()
    interact = pyqtSignal(bool)
    dropped = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self._pm = None; self._ap = None; self._last = None
        self.setAcceptDrops(True); self.setMouseTracking(True)
        self.setMinimumSize(260, 260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_result(self, pm, ap):
        self._pm, self._ap = pm, ap; self.update()

    def clear(self):
        self._pm = None; self.update()

    def _disp(self):
        pw, ph = self._pm.width(), self._pm.height()
        s = min(self.width() / pw, (self.height() - 22) / ph) * 0.97
        return s, (self.width() - pw * s) / 2, (self.height() - 22 - ph * s) / 2

    def _ap_rect(self):
        s, ox, oy = self._disp(); ax, ay, aw, ah = self._ap
        return QRectF(ox + ax * s, oy + ay * s, aw * s, ah * s)

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor(52, 52, 50))
        if not self._pm:
            p.setPen(QColor(150, 150, 150))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "La Polaroid\napparirà qui")
            return
        s, ox, oy = self._disp()
        p.drawPixmap(QRectF(ox, oy, self._pm.width() * s, self._pm.height() * s),
                     self._pm, QRectF(0, 0, self._pm.width(), self._pm.height()))
        p.setPen(QColor(150, 150, 150))
        p.drawText(QRectF(0, self.height() - 20, self.width(), 18), Qt.AlignmentFlag.AlignCenter,
                   "Rotella: zoom  ·  Trascina: sposta  ·  Doppio clic: reimposta")

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._pm and self._ap:
            self._last = e.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.interact.emit(True)

    def mouseMoveEvent(self, e):
        if self._last is not None:
            d = e.position() - self._last; self._last = e.position()
            r = self._ap_rect()
            self.panned.emit(d.x() / r.width(), d.y() / r.height())
        elif self._pm and self._ap:
            inside = self._ap_rect().contains(e.position())
            self.setCursor(Qt.CursorShape.OpenHandCursor if inside else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, e):
        if self._last is not None:
            self._last = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self.interact.emit(False)

    def mouseDoubleClickEvent(self, e):
        if self._pm: self.reset.emit()

    def wheelEvent(self, e):
        if not (self._pm and self._ap): return
        r = self._ap_rect(); pos = e.position()
        u = min(max((pos.x() - r.x()) / r.width(), 0), 1)
        v = min(max((pos.y() - r.y()) / r.height(), 0), 1)
        steps = e.angleDelta().y() / 120.0
        if steps: self.zoomed.emit(u, v, 1.12 ** steps)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls(): e.acceptProposedAction()

    def dropEvent(self, e):
        f = _drop_files(e)
        if f: self.dropped.emit(f)


class SldRow(QWidget):
    changed = pyqtSignal(int)

    def __init__(self, label, lo, hi, default, sfx="%"):
        super().__init__(); self.sfx = sfx
        lay = QHBoxLayout(self); lay.setContentsMargins(0, 1, 0, 1)
        lb = QLabel(label); lb.setFixedWidth(108); lay.addWidget(lb)
        self.sl = QSlider(Qt.Orientation.Horizontal)
        self.sl.setRange(lo, hi); self.sl.setValue(default)
        lay.addWidget(self.sl, 1)
        self.vl = QLabel(); self.vl.setFixedWidth(52)
        self.vl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._sh(default); lay.addWidget(self.vl)
        self.sl.valueChanged.connect(lambda v: (self._sh(v), self.changed.emit(v)))

    def _sh(self, v): self.vl.setText(f"{v}{self.sfx}")
    def value(self): return self.sl.value()

    def set_silent(self, v):
        self.sl.blockSignals(True); self.sl.setValue(v); self._sh(v); self.sl.blockSignals(False)


SS = """
QMainWindow{background:#1e1e1e}
QWidget{color:#ddd;font-size:12px}
QScrollArea{border:none;background:#1e1e1e}
#panel{background:#1e1e1e}
QGroupBox{border:1px solid #444;border-radius:6px;margin-top:12px;padding:12px 8px 8px;font-weight:bold;color:#aaa}
QGroupBox::title{subcontrol-origin:margin;left:10px;padding:0 5px}
QCheckBox::indicator{width:15px;height:15px;border:1px solid #666;border-radius:3px;background:#2b2b2b}
QCheckBox::indicator:checked{background:#3E7637;border-color:#3E7637}
QSlider::groove:horizontal{background:#3a3a3a;height:5px;border-radius:2px}
QSlider::handle:horizontal{background:#4CAF50;width:14px;height:14px;margin:-5px 0;border-radius:7px}
QSlider::sub-page:horizontal{background:#3E7637;border-radius:2px}
QLineEdit{background:#2b2b2b;border:1px solid #555;border-radius:4px;padding:5px 8px;color:#ddd}
QLineEdit:focus{border-color:#4CAF50}
QComboBox{background:#2b2b2b;border:1px solid #555;border-radius:4px;padding:4px 8px;color:#ddd}
QComboBox QAbstractItemView{background:#2b2b2b;color:#ddd;selection-background-color:#3E7637}
QProgressBar{background:#2b2b2b;border:none;border-radius:3px;height:8px}
QProgressBar::chunk{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #1A5738,stop:.5 #3E7637,stop:1 #C4C416);border-radius:3px}
QStatusBar{background:#151515;color:#777}
QMenuBar{background:#1a1a1a;color:#bbb}
QMenuBar::item:selected{background:#3E7637}
QMenu{background:#2b2b2b;color:#ddd;border:1px solid #444}
QMenu::item:selected{background:#3E7637}
QListWidget{background:#2b2b2b;border:1px solid #444;border-radius:4px;color:#ccc;font-size:11px}
QListWidget::item{padding:3px 6px}
QListWidget::item:selected{background:#3E7637;color:white}
"""
BG = ("QPushButton{background:#3E7637;color:white;border:none;border-radius:6px;"
      "padding:9px 14px;font-size:13px;font-weight:bold}"
      "QPushButton:hover{background:#4a8f42}QPushButton:pressed{background:#2d5a28}"
      "QPushButton:disabled{background:#333;color:#666}")
BF = ("QPushButton{background:#2b2b2b;color:#ccc;border:1px solid #555;"
      "border-radius:6px;padding:6px 10px;font-size:12px}"
      "QPushButton:hover{border-color:#4CAF50;background:#353535}"
      "QPushButton:disabled{color:#555;border-color:#3a3a3a}")
BR = ("QPushButton{background:#6b2b2b;color:#ccc;border:1px solid #855;border-radius:6px;"
      "padding:6px 10px;font-size:11px}QPushButton:hover{background:#7b3535;border-color:#a66}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Polaroid Simulator v6.1 — Reclaimed Series")
        self.setMinimumSize(1100, 720)
        self.resize(1400, 860)
        self.setStyleSheet(SS)
        self.items = []            # dict(path, view, proxy, rot_cache)
        self.cur = -1
        self.wk = None
        self.custom_color = None
        self.custom_frame = None
        self.film_colors = None
        self._prev_style = None
        self._dragging = False
        self._want_fast = False
        self.rt = QTimer(self); self.rt.setSingleShot(True); self.rt.timeout.connect(self._render_preview)
        self.ft = QTimer(self); self.ft.setSingleShot(True); self.ft.timeout.connect(self._settle)
        self._menu(); self._ui()
        self.statusBar().showMessage("Trascina immagini o premi 📂 Apri")

    # ── menu ──
    def _menu(self):
        mb = self.menuBar()
        fm = mb.addMenu("&File")
        for txt, sc, fn in (("&Apri…", "Ctrl+O", self.open_files),
                            ("&Salva questa…", "Ctrl+S", self.save_current),
                            ("&Esporta tutte…", "Ctrl+Shift+S", self.export_all)):
            a = QAction(txt, self); a.setShortcut(sc); a.triggered.connect(fn); fm.addAction(a)
        fm.addSeparator()
        a = QAction("&Esci", self); a.setShortcut("Ctrl+Q"); a.triggered.connect(self.close); fm.addAction(a)
        vm = mb.addMenu("&Inquadratura")
        for txt, sc, fn in (("Zoom +", "Ctrl++", lambda: self._zoom_center(1.15)),
                            ("Zoom −", "Ctrl+-", lambda: self._zoom_center(1 / 1.15)),
                            ("Reimposta", "Ctrl+0", self.reset_view),
                            ("Ruota a destra", "Ctrl+R", lambda: self.rotate(90)),
                            ("Ruota a sinistra", "Ctrl+Shift+R", lambda: self.rotate(-90))):
            a = QAction(txt, self); a.setShortcut(sc); a.triggered.connect(fn); vm.addAction(a)

    # ── UI ──
    def _ui(self):
        cw = QWidget(); self.setCentralWidget(cw)
        root = QVBoxLayout(cw); root.setContentsMargins(6, 6, 6, 4); root.setSpacing(4)

        bar = QLabel(); bar.setFixedHeight(26)
        pm = QPixmap(1200, 26); p = QPainter(pm); bw = 1200 / len(PAL)
        for i, c in enumerate(PAL): p.fillRect(int(i * bw), 0, int(bw) + 2, 26, QColor(*c))
        p.setPen(QColor(255, 255, 255, 210)); f = p.font(); f.setPointSize(7); f.setBold(True); p.setFont(f)
        p.drawText(8, 18, "POLAROID SIMULATOR v6.1 · RECLAIMED · DUOCHROME · CLASSICHE"); p.end()
        bar.setPixmap(pm); bar.setScaledContents(True); root.addWidget(bar)

        sp = QSplitter(Qt.Orientation.Horizontal)

        # ── pannello controlli (scorrevole) ──
        inner = QWidget(); inner.setObjectName("panel"); cl = QVBoxLayout(inner); cl.setContentsMargins(4, 4, 8, 4); cl.setSpacing(4)

        g = QGroupBox("📂 File"); gl = QVBoxLayout(g)
        rw = QHBoxLayout()
        b = QPushButton("📂 Apri"); b.setStyleSheet(BF); b.clicked.connect(self.open_files); rw.addWidget(b)
        b = QPushButton("✕"); b.setStyleSheet(BR); b.setFixedWidth(32); b.clicked.connect(self.clear_list); rw.addWidget(b)
        gl.addLayout(rw)
        self.flist = QListWidget(); self.flist.setMaximumHeight(96)
        self.flist.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.flist.currentRowChanged.connect(self._on_select); gl.addWidget(self.flist)
        cl.addWidget(g)

        rw = QHBoxLayout()
        self.bsave = QPushButton("💾 Salva"); self.bsave.setStyleSheet(BG); self.bsave.clicked.connect(self.save_current)
        self.ball = QPushButton("📦 Esporta tutte"); self.ball.setStyleSheet(BG); self.ball.clicked.connect(self.export_all)
        rw.addWidget(self.bsave); rw.addWidget(self.ball); cl.addLayout(rw)

        # pellicola
        g = QGroupBox("🎞️ Pellicola"); gl = QVBoxLayout(g)
        self.cfilm = QComboBox(); last = None
        for grp, name, _ in FILM_GROUPS:
            if last and grp != last: self.cfilm.insertSeparator(self.cfilm.count())
            self.cfilm.addItem(f"{grp} · {name}"); last = grp
        gl.addWidget(self.cfilm)
        rw = QHBoxLayout()
        self.bfs = QPushButton("Ombre…"); self.bfh = QPushButton("Luci…")
        for b, i in ((self.bfs, 0), (self.bfh, 1)):
            b.setStyleSheet(BF); b.clicked.connect(lambda _=False, i=i: self.pick_film_color(i)); rw.addWidget(b)
        self.film_row = QWidget(); self.film_row.setLayout(rw); self.film_row.setVisible(False)
        gl.addWidget(self.film_row)
        self.si = SldRow("Intensità", 0, 100, 95); gl.addWidget(self.si)
        self.slift = SldRow("Neri sollevati", 0, 40, 10, ""); gl.addWidget(self.slift)
        self.sg = SldRow("Grana", 0, 30, 9, ""); gl.addWidget(self.sg)
        self.sv = SldRow("Vignettatura", 0, 80, 28); gl.addWidget(self.sv)
        cl.addWidget(g)

        # ottica
        g = QGroupBox("🔍 Ottica"); gl = QVBoxLayout(g)
        self.ssoft = SldRow("Morbidezza", 0, 100, 40); gl.addWidget(self.ssoft)
        self.sca = SldRow("Aberrazione", 0, 100, 30); gl.addWidget(self.sca)
        self.shal = SldRow("Alone luci", 0, 100, 35); gl.addWidget(self.shal)
        cl.addWidget(g)

        # imperfezioni
        g = QGroupBox("🧪 Chimica e difetti"); gl = QVBoxLayout(g)
        self.sun = SldRow("Sviluppo irreg.", 0, 100, 40); gl.addWidget(self.sun)
        self.sed = SldRow("Bordi chimici", 0, 100, 35); gl.addWidget(self.sed)
        self.sdu = SldRow("Polvere", 0, 100, 15); gl.addWidget(self.sdu)
        b = QPushButton("🎲 Nuova variante dei difetti"); b.setStyleSheet(BF); b.clicked.connect(self.new_variant)
        gl.addWidget(b); cl.addWidget(g)

        # cornice
        g = QGroupBox("🖼️ Cornice"); gl = QVBoxLayout(g)
        rl = QHBoxLayout(); rl.addWidget(QLabel("Formato"))
        self.cfmt = QComboBox(); self.cfmt.addItems(list(FORMATS)); rl.addWidget(self.cfmt, 1); gl.addLayout(rl)
        rl = QHBoxLayout(); rl.addWidget(QLabel("Stile"))
        self.cstyle = QComboBox(); last = None
        for grp, name, _ in FRAME_STYLES:
            if last and grp != last: self.cstyle.insertSeparator(self.cstyle.count())
            self.cstyle.addItem(f"{grp} · {name}"); last = grp
        rl.addWidget(self.cstyle, 1); gl.addLayout(rl)
        rl = QHBoxLayout(); rl.addWidget(QLabel("Finestra"))
        self.cshape = QComboBox(); self.cshape.addItems(SHAPES); rl.addWidget(self.cshape, 1)
        self.bcol = QPushButton("🎨"); self.bcol.setStyleSheet(BF); self.bcol.setFixedWidth(36)
        self.bcol.setToolTip("Colore personalizzato"); self.bcol.clicked.connect(self.pick_color)
        rl.addWidget(self.bcol); gl.addLayout(rl)
        self.crnd = QCheckBox("Angoli arrotondati"); self.crnd.setChecked(True); gl.addWidget(self.crnd)
        self.csh = QCheckBox("Ombra (foto appoggiata)"); gl.addWidget(self.csh)
        cl.addWidget(g)

        # didascalia
        g = QGroupBox("✍️ Didascalia"); gl = QVBoxLayout(g)
        self.tc = QLineEdit(); self.tc.setPlaceholderText("Scrivi sul bordo…"); gl.addWidget(self.tc)
        self.cfont = QComboBox()
        for label, path in caption_fonts(): self.cfont.addItem(label, path)
        self.cfont.addItem("Carica font…", "__load__"); gl.addWidget(self.cfont)
        cl.addWidget(g)

        # inquadratura
        g = QGroupBox("🔲 Inquadratura"); gl = QVBoxLayout(g)
        self.sz = SldRow("Zoom", 100, int(MAX_ZOOM * 100), 100); gl.addWidget(self.sz)
        rw = QHBoxLayout()
        for txt, fn in (("⟲", lambda: self.rotate(-90)), ("⟳", lambda: self.rotate(90)),
                        ("Reimposta", self.reset_view)):
            b = QPushButton(txt); b.setStyleSheet(BF); b.clicked.connect(fn); rw.addWidget(b)
        gl.addLayout(rw); cl.addWidget(g)

        # output
        g = QGroupBox("💾 Output"); gl = QVBoxLayout(g)
        self.sm = SldRow("Lato lungo", 1000, 6000, 2400, "px"); gl.addWidget(self.sm)
        cl.addWidget(g)

        self.pb = QProgressBar(); self.pb.setRange(0, 100); self.pb.setTextVisible(False); cl.addWidget(self.pb)
        cl.addStretch()

        sc = QScrollArea(); sc.setWidget(inner); sc.setWidgetResizable(True)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sc.setMinimumWidth(318); sc.setMaximumWidth(360)
        sp.addWidget(sc)

        # originale
        lb = QWidget(); ll = QVBoxLayout(lb); ll.setContentsMargins(2, 0, 2, 0)
        h = QLabel("ORIGINALE · inquadratura"); h.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.setStyleSheet("color:#666;font-size:11px;font-weight:bold;letter-spacing:2px"); ll.addWidget(h)
        self.ov = OrigView(); self.ov.dropped.connect(self.add_files); ll.addWidget(self.ov, 1)
        sp.addWidget(lb)

        # polaroid
        rb = QWidget(); rl2 = QVBoxLayout(rb); rl2.setContentsMargins(2, 0, 2, 0)
        h = QLabel("POLAROID"); h.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.setStyleSheet("color:#3E7637;font-size:11px;font-weight:bold;letter-spacing:2px"); rl2.addWidget(h)
        self.pv = PolaroidView()
        self.pv.dropped.connect(self.add_files)
        self.pv.panned.connect(self._on_pan); self.pv.zoomed.connect(self._on_zoom)
        self.pv.reset.connect(self.reset_view); self.pv.interact.connect(self._on_interact)
        rl2.addWidget(self.pv, 1); sp.addWidget(rb)

        sp.setStretchFactor(1, 2); sp.setStretchFactor(2, 3)
        sp.setSizes([330, 380, 620]); root.addWidget(sp, 1)

        # segnali → anteprima
        for w in (self.si, self.slift, self.sg, self.sv, self.ssoft, self.sca, self.shal,
                  self.sun, self.sed, self.sdu):
            w.changed.connect(lambda _=None: self._request())
        self.cfilm.currentTextChanged.connect(self._on_film)
        self.cfmt.currentTextChanged.connect(lambda _: self._request())
        self.cshape.currentTextChanged.connect(lambda _: self._request())
        self.cstyle.currentTextChanged.connect(self._on_style)
        self.crnd.toggled.connect(lambda _: self._request())
        self.csh.toggled.connect(lambda _: self._request())
        self.tc.textChanged.connect(lambda _: self._request())
        self.cfont.currentIndexChanged.connect(self._on_font)
        self.sz.changed.connect(self._on_zoom_slider)
        self._prev_style = self.cstyle.currentText()
        self._on_style(self.cstyle.currentText())
        self._busy(False)

    # ── impostazioni ──
    def _settings(self):
        return dict(
            film=self.cfilm.currentText(), strength=self.si.value() / 100,
            lift=self.slift.value() / 255, grain=self.sg.value(), vignette=self.sv.value() / 100,
            softness=self.ssoft.value() / 100, aberration=self.sca.value() / 100,
            halation=self.shal.value() / 100, uneven=self.sun.value() / 100,
            edges=self.sed.value() / 100, dust=self.sdu.value() / 100,
            format=self.cfmt.currentText(), style=self.cstyle.currentText(),
            shape=self.cshape.currentText(), rounded=self.crnd.isChecked(),
            shadow=self.csh.isChecked(), custom_color=self.custom_color,
            custom_frame=self.custom_frame, text=self.tc.text(),
            film_colors=tuple(self.film_colors) if self.film_colors else None,
            font_path=self.cfont.currentData() if self.cfont.currentData() != "__load__" else None)

    def _on_film(self, name):
        custom = name == CUSTOM_FILM
        self.film_row.setVisible(custom)
        if custom and not self.film_colors:
            self.film_colors = [(20, 30, 90), (250, 230, 200)]
            self._paint_film_buttons()
        prof = film_profile(self._settings())
        self.slift.set_silent(prof.default_lift)
        if self.cstyle.currentText() in ("Classica · Bianca", "Classica · Nera") and not custom:
            self.cstyle.setCurrentText(prof.frame)
        self._request()

    def pick_film_color(self, i):
        cur = self.film_colors[i]
        c = QColorDialog.getColor(QColor(*cur), self, "Colore " + ("ombre" if i == 0 else "luci"))
        if c.isValid():
            self.film_colors[i] = (c.red(), c.green(), c.blue())
            self._paint_film_buttons(); self._request()

    def _paint_film_buttons(self):
        for b, col in ((self.bfs, self.film_colors[0]), (self.bfh, self.film_colors[1])):
            fg = "#111" if sum(col) > 380 else "#eee"
            b.setStyleSheet(BF + f"QPushButton{{background:rgb{tuple(col)};color:{fg}}}")

    def _on_style(self, name):
        spec = FRAME_MAP.get(name)
        if spec is None: return
        if spec["kind"] == "file" and not self.custom_frame:
            p, _ = QFileDialog.getOpenFileName(self, "Cornice PNG (finestra trasparente)", "", "PNG (*.png)")
            if not p:
                self.cstyle.blockSignals(True); self.cstyle.setCurrentText(self._prev_style)
                self.cstyle.blockSignals(False); return
            self.custom_frame = p
        if spec.get("custom") and not self.custom_color:
            self.pick_color(); return
        self._prev_style = name
        self.cshape.setEnabled(spec["kind"] != "file")
        self.cfmt.setEnabled(spec["kind"] != "file")
        self._request()

    def pick_color(self):
        c = QColorDialog.getColor(QColor(*(self.custom_color or (244, 243, 238))), self, "Colore cornice")
        if c.isValid():
            self.custom_color = (c.red(), c.green(), c.blue())
            key = next(k for k, v in FRAME_MAP.items() if v.get("custom"))
            if self.cstyle.currentText() != key:
                self.cstyle.setCurrentText(key)
            self._prev_style = key
            self._request()
        elif not self.custom_color:
            self.cstyle.blockSignals(True); self.cstyle.setCurrentText(self._prev_style or "Classica · Bianca")
            self.cstyle.blockSignals(False)

    def _on_font(self, _):
        if self.cfont.currentData() == "__load__":
            p, _ = QFileDialog.getOpenFileName(self, "Font", "", "Font (*.ttf *.otf *.ttc)")
            self.cfont.blockSignals(True)
            if p:
                self.cfont.insertItem(self.cfont.count() - 1, Path(p).stem, p)
                self.cfont.setCurrentIndex(self.cfont.count() - 2)
            else:
                self.cfont.setCurrentIndex(0)
            self.cfont.blockSignals(False)
        self._request()

    # ── file ──
    def open_files(self):
        ps, _ = QFileDialog.getOpenFileNames(self, "Apri immagini", "",
            "Immagini (*.jpg *.jpeg *.png *.bmp *.tiff *.tif *.webp);;Tutti (*)")
        if ps: self.add_files(ps)

    def add_files(self, paths):
        known = {it["path"] for it in self.items}
        for p in paths:
            if p in known: continue
            self.items.append(dict(path=p, view=View(seed=zlib.crc32(Path(p).name.encode())),
                                   proxy=None, rc={}))
            self.flist.addItem(Path(p).name)
        if self.items and self.flist.currentRow() < 0:
            self.flist.setCurrentRow(0)
        self._busy(False)

    def clear_list(self):
        self.items.clear(); self.flist.clear(); self.cur = -1
        self.ov.clear(); self.pv.clear(); self.pb.setValue(0); self._busy(False)

    def _item(self):
        return self.items[self.cur] if 0 <= self.cur < len(self.items) else None

    def _src(self, it):
        """Proxy ruotato (max 2048 px) per l'anteprima; il file intero si usa solo in export."""
        if it["proxy"] is None:
            im = load_image(it["path"]); im.thumbnail((2048, 2048), Image.LANCZOS)
            it["proxy"] = im
        rot = it["view"].rot
        if rot not in it["rc"]:
            o = oriented(it["proxy"], rot)
            it["rc"][rot] = (o, pil2pm(o))
        return it["rc"][rot]

    def _on_select(self, row):
        self.cur = row
        it = self._item()
        if not it: return
        try:
            src, _ = self._src(it)
            self.statusBar().showMessage(f"{Path(it['path']).name}")
            self._sync_zoom()
            self._request()
        except Exception as e:
            self.statusBar().showMessage(f"Errore: {e}")

    # ── inquadratura ──
    def _aspect(self):
        fd = build_frame(*frame_args(self._settings(), PREVIEW_FULL))
        return fd.ap[2] / fd.ap[3]

    def _on_pan(self, fx, fy):
        it = self._item()
        if not it: return
        src, _ = self._src(it); W, H = src.size; asp = self._aspect(); v = it["view"]
        bw, bh = base_window(W, H, asp)
        v.cx -= fx * bw / v.zoom / W
        v.cy -= fy * bh / v.zoom / H
        clamp_view(W, H, asp, v)
        self._request(fast=True)

    def _on_zoom(self, u, vv, factor):
        it = self._item()
        if not it: return
        src, _ = self._src(it); W, H = src.size; asp = self._aspect(); v = it["view"]
        bw, bh = base_window(W, H, asp)
        px = v.cx * W + (u - 0.5) * bw / v.zoom
        py = v.cy * H + (vv - 0.5) * bh / v.zoom
        v.zoom = min(max(v.zoom * factor, 1.0), MAX_ZOOM)
        v.cx = (px - (u - 0.5) * bw / v.zoom) / W
        v.cy = (py - (vv - 0.5) * bh / v.zoom) / H
        clamp_view(W, H, asp, v)
        self._sync_zoom()
        self._request(fast=True)

    def _zoom_center(self, f):
        self._on_zoom(0.5, 0.5, f)

    def _on_zoom_slider(self, val):
        it = self._item()
        if it: self._on_zoom(0.5, 0.5, (val / 100) / it["view"].zoom)

    def _sync_zoom(self):
        it = self._item()
        if it: self.sz.set_silent(int(round(it["view"].zoom * 100)))

    def reset_view(self):
        it = self._item()
        if not it: return
        v = it["view"]; v.zoom, v.cx, v.cy = 1.0, 0.5, 0.5
        self._sync_zoom(); self._request()

    def rotate(self, deg):
        it = self._item()
        if not it: return
        v = it["view"]; v.rot = (v.rot + deg) % 360; v.cx = v.cy = 0.5
        self._request()

    def new_variant(self):
        it = self._item()
        if it:
            it["view"].seed = random.randrange(1 << 31); self._request()

    def _on_interact(self, on):
        self._dragging = on
        if not on: self._request()

    # ── anteprima ──
    def _request(self, fast=False):
        if fast:
            self._want_fast = True; self.rt.start(10); self.ft.start(320)
        else:
            self._want_fast = False; self.rt.start(40)

    def _settle(self):
        if self._dragging: self.ft.start(320)
        else: self._request()

    def _render_preview(self):
        it = self._item()
        if not it: return
        try:
            src, spm = self._src(it)
            s = self._settings()
            fast = self._want_fast
            img, ap = render_polaroid(src, s, it["view"], PREVIEW_FAST if fast else PREVIEW_FULL, fast)
            self.pv.set_result(pil2pm(img), ap)
            fd = build_frame(*frame_args(s, PREVIEW_FULL))
            x0, y0, x1, y1 = crop_box(src.size, fd.ap[2] / fd.ap[3], it["view"])
            W, H = src.size
            self.ov.set_data(spm, (x0 / W, y0 / H, x1 / W, y1 / H))
            if not fast:
                self.statusBar().showMessage(
                    f"{Path(it['path']).name}  ·  zoom {it['view'].zoom:.2f}×  ·  rot {it['view'].rot}°")
        except Exception as e:
            self.statusBar().showMessage(f"⚠️ {e}")

    # ── export ──
    def _busy(self, on):
        has = bool(self.items)
        self.bsave.setEnabled(has and not on)
        self.ball.setEnabled(len(self.items) > 1 and not on)

    def _start(self, jobs):
        self._busy(True); self.pb.setValue(0)
        self.wk = ExportWorker(jobs, self._settings(), self.sm.value())
        self.wk.progress.connect(lambda m, p: (self.pb.setValue(p), self.statusBar().showMessage(m)))
        self.wk.done.connect(self._done)
        self.wk.start()

    def save_current(self):
        it = self._item()
        if not it or (self.wk and self.wk.isRunning()): return
        df = Path(it["path"]).stem + "_polaroid.png"
        p, _ = QFileDialog.getSaveFileName(self, "Salva Polaroid", df,
            "PNG (*.png);;JPEG (*.jpg);;TIFF (*.tiff)")
        if p: self._start([(it["path"], replace(it["view"]), p)])

    def export_all(self):
        if not self.items or (self.wk and self.wk.isRunning()): return
        d = QFileDialog.getExistingDirectory(self, "Cartella di output")
        if not d: return
        jobs = [(it["path"], replace(it["view"]),
                 os.path.join(d, Path(it["path"]).stem + "_polaroid.png")) for it in self.items]
        self._start(jobs)

    def _done(self, n, errs):
        self._busy(False); self.pb.setValue(100)
        if errs:
            QMessageBox.warning(self, "Esportazione", f"{n - len(errs)}/{n} salvate.\n\n" + "\n".join(errs))
        else:
            self.statusBar().showMessage(f"✅ {n} Polaroid salvate")
            if n > 1: QMessageBox.information(self, "Esportazione", f"{n} Polaroid salvate.")


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    from PyQt6.QtGui import QPalette
    pal = QPalette()
    for role, col in ((QPalette.ColorRole.Window, "#1e1e1e"), (QPalette.ColorRole.Base, "#2b2b2b"),
                      (QPalette.ColorRole.WindowText, "#dddddd"), (QPalette.ColorRole.Text, "#dddddd"),
                      (QPalette.ColorRole.Button, "#2b2b2b"), (QPalette.ColorRole.ButtonText, "#dddddd"),
                      (QPalette.ColorRole.Highlight, "#3E7637"), (QPalette.ColorRole.HighlightedText, "#ffffff")):
        pal.setColor(role, QColor(col))
    app.setPalette(pal)
    w = MainWindow(); w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
