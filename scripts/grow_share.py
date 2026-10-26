"""Grow the company share to a realistic size for the night sweep, using every CPU core.

  python scripts/grow_share.py --total 10000

Adds only everyday files (commercial drawings as PDFs and rescans, memos, quotes, invoices) to the open
folders, so every planted problem from make_dataset.py stays the same and the sweep's answer key still holds.
Rescans get fresh scanner noise each time, so they are new files, not copies.
"""
from __future__ import annotations

import argparse
import csv
import os
import random
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_dataset as md  # noqa: E402

OPEN = ["Engineering/Commercial", "Sales/Quotes", "Shop/Travelers", "Shared/Everyone", "Purchasing/RFQ-outbound"]


def parts(seed: int = 7):
    """The same 60 parts make_dataset.build() made (same seed, same draw order)."""
    rng = random.Random(seed)
    out, n = [], 1001
    for jur, count in [("ITAR", 25), ("EAR", 10), ("EAR99", 25)]:
        for _ in range(count):
            if jur == "ITAR":
                prog, cls = rng.choice([("HALCYON", "USML VIII(h)(1)"), ("SKYWARD-7", "USML XII(e)")])
            elif jur == "EAR":
                prog, cls = "OSPREY-L", "ECCN 9A610.x"
            else:
                prog, cls = rng.choice(["TRITON MARINE", "NORTHWIND AG", "CATALOG"]), "EAR99"
            out.append(md.Part(pn=f"KP-{n}", title=rng.choice(md.TITLES), material=rng.choice(md.MATERIALS),
                               finish=rng.choice(md.FINISHES), rev=rng.choice(["A", "A", "B", "C"]), program=prog,
                               jurisdiction=jur, classification=cls, seed=rng.randint(1, 10**9),
                               drawn=rng.choice(md.DRAFTERS), date=f"2026-0{rng.randint(1,9)}-{rng.randint(10,28)}"))
            n += 1
    return out


def one(i: int):
    rng = random.Random(1_000_003 * i + 17)
    com = [p for p in parts() if p.jurisdiction == "EAR99"]
    folder = rng.choice(OPEN)
    r = rng.random()
    if r < 0.45:
        p = rng.choice(com)
        name = f"{p.pn}_scan_{i:05d}.jpg"
        md.save_image(md.v_rescan(md.render(p), rng), md.DATA / "share" / folder / name)
        row = (folder, name, "drawing", p.pn, "EAR99")
    elif r < 0.65:
        p = rng.choice(com)
        name = f"{p.pn}_rev{p.rev}_{i:05d}.pdf"
        shutil.copy(md.DATA / "vault" / f"{p.pn}_rev{p.rev}.pdf", md.DATA / "share" / folder / name)
        row = (folder, name, "drawing", p.pn, "EAR99")
    else:
        kind = rng.choice(["quote", "memo", "invoice", "inspection", "safety", "traveler", "po"])
        name = f"{kind}_{i:05d}.pdf"
        md.make_text_pdf(md.DATA / "share" / folder / name, kind.upper(),
                         [rng.choice(md.MEMO_LINES).format(n=rng.randint(100, 999)) for _ in range(rng.randint(2, 6))])
        row = (folder, name, "document", "", "EAR99")
    f, n, k, pn, j = row
    return {"file": f"share/{f}/{n}", "folder": f, "kind": k, "source_part": pn, "true_jurisdiction": j, "planted": ""}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--total", type=int, default=10000)
    a = ap.parse_args()
    truth_p = md.DATA / "share" / "share_truth.csv"
    truth = list(csv.DictReader(open(truth_p)))
    need = a.total - len(truth)
    if need <= 0:
        print(f"share already has {len(truth)} files")
        return
    start = 50_000 + len(truth)
    with ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 2) - 2)) as ex:
        rows = list(ex.map(one, range(start, start + need), chunksize=32))
    with open(truth_p, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=list(truth[0].keys())).writerows(rows)
    print(f"share: {len(truth)} -> {len(truth) + len(rows)} files")


if __name__ == "__main__":
    main()
