"""Read a document into text + grounded clues. "No quote, no claim."

Transcript source, cheapest first:
  - PDF text layer (no model call),
  - otherwise the local vision model transcribes overlapping tiles.
Clues come from two extractors over the transcript:
  - deterministic patterns (always),
  - the local model's claims (when available). Every model claim must quote text that
    really is in the transcript (fuzzy match >= 90), or it is dropped and counted.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

import numpy as np
from rapidfuzz import fuzz

from . import llm
from .config import SETTINGS
from .ingest import Document, png_bytes, tiles
from .registry import registry

QUOTE_MIN = 90

NEG = re.compile(r"\b(NOT|NO|NON|UN)[\s-]*(SUBJECT TO |CONTROLLED BY |-)?$")
PATTERNS = [
    # field, value, regex
    ("export_marking", "ITAR", re.compile(r"\bITAR\b|ARMS EXPORT CONTROL ACT|INTERNATIONAL TRAFFIC IN ARMS|22\s*U\.?S\.?C\.?,?\s*(SEC\.?\s*)?2751|22\s*CFR\s*12\d")),
    ("classification", "USML", re.compile(r"\bUSML\s*(CAT\.?\s*)?[IVXL]+(\([a-z0-9]+\))*", re.I)),
    ("export_marking", "EAR", re.compile(r"EXPORT ADMINISTRATION REGULATIONS|\bEAR\b(?!\s*-?99)|15\s*CFR\s*7[3-7]\d")),
    ("classification", "ECCN", re.compile(r"\bECCN\s*[0-9][A-E][0-9]{3}(\.[a-z0-9]+)?", re.I)),
    ("commercial_marker", "EAR99", re.compile(r"\bEAR\s*-?99\b|\bNLR\b|NO LICENSE REQUIRED")),
    ("commercial_marker", "PUBLIC_RELEASE", re.compile(r"APPROVED FOR PUBLIC RELEASE|DISTRIBUTION IS UNLIMITED")),
    ("commercial_marker", "CATALOG", re.compile(r"COMMERCIAL CATALOG|\bCOTS\b|CATALOG PART")),
]
DIST = re.compile(r"DISTRIBUTION\s+STATEMENT\s+([A-F])\b")
# Text that talks to the AI instead of describing the part: a prompt-injection attempt.
INJECTION = re.compile(r"IGNORE\s+(ALL\s+|ANY\s+)?(PRIOR|PREVIOUS|ABOVE|EARLIER)|NOTE\s+TO\s+(THE\s+)?(AI|MODEL|ASSISTANT|REVIEWER)"
                       r"|\b(AI|LLM|MODEL)\s*:\s|SYSTEM\s*(PROMPT|NOTE|MESSAGE)?\s*:|APPROVE\s+(THE\s+|THIS\s+)?RELEASE"
                       r"|CLASSIFY\s+(THIS|IT)\s+AS|DISREGARD\s+(THE\s+)?(MARKINGS?|LABELS?|INSTRUCTIONS)")
PN = re.compile(r"\b([A-Z]{2,4}-[0-9OQIL]{3,6}[A-Z]?)\b")


@dataclass
class Clue:
    field: str
    value: str
    quote: str
    source: str  # pattern | model


@dataclass
class Reading:
    transcript: str
    transcript_source: str  # text-layer | vision-model | none
    clues: list[Clue] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)  # model claims that failed the quote check
    model_calls: int = 0
    tiles_read: int = 0
    error: str = ""

    def to_dict(self):
        d = asdict(self)
        d["transcript"] = self.transcript[:4000]
        return d

    def values(self, field_name):
        return {c.value for c in self.clues if c.field == field_name}


def _ctx(text, m, pad=40):
    a, b = max(0, m.start() - pad), min(len(text), m.end() + pad)
    return " ".join(text[a:b].split())


def pattern_clues(text: str) -> list[Clue]:
    up = text.upper()
    out: list[Clue] = []
    seen = set()
    for fld, val, rx in PATTERNS:
        for m in rx.finditer(up):
            before = up[max(0, m.start() - 22):m.start()]
            if fld == "export_marking" and NEG.search(before):
                continue  # "NOT SUBJECT TO ITAR"
            v = val
            if fld == "classification":
                v = " ".join(m.group(0).split()).upper()
            if (fld, v) not in seen:
                seen.add((fld, v))
                out.append(Clue(fld, v, _ctx(text, m), "pattern"))
    for m in DIST.finditer(up):
        v = m.group(1)
        if ("distribution_statement", v) not in seen:
            seen.add(("distribution_statement", v))
            out.append(Clue("distribution_statement", v, _ctx(text, m), "pattern"))
    m = INJECTION.search(up)
    if m:
        out.append(Clue("injection", "instruction to the AI", _ctx(text, m, 60), "pattern"))
    for m in PN.finditer(up):
        v = m.group(1)
        if ("part_number", v) not in seen:
            seen.add(("part_number", v))
            out.append(Clue("part_number", v, _ctx(text, m, 20), "pattern"))
    for prog in registry().programs:
        if prog in ("CATALOG",):
            continue
        for m in re.finditer(r"\b" + re.escape(prog) + r"\b", up):
            if ("program_name", prog) not in seen:
                seen.add(("program_name", prog))
                out.append(Clue("program_name", prog, _ctx(text, m), "pattern"))
            break
    return out


def grounded(quote: str, transcript: str) -> bool:
    q = " ".join(quote.split()).upper()
    if len(q) < 3:
        return False
    return fuzz.partial_ratio(q, " ".join(transcript.split()).upper()) >= QUOTE_MIN


def _blank(tile: np.ndarray) -> bool:
    return float((tile < 160).mean()) < 0.002


def transcribe_pages(doc: Document) -> tuple[str, int]:
    jobs = []
    for p in doc.pages:
        for (x, y), t in tiles(p.image):
            if not _blank(t):
                jobs.append(((p.index, y, x), t))
    if not jobs:
        return "", 0
    with ThreadPoolExecutor(max_workers=SETTINGS.llm_concurrency) as ex:
        texts = list(ex.map(lambda j: llm.transcribe(png_bytes(j[1])), jobs))
    parts = []
    for (key, _), txt in sorted(zip(jobs, texts), key=lambda z: z[0][0]):
        if txt.strip():
            parts.append(txt.strip())
    return "\n".join(parts), len(jobs)


def read(doc: Document, mode: str | None = None, use_model_claims: bool | None = None) -> Reading:
    mode = mode or SETTINGS.reader
    text_layer = "\n".join(p.text_layer for p in doc.pages).strip()
    rd = Reading("", "none")
    if text_layer and mode in ("auto", "text") and doc.has_text_layer:
        rd.transcript, rd.transcript_source = text_layer, "text-layer"
    elif mode in ("auto", "vlm"):
        try:
            rd.transcript, rd.tiles_read = transcribe_pages(doc)
            rd.model_calls += rd.tiles_read
            rd.transcript_source = "vision-model"
        except llm.LLMUnavailable as e:
            rd.error = str(e)
            if text_layer:
                rd.transcript, rd.transcript_source = text_layer, "text-layer"
    rd.clues = pattern_clues(rd.transcript)

    if use_model_claims is None:
        use_model_claims = rd.transcript_source == "vision-model"
    if use_model_claims and rd.transcript and not rd.error:
        try:
            claims = llm.extract_claims(rd.transcript)
            rd.model_calls += 1
        except llm.LLMUnavailable as e:
            claims, rd.error = [], str(e)
        have = {(c.field, c.value.upper()) for c in rd.clues}
        for c in claims:
            fld, val, quote = c.get("field", ""), str(c.get("value", "")).strip(), str(c.get("quote", ""))
            if not grounded(quote, rd.transcript):
                rd.dropped.append({"field": fld, "value": val, "quote": quote, "why": "quote not in document"})
                continue
            # A grounded claim adds what our own patterns recognise in its quote. Anything the
            # patterns don't know (e.g. "defense article") is kept as a weak, unstructured hint.
            recognised = pattern_clues(quote)
            for pc in recognised:
                if (pc.field, pc.value.upper()) not in have:
                    have.add((pc.field, pc.value.upper()))
                    rd.clues.append(Clue(pc.field, pc.value, quote, "model"))
            if not recognised and fld in ("export_marking", "program_name", "classification"):
                rd.clues.append(Clue("hint", f"{fld}: {val}"[:80], quote, "model"))
    return rd
