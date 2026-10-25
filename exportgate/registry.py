"""Company records: export register, programs, staff, outside recipients, folder access."""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from rapidfuzz.distance import Levenshtein

from .config import SETTINGS

RESTRICTIVENESS = {"ITAR": 3, "EAR": 2, "EAR99": 1, "": 0}


@dataclass
class RegisterHit:
    part_number: str      # the register entry
    seen_as: str          # what was on the drawing
    how: str              # exact | normalized | fuzzy
    jurisdiction: str
    classification: str
    program: str


@dataclass
class Person:
    email: str
    name: str
    role: str
    us_person: bool
    nationality: str
    cleared_programs: set[str] = field(default_factory=set)


@dataclass
class Recipient:
    email: str
    name: str
    company: str
    country: str
    us_person: bool
    licenses: dict[str, str] = field(default_factory=dict)  # license id -> program
    note: str = ""


def _read(path: Path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _bool(s: str) -> bool:
    return str(s).strip().lower() in ("1", "true", "yes", "y")


_LOOKALIKE = str.maketrans({"O": "0", "Q": "0", "I": "1", "L": "1"})


def normalize_pn(s: str) -> str:
    """KP-1O42 -> KP-1042: fix letter/digit look-alikes in the numeric part (typos and OCR slips)."""
    s = s.upper().replace(" ", "").replace("_", "-")
    prefix, dash, rest = s.partition("-")
    return prefix + dash + rest.translate(_LOOKALIKE) if dash else s


class Registry:
    def __init__(self, company_dir: Path | None = None):
        d = company_dir or SETTINGS.company_dir
        self.register = {r["part_number"].upper(): r for r in _read(d / "export_register.csv")}
        self.programs = {r["program"].upper(): r for r in _read(d / "programs.csv")}
        self.staff = {}
        for r in _read(d / "staff.csv"):
            self.staff[r["email"].lower()] = Person(
                r["email"].lower(), r["name"], r["role"], _bool(r["us_person"]), r["nationality"],
                {p for p in r["cleared_programs"].split(";") if p})
        self.recipients = {}
        for r in _read(d / "recipients.csv"):
            lic = {}
            for item in filter(None, r["licenses"].split(";")):
                lid, _, prog = item.partition(":")
                lic[lid] = prog
            self.recipients[r["email"].lower()] = Recipient(
                r["email"].lower(), r["name"], r["company"], r["country"], _bool(r["us_person"]), lic, r["note"])
        self.sanctioned = {r["country"].upper() for r in _read(d / "sanctioned_countries.csv")}
        self.acl: dict[str, set[str]] = {}
        for r in _read(d / "acl.csv"):
            self.acl.setdefault(r["folder"], set()).add(r["email"].lower())
        self._norm = {normalize_pn(k): k for k in self.register}
        self.controlled_programs = {p for p, r in self.programs.items() if r["jurisdiction"] in ("ITAR", "EAR")}

    # ---- part numbers
    def lookup_pn(self, seen: str) -> RegisterHit | None:
        key = seen.upper().strip()
        if key in self.register:
            return self._hit(key, seen, "exact")
        n = normalize_pn(key)
        if n in self._norm:
            return self._hit(self._norm[n], seen, "normalized")
        # strip a trailing revision / dash suffix: KP-1042B, KP-1042-02
        base = re.sub(r"(-?[A-Z]|-\d{1,2})$", "", n)
        if base != n and base in self._norm:
            return self._hit(self._norm[base], seen, "fuzzy")
        best, bd = None, 99
        for k in self._norm:
            if abs(len(k) - len(n)) <= 1:
                d = Levenshtein.distance(k, n)
                if d < bd:
                    best, bd = k, d
        if best and bd == 1:
            return self._hit(self._norm[best], seen, "fuzzy")
        return None

    def _hit(self, key, seen, how):
        r = self.register[key]
        return RegisterHit(key, seen, how, r["jurisdiction"], r["classification"], r["program"])

    # ---- people
    def person(self, email: str) -> Person | None:
        return self.staff.get((email or "").lower())

    def recipient(self, email: str) -> Recipient | None:
        e = (email or "").lower()
        if e in self.staff:  # internal recipient
            p = self.staff[e]
            return Recipient(p.email, p.name, "Kestrel Precision (internal)", "US" if p.us_person else p.nationality,
                             p.us_person, {}, f"employee, {p.role}")
        return self.recipients.get(e)

    def folder_members(self, folder: str) -> set[str]:
        return self.acl.get(folder, set())


@lru_cache(maxsize=1)
def registry() -> Registry:
    return Registry()
