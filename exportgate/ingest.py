"""Turn any incoming file into page images (+ the PDF text layer when there is one).

Drawings rarely leave as a clean PDF. They get pasted into a quote deck, zipped with a job folder, or ride along
inside a CNC program whose header names the part. So besides PDFs and images this also opens:
  Word / PowerPoint / Excel  every embedded picture or PDF becomes a page; the document's own text is its text layer
  ZIP                        every supported file inside, including ZIPs inside ZIPs (2 levels)
  CNC programs and text      the text is the text layer (part numbers in G-code headers), drawn onto a page image
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import cv2
import pymupdf as fitz
import numpy as np

from .config import SETTINGS

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
OFFICE_EXT = {".docx", ".pptx", ".xlsx"}
TEXT_EXT = {".nc", ".gcode", ".ngc", ".tap", ".txt", ".csv"}
SUPPORTED_EXT = IMAGE_EXT | OFFICE_EXT | TEXT_EXT | {".pdf", ".zip"}
MAX_PAGES = 4
MAX_MEMBER_BYTES = 60 << 20   # skip anything bigger inside an archive (zip bombs)
_XML_TEXT = re.compile(r"<(?:w:t|a:t|t)(?:\s[^>]*)?>([^<]*)</(?:w:t|a:t|t)>")


@dataclass
class Page:
    index: int
    image: np.ndarray  # grayscale uint8
    text_layer: str    # empty for scans and photos


@dataclass
class Document:
    path: Path
    sha256: str
    kind: str  # pdf | image
    pages: list[Page]

    @property
    def has_text_layer(self) -> bool:
        return any(len(p.text_layer.strip()) > 40 for p in self.pages)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load(path: str | Path, dpi: int | None = None) -> Document:
    path = Path(path)
    dpi = dpi or SETTINGS.render_dpi
    ext = path.suffix.lower()
    pages: list[Page] = []
    if ext == ".pdf":
        with fitz.open(path) as doc:
            for i, page in enumerate(doc):
                if i >= MAX_PAGES:
                    break
                pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
                img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
                pages.append(Page(i, img, page.get_text("text") or ""))
        kind = "pdf"
    elif ext in IMAGE_EXT:
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise ValueError(f"cannot read image {path}")
        pages.append(Page(0, img, ""))
        kind = "image"
    elif ext in OFFICE_EXT or ext == ".zip":
        texts: list[str] = []
        with zipfile.ZipFile(path) as z:
            _from_zip(z, pages, texts, dpi, depth=0, office=ext in OFFICE_EXT)
        text = "\n".join(t for t in texts if t.strip())
        if not pages:   # text only (e.g. a spreadsheet BOM): draw the text so there is something to look at
            pages.append(Page(0, _text_image(text), text))
        elif text:
            pages[0] = Page(0, pages[0].image, (pages[0].text_layer + "\n" + text).strip())
        kind = "office" if ext in OFFICE_EXT else "zip"
    elif ext in TEXT_EXT:
        text = path.read_text(errors="replace")[:200_000]
        pages.append(Page(0, _text_image(text), text))
        kind = "text"
    else:
        raise ValueError(f"unsupported file type: {ext}")
    return Document(path, sha256_file(path), kind, pages)


def _from_zip(z: zipfile.ZipFile, pages: list, texts: list, dpi: int, depth: int, office: bool):
    for info in z.infolist():
        if info.is_dir() or info.file_size > MAX_MEMBER_BYTES or len(pages) >= MAX_PAGES:
            continue
        name = info.filename
        low = name.lower()
        ext = Path(low).suffix
        if office and ext == ".xml":   # document text: word/document.xml, ppt/slides/slideN.xml, xl/sharedStrings.xml
            if re.search(r"(document|slide\d+|sharedstrings|header\d*|footer\d*)\.xml$", low):
                texts.append(" ".join(_XML_TEXT.findall(z.read(info).decode("utf-8", "replace"))))
            continue
        data = z.read(info)
        if ext in IMAGE_EXT:
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
            if img is not None and min(img.shape[:2]) >= 120:   # skip icons and bullets
                pages.append(Page(len(pages), img, ""))
        elif ext == ".pdf":
            with fitz.open(stream=data, filetype="pdf") as doc:
                for page in doc:
                    if len(pages) >= MAX_PAGES:
                        break
                    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
                    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
                    pages.append(Page(len(pages), img, page.get_text("text") or ""))
        elif ext in TEXT_EXT:
            texts.append(data.decode("utf-8", "replace")[:200_000])
        elif ext in OFFICE_EXT | {".zip"} and depth < 2:
            with zipfile.ZipFile(io.BytesIO(data)) as inner:
                _from_zip(inner, pages, texts, dpi, depth + 1, office=ext in OFFICE_EXT)


def _text_image(text: str, w: int = 1100) -> np.ndarray:
    """Render the first lines of a text file, so text-only files still have a page to show and fingerprint."""
    lines = [ln[:90] for ln in text.splitlines()[:36]] or [""]
    img = np.full((max(300, 40 + 28 * len(lines)), w), 255, np.uint8)
    for i, ln in enumerate(lines):
        cv2.putText(img, ln, (24, 40 + 28 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.62, 0, 1, cv2.LINE_AA)
    return img


def tiles(img: np.ndarray, size: int | None = None, overlap: float | None = None):
    """Overlapping square tiles so small drawing lettering stays readable to a vision model."""
    size = size or SETTINGS.tile_px
    overlap = SETTINGS.tile_overlap if overlap is None else overlap
    h, w = img.shape[:2]
    stride = max(1, int(size * (1 - overlap)))

    def starts(n):
        if n <= size:
            return [0]
        s = list(range(0, n - size, stride))
        s.append(n - size)
        return sorted(set(s))

    out = []
    for y in starts(h):
        for x in starts(w):
            out.append(((x, y), img[y:y + size, x:x + size]))
    return out


def png_bytes(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("png encode failed")
    return buf.tobytes()
