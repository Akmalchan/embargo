"""Drawing fingerprint: recognise a copy of a vault original even with its labels cut off.

No AI model is involved. Steps:
  1. Find the drawing frame and straighten it (handles rescans and crops to the frame).
  2. Mask the parts people crop or paint over: margins, title block, notes strip.
     What is left is the geometry, which is what actually carries the technical data.
  3. ORB keypoints on that content. Every vault descriptor sits in one index; each
     query keypoint votes for the vault drawing its nearest neighbour came from.
  4. Verify the top candidates geometrically (RANSAC similarity transform).
     Inliers I, coverage M = I / min(n_query, n_vault), distinctiveness D = I / max(I_2nd, 20)
     where I_2nd is the best inlier count for any OTHER vault drawing. Repeating patterns
     (hatching, hole symbols) give every sheet ~100 chance inliers; D is what separates a real copy.
        strong:  I >= 250 and D >= 3
        weak:    I >= 60  and D >= 2.5
  5. A 64-bit perceptual hash is recorded as supporting detail for whole-sheet copies.
"""
from __future__ import annotations

import csv
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import cv2
import imagehash
import numpy as np
from PIL import Image

from .config import SETTINGS
from .ingest import load

CANON_W, CANON_H = 1600, 1000      # frame aspect of a B-size / tabloid sheet (1.6)
BORDER = 0.015                     # inside-frame band dropped (frame lines)
BOTTOM_CUT = 0.77                  # below this the sheet is notes + title block
N_FEATURES = 2500
VOTE_FEATURES = 600

# Thresholds (tuned on the answer key, see scripts/eval.py and RESULTS.md)
STRONG_I, STRONG_D = 250, 3.0
WEAK_I, WEAK_D = 60, 2.5
D_FLOOR = 20

_orb = None


def orb():
    global _orb
    if _orb is None:
        _orb = cv2.ORB_create(nfeatures=N_FEATURES, scaleFactor=1.2, nlevels=8, fastThreshold=12)
    return _orb


def _order_quad(pts: np.ndarray) -> np.ndarray:
    pts = pts.reshape(4, 2).astype(np.float32)
    s, d = pts.sum(1), np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], np.float32)


def find_frame(gray: np.ndarray):
    """Largest 4-corner contour covering a good part of the image, or None."""
    h, w = gray.shape
    small = 1000.0 / max(h, w)
    g = cv2.resize(gray, (int(w * small), int(h * small)), interpolation=cv2.INTER_AREA)
    _, bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    bw = cv2.dilate(bw, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(bw, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    best, best_area = None, 0.0
    min_area = 0.35 * g.shape[0] * g.shape[1]
    for c in contours:
        area = cv2.contourArea(c)
        if area < min_area or area <= best_area:
            continue
        approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            quad = _order_quad(approx) / small
            wq = np.linalg.norm(quad[1] - quad[0])
            hq = np.linalg.norm(quad[3] - quad[0])
            if hq > 0 and 1.3 < wq / hq < 1.9:  # landscape drawing sheet
                best, best_area = quad, area
    return best


def content_image(gray: np.ndarray):
    """Return (content image, how it was obtained). Content = geometry only."""
    quad = find_frame(gray)
    if quad is not None:
        dst = np.array([[0, 0], [CANON_W, 0], [CANON_W, CANON_H], [0, CANON_H]], np.float32)
        M = cv2.getPerspectiveTransform(quad, dst)
        warped = cv2.warpPerspective(gray, M, (CANON_W, CANON_H), borderValue=255)
        b = int(BORDER * CANON_W)
        return warped[b:int(BOTTOM_CUT * CANON_H), b:CANON_W - b].copy(), "frame"
    h, w = gray.shape
    s = CANON_W / max(h, w)
    return cv2.resize(gray, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA), "whole"


def features(content: np.ndarray):
    """ORB keypoints sorted strongest first, so the voting stage can use just the top ones."""
    kps, des = orb().detectAndCompute(content, None)
    if not kps or des is None:
        return np.zeros((0, 2), np.float32), np.zeros((0, 32), np.uint8)
    order = np.argsort([-k.response for k in kps])
    pts = np.array([kps[i].pt for i in order], np.float32)
    return pts, des[order]


def phash(content: np.ndarray) -> int:
    return int(str(imagehash.phash(Image.fromarray(content), hash_size=8)), 16)


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


@dataclass
class VaultItem:
    file: str
    part_number: str
    rev: str
    program: str
    jurisdiction: str
    classification: str
    phash: int
    n_features: int


@dataclass
class Match:
    file: str
    part_number: str
    jurisdiction: str
    classification: str
    program: str
    inliers: int
    score: float
    phash_distance: int
    votes: int
    distinct: float = 0.0


@dataclass
class FingerprintResult:
    strength: str  # strong | weak | none
    best: Match | None
    candidates: list[Match] = field(default_factory=list)
    mode: str = ""
    n_query_features: int = 0
    ms: float = 0.0

    def to_dict(self):
        d = asdict(self)
        return d


class Vault:
    """Index of the company's marked originals."""

    def __init__(self, items, pts, des, owner):
        self.items: list[VaultItem] = items
        self.pts: list[np.ndarray] = pts
        self.des: list[np.ndarray] = des
        self.owner = owner  # vault index for every stacked descriptor
        self.all_des = np.vstack(des) if des else np.zeros((0, 32), np.uint8)
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING)

    # ---- build / persist
    @classmethod
    def build(cls, vault_dir: Path | None = None):
        vault_dir = vault_dir or SETTINGS.vault_dir
        items, pts, des, owner = [], [], [], []
        with open(vault_dir / "vault_index.csv") as f:
            rows = list(csv.DictReader(f))
        for i, r in enumerate(rows):
            doc = load(vault_dir / r["file"])
            content, _ = content_image(doc.pages[0].image)
            p, d = features(content)
            items.append(VaultItem(r["file"], r["part_number"], r["rev"], r["program"], r["jurisdiction"],
                                   r["classification"], phash(content), len(p)))
            pts.append(p)
            des.append(d)
            owner.append(np.full(len(d), i, np.int32))
        v = cls(items, pts, des, np.concatenate(owner) if owner else np.zeros(0, np.int32))
        v.save()
        return v

    def save(self, path: Path | None = None):
        path = path or SETTINGS.state_dir / "vault"
        path.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path / "features.npz",
                            **{f"p{i}": p for i, p in enumerate(self.pts)},
                            **{f"d{i}": d for i, d in enumerate(self.des)})
        (path / "items.json").write_text(json.dumps([asdict(i) for i in self.items], indent=1))

    @classmethod
    def open(cls, path: Path | None = None):
        path = path or SETTINGS.state_dir / "vault"
        if not (path / "items.json").exists():
            return cls.build()
        items = [VaultItem(**d) for d in json.loads((path / "items.json").read_text())]
        z = np.load(path / "features.npz")
        pts = [z[f"p{i}"] for i in range(len(items))]
        des = [z[f"d{i}"] for i in range(len(items))]
        owner = np.concatenate([np.full(len(d), i, np.int32) for i, d in enumerate(des)])
        return cls(items, pts, des, owner)

    # ---- query
    def _verify(self, qpts, qdes, i):
        vd = self.des[i]
        if len(vd) < 8 or len(qdes) < 8:
            return 0
        knn = self.matcher.knnMatch(qdes, vd, k=2)
        good = [m for m, n in (p for p in knn if len(p) == 2) if m.distance < 0.8 * n.distance]
        if len(good) < 8:
            return 0
        src = qpts[[m.queryIdx for m in good]]
        dst = self.pts[i][[m.trainIdx for m in good]]
        A, mask = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=6.0,
                                              maxIters=4000, confidence=0.995)
        if A is None or mask is None:
            return 0
        s = float(np.hypot(A[0, 0], A[1, 0]))
        if not 0.4 < s < 2.5:
            return 0
        return int(mask.sum())

    def match(self, gray: np.ndarray, top_k: int = 4) -> FingerprintResult:
        t0 = time.perf_counter()
        content, mode = content_image(gray)
        qpts, qdes = features(content)
        qh = phash(content)
        if len(qdes) == 0 or len(self.all_des) == 0:
            return FingerprintResult("none", None, [], mode, 0, (time.perf_counter() - t0) * 1000)

        knn = self.matcher.knnMatch(qdes[:VOTE_FEATURES], self.all_des, k=2)  # vote with the strongest only
        votes = np.zeros(len(self.items), np.int32)
        for pair in knn:
            if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance:
                votes[self.owner[pair[0].trainIdx]] += 1
        ph = np.array([hamming(qh, it.phash) for it in self.items])
        cand = list(np.argsort(-votes)[:top_k])
        if mode == "frame":  # pHash only means something on a whole straightened sheet
            cand += [int(j) for j in np.argsort(ph)[:2] if j not in cand]

        results = []
        for i in cand:
            I = self._verify(qpts, qdes, int(i))
            it = self.items[int(i)]
            score = I / max(1, min(len(qpts), it.n_features))
            results.append(Match(it.file, it.part_number, it.jurisdiction, it.classification, it.program,
                                 I, round(score, 3), int(ph[i]), int(votes[i])))
        results.sort(key=lambda m: (m.inliers, -m.phash_distance), reverse=True)
        best = results[0] if results else None
        strength = "none"
        if best:
            second = max((m.inliers for m in results[1:] if m.part_number != best.part_number), default=0)
            best.distinct = round(best.inliers / max(second, D_FLOOR), 2)
            if best.inliers >= STRONG_I and best.distinct >= STRONG_D:
                strength = "strong"
            elif best.inliers >= WEAK_I and best.distinct >= WEAK_D:
                strength = "weak"
        return FingerprintResult(strength, best, results, mode, len(qpts), round((time.perf_counter() - t0) * 1000, 1))

    def match_picture(self, gray: np.ndarray, part_number: str) -> np.ndarray | None:
        """Side-by-side picture of the matching points, for the evidence card."""
        idx = next((i for i, it in enumerate(self.items) if it.part_number == part_number), None)
        if idx is None:
            return None
        content, _ = content_image(gray)
        qkp, qdes = orb().detectAndCompute(content, None)
        vdoc = load(SETTINGS.vault_dir / self.items[idx].file)
        vcontent, _ = content_image(vdoc.pages[0].image)
        vkp, vdes = orb().detectAndCompute(vcontent, None)
        if qdes is None or vdes is None:
            return None
        knn = self.matcher.knnMatch(qdes, vdes, k=2)
        good = [m for m, n in (p for p in knn if len(p) == 2) if m.distance < 0.8 * n.distance]
        if len(good) < 8:
            return None
        src = np.float32([qkp[m.queryIdx].pt for m in good])
        dst = np.float32([vkp[m.trainIdx].pt for m in good])
        _, mask = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=6.0)
        inl = [g for g, k in zip(good, mask.ravel()) if k] if mask is not None else []
        pic = cv2.drawMatches(cv2.cvtColor(content, cv2.COLOR_GRAY2BGR), qkp,
                              cv2.cvtColor(vcontent, cv2.COLOR_GRAY2BGR), vkp, inl[::max(1, len(inl) // 120)], None,
                              matchColor=(200, 110, 20), singlePointColor=None,
                              flags=cv2.DrawMatchesFlags_NOT_DRAW_SINGLE_POINTS)
        s = 1400 / pic.shape[1]
        return cv2.resize(pic, (1400, int(pic.shape[0] * s)), interpolation=cv2.INTER_AREA)
