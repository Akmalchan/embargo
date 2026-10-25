"""Build the console's pictures from our own fictional drawings (no outside images).

  python scripts/make_assets.py   ->  web/static/img/*.png
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from exportgate.fingerprint import Vault, content_image  # noqa: E402
from exportgate.ingest import load  # noqa: E402

OUT = ROOT / "web" / "static" / "img"
DATA = ROOT / "data"


def blueprint(gray: np.ndarray, ink=(255, 214, 160), paper=(28, 17, 8)) -> np.ndarray:
    """Black-on-white drawing -> light lines on deep navy (BGR)."""
    g = gray.astype(np.float32) / 255.0
    ink_amt = np.clip((1.0 - g) * 1.6, 0, 1)[..., None]
    paper_c = np.array(paper, np.float32)[None, None, :]
    ink_c = np.array(ink, np.float32)[None, None, :]
    img = paper_c * (1 - ink_amt) + ink_c * ink_amt
    h, w = gray.shape
    yy, xx = np.mgrid[0:h, 0:w]
    grid = ((yy % 40 == 0) | (xx % 40 == 0)).astype(np.float32)[..., None] * 0.10
    img = img + grid * np.array([120, 80, 40], np.float32)
    return np.clip(img, 0, 255).astype(np.uint8)


def fit(img, width):
    s = width / img.shape[1]
    return cv2.resize(img, (width, int(img.shape[0] * s)), interpolation=cv2.INTER_AREA)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    key = list(csv.DictReader(open(DATA / "outgoing" / "answer_key.csv")))
    hero = next(r for r in key if r["file"].endswith("A01_bracket_views_only.png"))
    vault_rows = {r["part_number"]: r for r in csv.DictReader(open(DATA / "vault" / "vault_index.csv"))}
    original = load(DATA / "vault" / vault_rows[hero["source_part"]]["file"], dpi=150).pages[0].image
    cropped = load(DATA / hero["file"]).pages[0].image

    # 1. hero: the full marked original as a blueprint
    cv2.imwrite(str(OUT / "hero_blueprint.png"), fit(blueprint(original), 1800))

    # 2. the original paper look (for "marked original")
    cv2.imwrite(str(OUT / "step_original.png"), fit(cv2.cvtColor(original, cv2.COLOR_GRAY2BGR), 900))

    # 3. the cropped copy someone tries to send
    cv2.imwrite(str(OUT / "step_cropped.png"), fit(cv2.cvtColor(cropped, cv2.COLOR_GRAY2BGR), 900))

    # 4. what the fingerprint sees: matching points between the copy and the original
    v = Vault.open()
    pic = v.match_picture(cropped, hero["source_part"])
    if pic is not None:
        cv2.imwrite(str(OUT / "step_match.png"), fit(pic, 1400))

    # 5. the masked content region the fingerprint compares (labels removed)
    content, _ = content_image(original)
    cv2.imwrite(str(OUT / "step_content.png"), fit(blueprint(content), 1200))
    print("assets ->", OUT, sorted(p.name for p in OUT.glob("*.png")))


if __name__ == "__main__":
    main()
