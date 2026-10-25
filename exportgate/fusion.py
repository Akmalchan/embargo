"""Evidence ledger: combine independent clues into one probability, in log-odds.

    logit(p) = logit(p0) + sum_i log(LR_i)        p = 1 / (1 + e^-logit)

LR_i = how many times more likely clue i is if the document IS export-controlled.
Within one category only the strongest clue counts, so repeated wording can't stack.
Jurisdiction = the most restrictive one any credible clue points to.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from .fingerprint import FingerprintResult
from .reader import Reading
from .registry import RESTRICTIVENESS, RegisterHit, registry

P0 = 0.15  # prior: share of a machine shop's outgoing drawings that are controlled (assumption)
CONTROLLED_AT, UNSURE_AT = 0.90, 0.50

LR = {
    ("vault", "strong", "ITAR"): 30.0, ("vault", "strong", "EAR"): 15.0, ("vault", "strong", "EAR99"): 0.2,
    ("vault", "weak", "ITAR"): 4.0, ("vault", "weak", "EAR"): 3.0, ("vault", "weak", "EAR99"): 0.7,
    ("register", "exact", "ITAR"): 50.0, ("register", "exact", "EAR"): 20.0, ("register", "exact", "EAR99"): 0.2,
    ("register", "normalized", "ITAR"): 25.0, ("register", "normalized", "EAR"): 10.0, ("register", "normalized", "EAR99"): 0.5,
    ("register", "fuzzy", "ITAR"): 8.0, ("register", "fuzzy", "EAR"): 5.0, ("register", "fuzzy", "EAR99"): 0.8,
    ("marking", "ITAR"): 40.0, ("marking", "EAR"): 20.0,
    ("classification", "USML"): 30.0, ("classification", "ECCN"): 10.0,
    ("distribution", "B"): 8.0, ("distribution", "C"): 8.0, ("distribution", "D"): 8.0,
    ("distribution", "E"): 8.0, ("distribution", "F"): 8.0, ("distribution", "A"): 0.4,
    ("program", "controlled"): 10.0,
    ("commercial", "EAR99"): 0.3, ("commercial", "PUBLIC_RELEASE"): 0.4, ("commercial", "CATALOG"): 0.3,
    ("hint", "model"): 1.5,
    ("injection",): 3.0,
    ("contradiction",): 5.0,
}


@dataclass
class Evidence:
    category: str
    label: str
    lr: float
    jurisdiction: str = ""
    detail: str = ""
    source: str = ""

    @property
    def weight(self) -> float:  # natural-log contribution, shown on the card
        return round(math.log(self.lr), 2)


@dataclass
class Assessment:
    p: float
    logit: float
    band: str           # controlled | uncertain | clear
    jurisdiction: str   # ITAR | EAR | EAR99
    classification: str
    program: str
    ledger: list[Evidence] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    register_hit: RegisterHit | None = None

    def to_dict(self):
        d = asdict(self)
        for e, ev in zip(d["ledger"], self.ledger):
            e["weight"] = ev.weight
        return d


def logit(p):
    return math.log(p / (1 - p))


def assess(fp: FingerprintResult | None, rd: Reading) -> Assessment:
    reg = registry()
    cands: list[Evidence] = []
    flags: list[str] = []

    # 1. fingerprint against the vault
    if fp and fp.best and fp.strength != "none":
        b = fp.best
        cands.append(Evidence("vault", f"{fp.strength} copy of vault original {b.part_number}",
                              LR[("vault", fp.strength, b.jurisdiction)], b.jurisdiction,
                              f"{b.inliers} matching points, {b.distinct}x the next-best drawing, coverage {b.score}",
                              "fingerprint"))

    # 2. part numbers on the drawing -> export register
    hit = None
    for pn in sorted(rd.values("part_number")):
        h = reg.lookup_pn(pn)
        if h and (hit is None or LR[("register", h.how, h.jurisdiction)] > LR[("register", hit.how, hit.jurisdiction)]):
            hit = h
    if hit:
        how = {"exact": "on the export register", "normalized": "matches register after fixing look-alike characters",
               "fuzzy": "near-match to the register (suffix or one-character change)"}[hit.how]
        cands.append(Evidence("register", f"part number {hit.seen_as} {how} ({hit.part_number})",
                              LR[("register", hit.how, hit.jurisdiction)], hit.jurisdiction,
                              f"{hit.jurisdiction} {hit.classification}, program {hit.program}", "register"))
        if hit.how in ("normalized", "fuzzy") and hit.jurisdiction in ("ITAR", "EAR"):
            flags.append("ALTERED_PART_NUMBER")

    # 3. what the document itself says (quotes)
    for c in rd.clues:
        src = f"{c.source}: \"{c.quote[:120]}\""
        if c.field == "export_marking" and c.value in ("ITAR", "EAR"):
            cands.append(Evidence("marking", f"{c.value} export marking quoted", LR[("marking", c.value)], c.value, src, c.source))
        elif c.field == "classification":
            kind = "USML" if c.value.startswith("USML") else "ECCN"
            cands.append(Evidence("classification", f"classification {c.value}", LR[("classification", kind)],
                                  "ITAR" if kind == "USML" else "EAR", src, c.source))
        elif c.field == "distribution_statement":
            cands.append(Evidence("distribution", f"distribution statement {c.value}", LR[("distribution", c.value)],
                                  "", src, c.source))
        elif c.field == "program_name":
            prog = reg.programs.get(c.value.upper())
            if prog and prog["jurisdiction"] in ("ITAR", "EAR"):
                cands.append(Evidence("program", f"controlled program named: {c.value}", LR[("program", "controlled")],
                                      prog["jurisdiction"], src, c.source))
        elif c.field == "commercial_marker":
            cands.append(Evidence("commercial", f"commercial marker: {c.value}", LR[("commercial", c.value)],
                                  "EAR99", src, c.source))
        elif c.field == "injection":
            cands.append(Evidence("injection", "document contains instructions aimed at the AI", LR[("injection",)],
                                  "", src, c.source))
            flags.append("INJECTION_ATTEMPT")
        elif c.field == "hint":
            cands.append(Evidence("hint", f"model hint: {c.value}", LR[("hint", "model")], "", src, c.source))

    # 4. contradiction: the original is marked, this copy carries no marking at all
    marked_here = any(e.category in ("marking", "classification") for e in cands)
    original_marked = (fp and fp.best and fp.strength != "none" and fp.best.jurisdiction in ("ITAR", "EAR")) or \
                      (hit is not None and hit.jurisdiction in ("ITAR", "EAR"))
    was_read = rd.transcript_source in ("text-layer", "vision-model") and bool(rd.transcript.strip())
    if original_marked and not marked_here and was_read:
        cands.append(Evidence("contradiction", "original is export-marked but this copy has no marking",
                              LR[("contradiction",)], "", "possible label removal or cropping", "rule"))
        flags.append("MARKING_MISSING")
    elif original_marked and not was_read:
        flags.append("MARKINGS_UNREAD")  # image never read: we can't say whether the label is there
    known_commercial = fp is not None and fp.strength == "strong" and fp.best and fp.best.jurisdiction == "EAR99"
    if not was_read and not original_marked and not known_commercial:
        flags.append("UNREAD")  # fail closed: nothing could read this file and we don't recognise it

    # strongest clue per category; a commercial marker cannot cancel a controlled one
    best: dict[str, Evidence] = {}
    for e in cands:
        k = e.category
        if k not in best or abs(math.log(e.lr)) > abs(math.log(best[k].lr)):
            best[k] = e
    controlled_signal = any(e.lr > 1 and e.category in ("vault", "register", "marking", "classification") for e in best.values())
    ledger = [e for e in best.values() if not (controlled_signal and e.category == "commercial")]
    ledger.sort(key=lambda e: -abs(math.log(e.lr)))

    lg = logit(P0) + sum(math.log(e.lr) for e in ledger)
    p = 1 / (1 + math.exp(-lg))
    band = "controlled" if p >= CONTROLLED_AT else "uncertain" if p >= UNSURE_AT else "clear"

    jur, cls, prog = "EAR99", "EAR99", ""
    for e in ledger:
        if e.lr > 1 and RESTRICTIVENESS.get(e.jurisdiction, 0) > RESTRICTIVENESS[jur]:
            jur = e.jurisdiction
    if band == "clear":
        jur = "EAR99"
    if hit and hit.jurisdiction == jur:
        cls, prog = hit.classification, hit.program
    elif fp and fp.best and fp.strength != "none" and fp.best.jurisdiction == jur:
        cls, prog = fp.best.classification, fp.best.program
    elif jur != "EAR99":
        cls = next((e.label.replace("classification ", "") for e in ledger if e.category == "classification"), jur)
    return Assessment(round(p, 4), round(lg, 3), band, jur, cls, prog, ledger, flags, hit)
