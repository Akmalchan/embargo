"""Render the login visuals procedurally, from our own fictional data. No photos, no downloads.

  python scripts/make_visuals.py  ->  web/static/img/v_*.jpg

  v_defense.jpg   a machined part, extruded from a vault drawing's front view, studio-lit
  v_chips.jpg     a chip die: standard-cell rows, routing, bond pads
  v_health.jpg    a patient chart with redacted identifiers and an ECG trace
  v_biotech.jpg   a 96-well plate in perspective
  v_gov.jpg       a stack of controlled government files with a CUI banner
"""
from __future__ import annotations

import csv
import math
import random
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from exportgate.fingerprint import content_image  # noqa: E402
from exportgate.ingest import load  # noqa: E402

OUT = ROOT / "web" / "static" / "img"
DATA = ROOT / "data"
W, H = 1600, 1000


# ------------------------------------------------------------------ helpers (float BGR images, 0..1)

def studio(w=W, h=H, top=(0.11, 0.09, 0.07), bottom=(0.025, 0.02, 0.015), glow=(0.35, 0.22, 0.10), gx=0.62, gy=0.38):
    y = np.linspace(0, 1, h)[:, None, None]
    img = np.array(top)[None, None, :] * (1 - y) + np.array(bottom)[None, None, :] * y
    img = np.repeat(img, w, axis=1)
    yy, xx = np.mgrid[0:h, 0:w]
    d = np.sqrt(((xx / w - gx) * 1.6) ** 2 + (yy / h - gy) ** 2)
    img += np.exp(-(d / 0.42) ** 2)[..., None] * np.array(glow)[None, None, :] * 0.55
    return img


def vignette(img, k=0.55):
    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    d = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
    return img * (1 - k * np.clip(d - 0.35, 0, 1)[..., None] ** 1.5)


def grain(img, s=0.012, seed=1):
    rng = np.random.default_rng(seed)
    return img + rng.normal(0, s, img.shape[:2])[..., None]


def save(img, name):
    out = np.clip(img, 0, 1) ** (1 / 1.05)
    cv2.imwrite(str(OUT / name), (out * 255).astype(np.uint8), [cv2.IMWRITE_JPEG_QUALITY, 90])
    print("  ", name)


def blur(a, k):
    k = int(k) | 1
    return cv2.GaussianBlur(a, (k, k), 0)


def put_text(img, text, org, scale, color, thick=1, font=cv2.FONT_HERSHEY_SIMPLEX):
    """putText only draws on 8-bit images: draw a mask, then blend the colour into the float image."""
    m = np.zeros(img.shape[:2], np.uint8)
    cv2.putText(m, text, org, font, scale, 255, thick, cv2.LINE_AA)
    a = (m.astype(np.float32) / 255.0)[..., None]
    img[:] = img * (1 - a) + np.array(color, np.float32)[None, None, :] * a


# ------------------------------------------------------------------ 1. machined part from a drawing

def part_mask(pdf: Path):
    """Front-view silhouette (1 = metal), through holes and pockets, from the drawing itself."""
    content, _ = content_image(load(pdf, dpi=150).pages[0].image)
    _, bw = cv2.threshold(content, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    bw = cv2.dilate(bw, np.ones((3, 3), np.uint8))
    cnts, hier = cv2.findContours(bw, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    # outer outline = biggest external contour in the left 60% (the front view)
    best, area = None, 0
    for i, c in enumerate(cnts):
        x, y, w, h = cv2.boundingRect(c)
        a = cv2.contourArea(c)
        if hier[0][i][3] == -1 and x < content.shape[1] * 0.55 and a > area:
            best, area = i, a
    x, y, w, h = cv2.boundingRect(cnts[best])
    mask = np.zeros_like(content)
    cv2.drawContours(mask, cnts, best, 255, -1)
    holes = np.zeros_like(content)
    pockets = np.zeros_like(content)
    for i, c in enumerate(cnts):
        if i == best:
            continue
        cx, cy, cw, ch = cv2.boundingRect(c)
        if not (x < cx and cx + cw < x + w and y < cy and cy + ch < y + h):
            continue
        a = cv2.contourArea(c)
        per = cv2.arcLength(c, True) + 1e-6
        circ = 4 * math.pi * a / per ** 2
        if a < 60:
            continue
        if circ > 0.75 and cw < w * 0.4:
            cv2.drawContours(holes, [c], -1, 255, -1)
        elif cw > 30 and ch > 10 and circ < 0.75 and cw < w * 0.7 and ch < h * 0.6:
            cv2.drawContours(pockets, [c], -1, 255, -1)
    crop = lambda m: m[y - 4:y + h + 4, x - 4:x + w + 4]
    m, ho, po = crop(mask), crop(holes), crop(pockets)
    # the outline is drawn with a line; erode back to its middle
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    return (m > 0).astype(np.float32), (ho > 0).astype(np.float32), (po > 0).astype(np.float32)


def render_part(pdf: Path, finish="black", seed=3):
    m, holes, pockets = part_mask(pdf)
    metal = np.clip(m - holes, 0, 1)
    h0, w0 = metal.shape
    s = 900 / max(w0, h0)
    size = (int(w0 * s), int(h0 * s))
    metal = cv2.resize(metal, size, interpolation=cv2.INTER_AREA)
    pockets = cv2.resize(pockets, size, interpolation=cv2.INTER_AREA)
    holes = cv2.resize(holes, size, interpolation=cv2.INTER_AREA)

    # place on a big canvas and tilt it into a 3/4 view
    big = np.zeros((H, W), np.float32)
    ox, oy = (W - size[0]) // 2 + 40, (H - size[1]) // 2 - 40
    def place(a):
        c = np.zeros((H, W), np.float32)
        c[oy:oy + a.shape[0], ox:ox + a.shape[1]] = a
        return c
    metal, pockets, holes = place(metal), place(pockets), place(holes)
    src = np.float32([[ox, oy], [ox + size[0], oy], [ox, oy + size[1]], [ox + size[0], oy + size[1]]])
    dst = np.float32([[ox + 120, oy + 120], [ox + size[0] - 60, oy + 40], [ox - 40, oy + size[1] + 10],
                      [ox + size[0] - 160, oy + size[1] - 20]])
    P = cv2.getPerspectiveTransform(src, dst)
    warp = lambda a: cv2.warpPerspective(a, P, (W, H), flags=cv2.INTER_LINEAR)
    top, pk, ho = warp(metal), warp(pockets), warp(holes)

    palette = {
        "black": dict(base=(0.11, 0.115, 0.13), side=(0.045, 0.05, 0.06), spec=0.55),
        "silver": dict(base=(0.62, 0.64, 0.66), side=(0.30, 0.31, 0.33), spec=0.7),
        "gold": dict(base=(0.30, 0.55, 0.70), side=(0.13, 0.26, 0.36), spec=0.6),
        "titanium": dict(base=(0.40, 0.40, 0.42), side=(0.18, 0.18, 0.2), spec=0.5),
    }[finish]
    img = studio()
    # floor shadow
    sh = blur(np.roll(top, 70, axis=0), 91) * 0.75
    img *= (1 - sh[..., None] * 0.85)
    # extrusion: side walls drawn bottom-up
    T = 46
    for z in range(T, 0, -1):
        layer = np.roll(top, z, axis=0)
        shade = np.array(palette["side"]) * (0.75 + 0.5 * (1 - z / T))
        img = img * (1 - layer[..., None]) + layer[..., None] * shade[None, None, :]
    # top face: brushed metal + light gradient + pockets darker
    rng = np.random.default_rng(seed)
    brush = rng.normal(0, 1, (H, W)).astype(np.float32)
    brush = cv2.GaussianBlur(brush, (0, 0), sigmaX=40, sigmaY=0.8)
    brush = (brush - brush.min()) / (np.ptp(brush) + 1e-6)
    yy, xx = np.mgrid[0:H, 0:W]
    light = 0.75 + 0.45 * np.exp(-(((xx - W * 0.62) / (W * 0.35)) ** 2 + ((yy - H * 0.3) / (H * 0.35)) ** 2))
    base = np.array(palette["base"])[None, None, :] * (light * (0.85 + 0.3 * brush))[..., None]
    pocket_shade = 1 - 0.45 * blur(pk, 9)
    face = base * pocket_shade[..., None]
    img = img * (1 - top[..., None]) + face * top[..., None]
    # bevel highlight along the top edges
    edge = np.clip(top - cv2.erode(top, np.ones((5, 5), np.uint8)), 0, 1)
    hi = edge * (yy < H) * palette["spec"]
    img += blur(hi, 3)[..., None] * np.array([0.95, 0.97, 1.0])[None, None, :] * 0.8
    # holes: dark bores with a lit lower wall
    bore = blur(ho, 5)
    img = img * (1 - bore[..., None] * 0.92)
    lower = np.clip(np.roll(ho, -6, axis=0) - ho, 0, 1)
    img += blur(lower, 5)[..., None] * np.array(palette["side"])[None, None, :] * 2.5
    # soft specular sweep across the face
    sweep = np.exp(-((xx - yy * 0.9 - W * 0.25) / 140.0) ** 2) * top * palette["spec"] * 0.35
    img += sweep[..., None]
    return grain(vignette(img), 0.01, seed)


# ------------------------------------------------------------------ 2. chip die

def render_chip(seed=7):
    rng = random.Random(seed)
    img = studio(top=(0.10, 0.07, 0.10), glow=(0.40, 0.18, 0.30), gx=0.55)
    die = np.zeros((900, 900, 3), np.float32)
    die[:] = (0.13, 0.12, 0.10)
    # standard-cell rows
    for r in range(70, 830, 14):
        x = 70
        while x < 830:
            wcell = rng.choice([6, 8, 10, 14, 18, 24])
            col = rng.choice([(0.28, 0.22, 0.55), (0.18, 0.40, 0.55), (0.25, 0.50, 0.38), (0.55, 0.42, 0.25), (0.20, 0.20, 0.30)])
            die[r:r + 11, x:x + wcell - 1] = np.array(col) * rng.uniform(0.7, 1.15)
            x += wcell
    # macro blocks (SRAM, PLL)
    for _ in range(5):
        bx, by = rng.randint(90, 600), rng.randint(90, 600)
        bw, bh = rng.randint(120, 220), rng.randint(90, 200)
        die[by:by + bh, bx:bx + bw] = (0.22, 0.30, 0.36)
        for k in range(by + 6, by + bh - 6, 5):
            die[k:k + 2, bx + 6:bx + bw - 6] = (0.35, 0.48, 0.55)
    # routing: metal layers
    for _ in range(260):
        if rng.random() < 0.5:
            y = rng.randint(70, 830); x1, x2 = sorted(rng.sample(range(70, 830), 2))
            cv2.line(die, (x1, y), (x2, y), (0.55, 0.62, 0.70), 1, cv2.LINE_AA)
        else:
            x = rng.randint(70, 830); y1, y2 = sorted(rng.sample(range(70, 830), 2))
            cv2.line(die, (x, y1), (x, y2), (0.45, 0.70, 0.85), 1, cv2.LINE_AA)
    # bond pads
    for k in range(40, 880, 34):
        for (px, py) in [(k, 20), (k, 860), (20, k), (860, k)]:
            cv2.rectangle(die, (px, py), (px + 20, py + 20), (0.55, 0.72, 0.82), -1)
    die = cv2.GaussianBlur(die, (3, 3), 0)
    # perspective into the scene
    src = np.float32([[0, 0], [900, 0], [0, 900], [900, 900]])
    dst = np.float32([[470, 160], [1250, 120], [380, 880], [1290, 820]])
    P = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(die, P, (W, H))
    m = cv2.warpPerspective(np.ones((900, 900), np.float32), P, (W, H))
    sh = blur(np.roll(m, 40, axis=0), 81)
    img *= (1 - sh[..., None] * 0.7)
    img = img * (1 - m[..., None]) + warped * m[..., None]
    yy, xx = np.mgrid[0:H, 0:W]
    sheen = np.exp(-((xx + yy * 0.6 - 1250) / 170.0) ** 2) * m * 0.35
    img += sheen[..., None] * np.array([1.0, 0.9, 1.0])[None, None, :]
    return grain(vignette(img), 0.01, seed)


# ------------------------------------------------------------------ 3. patient chart

def paper(w, h, seed=1):
    rng = np.random.default_rng(seed)
    p = np.ones((h, w, 3), np.float32) * np.array([0.93, 0.95, 0.96])[None, None, :]
    p += rng.normal(0, 0.01, (h, w))[..., None]
    return p


def render_health(seed=11):
    rng = random.Random(seed)
    img = studio(top=(0.12, 0.09, 0.06), glow=(0.45, 0.30, 0.12), gx=0.5)
    pg = paper(820, 1060, seed)
    ink = (0.18, 0.16, 0.14)
    put_text(pg, "PATIENT RECORD", (60, 90), 1.3, (0.35, 0.25, 0.10), 2)
    cv2.line(pg, (60, 110), (760, 110), (0.6, 0.55, 0.5), 2)
    fields = ["Name", "DOB", "MRN", "Address", "Insurance ID"]
    for i, f in enumerate(fields):
        y = 170 + i * 52
        put_text(pg, f.upper(), (60, y), 0.55, (0.45, 0.42, 0.40), 1)
        cv2.rectangle(pg, (250, y - 24), (250 + rng.randint(260, 440), y + 6), (0.06, 0.06, 0.08), -1)
    put_text(pg, "ECG  LEAD II", (60, 470), 0.6, (0.45, 0.42, 0.40), 1)
    for gx in range(60, 770, 20):
        cv2.line(pg, (gx, 490), (gx, 680), (0.80, 0.82, 0.95), 1)
    for gy in range(490, 690, 20):
        cv2.line(pg, (60, gy), (760, gy), (0.80, 0.82, 0.95), 1)
    pts = []
    for x in range(60, 760):
        t = (x - 60) / 120.0
        ph = t % 1
        v = 0.0
        v += 0.12 * math.exp(-((ph - 0.18) / 0.04) ** 2)
        v -= 0.15 * math.exp(-((ph - 0.36) / 0.012) ** 2)
        v += 1.0 * math.exp(-((ph - 0.40) / 0.012) ** 2)
        v -= 0.25 * math.exp(-((ph - 0.44) / 0.014) ** 2)
        v += 0.25 * math.exp(-((ph - 0.65) / 0.06) ** 2)
        pts.append((x, int(600 - v * 90)))
    cv2.polylines(pg, [np.array(pts, np.int32)], False, (0.15, 0.15, 0.75), 2, cv2.LINE_AA)
    for i in range(7):
        y = 740 + i * 38
        cv2.line(pg, (60, y), (60 + rng.randint(380, 700), y), (0.55, 0.53, 0.50), 6)
    cv2.rectangle(pg, (610, 40), (760, 96), (0.20, 0.25, 0.80), 3)
    put_text(pg, "PHI", (648, 82), 1.2, (0.20, 0.25, 0.80), 3)
    pg = cv2.GaussianBlur(pg, (3, 3), 0)
    src = np.float32([[0, 0], [820, 0], [0, 1060], [820, 1060]])
    dst = np.float32([[560, 70], [1180, 120], [470, 930], [1130, 990]])
    P = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(pg, P, (W, H))
    m = cv2.warpPerspective(np.ones((1060, 820), np.float32), P, (W, H))
    sh = blur(np.roll(np.roll(m, 30, axis=0), 25, axis=1), 71)
    img *= (1 - sh[..., None] * 0.75)
    img = img * (1 - m[..., None]) + warped * m[..., None] * 0.92
    return grain(vignette(img), 0.01, seed)


# ------------------------------------------------------------------ 4. 96-well plate

def render_plate(seed=5):
    rng = random.Random(seed)
    img = studio(top=(0.10, 0.10, 0.06), glow=(0.25, 0.40, 0.18), gx=0.5)
    pw, ph = 1280, 860
    plate = np.zeros((ph, pw, 3), np.float32)
    plate[:] = (0.80, 0.82, 0.83)
    cv2.rectangle(plate, (0, 0), (pw - 1, ph - 1), (0.62, 0.64, 0.66), 10)
    cols = [(0.20, 0.25, 0.80), (0.25, 0.65, 0.85), (0.30, 0.75, 0.40), (0.75, 0.40, 0.70), (0.25, 0.80, 0.90)]
    for r in range(8):
        put_text(plate, "ABCDEFGH"[r], (28, 125 + r * 92), 1.0, (0.4, 0.4, 0.42), 2)
        for c in range(12):
            cx, cy = 110 + c * 96, 112 + r * 92
            cv2.circle(plate, (cx, cy), 38, (0.45, 0.47, 0.50), -1, cv2.LINE_AA)
            if rng.random() < 0.82:
                col = np.array(rng.choice(cols)) * rng.uniform(0.6, 1.0)
                cv2.circle(plate, (cx, cy), 31, tuple(float(v) for v in col), -1, cv2.LINE_AA)
                cv2.circle(plate, (cx - 10, cy - 12), 8, (0.95, 0.97, 1.0), -1, cv2.LINE_AA)
            else:
                cv2.circle(plate, (cx, cy), 31, (0.70, 0.72, 0.74), -1, cv2.LINE_AA)
    for c in range(12):
        put_text(plate, str(c + 1), (98 + c * 96, 42), 0.8, (0.4, 0.4, 0.42), 2)
    plate = cv2.GaussianBlur(plate, (3, 3), 0)
    src = np.float32([[0, 0], [pw, 0], [0, ph], [pw, ph]])
    dst = np.float32([[430, 250], [1360, 210], [250, 860], [1420, 800]])
    P = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(plate, P, (W, H))
    m = cv2.warpPerspective(np.ones((ph, pw), np.float32), P, (W, H))
    sh = blur(np.roll(m, 45, axis=0), 91)
    img *= (1 - sh[..., None] * 0.75)
    img = img * (1 - m[..., None]) + warped * m[..., None]
    return grain(vignette(img), 0.01, seed)


# ------------------------------------------------------------------ 5. government files

def render_gov(seed=9):
    rng = random.Random(seed)
    img = studio(top=(0.09, 0.08, 0.06), glow=(0.40, 0.30, 0.18), gx=0.48)
    for k, (angle, dx, dy) in enumerate([(-9, -90, 40), (6, 60, 10), (-2, 0, -10)]):
        pg = paper(760, 980, seed + k)
        cv2.rectangle(pg, (0, 0), (760, 54), (0.18, 0.55, 0.25), -1)
        put_text(pg, "CUI", (330, 40), 1.2, (1, 1, 1), 3)
        put_text(pg, "CONTROLLED UNCLASSIFIED INFORMATION", (70, 110), 0.75, (0.25, 0.25, 0.25), 2)
        for i in range(16):
            y = 170 + i * 40
            if rng.random() < 0.25:
                cv2.rectangle(pg, (70, y - 18), (70 + rng.randint(200, 600), y + 6), (0.05, 0.05, 0.06), -1)
            else:
                cv2.line(pg, (70, y), (70 + rng.randint(300, 620), y), (0.55, 0.53, 0.50), 6)
        cv2.rectangle(pg, (0, 926), (760, 980), (0.18, 0.55, 0.25), -1)
        put_text(pg, "CUI", (330, 966), 1.2, (1, 1, 1), 3)
        pg = cv2.GaussianBlur(pg, (3, 3), 0)
        M = cv2.getRotationMatrix2D((380, 490), angle, 0.78)
        M[0, 2] += 420 + dx
        M[1, 2] += 10 + dy
        warped = cv2.warpAffine(pg, M, (W, H))
        m = cv2.warpAffine(np.ones((980, 760), np.float32), M, (W, H))
        sh = blur(np.roll(np.roll(m, 25, axis=0), 20, axis=1), 61)
        img *= (1 - sh[..., None] * 0.6)
        img = img * (1 - m[..., None]) + warped * m[..., None] * 0.9
    return grain(vignette(img), 0.01, seed)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    key = list(csv.DictReader(open(DATA / "outgoing" / "answer_key.csv")))
    hero = next(r for r in key if r["file"].endswith("A01_bracket_views_only.png"))
    vault = {r["part_number"]: r for r in csv.DictReader(open(DATA / "vault" / "vault_index.csv"))}
    pdf = DATA / "vault" / vault[hero["source_part"]]["file"]
    print("rendering ->", OUT)
    save(render_part(pdf, "black"), "v_defense.jpg")
    others = [r for r in vault.values() if r["jurisdiction"] == "ITAR" and r["part_number"] != hero["source_part"]][:2]
    for r, fin in zip(others, ["silver", "gold"]):
        try:
            save(render_part(DATA / "vault" / r["file"], fin, seed=hash(r["part_number"]) % 100), f"v_part_{fin}.jpg")
        except Exception as e:  # some drawings have awkward outlines; skip them
            print("   skip", r["part_number"], e)
    save(render_chip(), "v_chips.jpg")
    save(render_health(), "v_health.jpg")
    save(render_plate(), "v_biotech.jpg")
    save(render_gov(), "v_gov.jpg")


if __name__ == "__main__":
    main()
