"""Score Embargo against the answer key and the planted share. Writes RESULTS.md.

  python scripts/eval.py                 # reader from EG_READER (default auto)
  EG_READER=text python scripts/eval.py  # no model at all: fingerprint + register + text layer
  python scripts/eval.py --sweep         # also run the night sweep and score it

Uses its own state folder (data/state_eval) so the demo database stays clean.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("EG_STATE", str(ROOT / "data" / "state_eval"))

from exportgate import llm, store, sweep  # noqa: E402
from exportgate.config import SETTINGS  # noqa: E402
from exportgate.engine import check_outgoing  # noqa: E402
from exportgate.registry import registry  # noqa: E402


def authorized(rec, jur, program) -> bool:
    """Ground truth: may this recipient receive this data with no human review?"""
    if rec is None:
        return jur == "EAR99"
    if rec.country.upper() in registry().sanctioned:
        return False
    if jur == "EAR99":
        return True
    return rec.us_person


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="ignore cached readings")
    ap.add_argument("--out", default="RESULTS.md")
    ap.add_argument("--only", default="", help="comma-separated variants to score, e.g. new_design_scan,split_half")
    a = ap.parse_args()
    store.reset(keep_cache=not a.fresh)
    reg = registry()
    rows = list(csv.DictReader(open(SETTINGS.data_dir / "outgoing" / "answer_key.csv")))
    if a.only:
        rows = [r for r in rows if r["variant"] in a.only.split(",")]
    out = []
    t0 = time.perf_counter()
    calls0 = llm.STATS["calls"]
    for r in rows:
        card = check_outgoing(SETTINGS.data_dir / r["file"], r["requester"], r["recipient"], channel="eval",
                              use_cache=not a.fresh)
        truth = r["true_controlled"] == "1"
        flagged = card["band"] != "clear"
        rec = reg.recipient(r["recipient"])
        prog = reg.register.get(r["source_part"], {}).get("program", "") if r["source_part"] else ""
        auth = authorized(rec, r["true_jurisdiction"], prog)
        leak = (not auth) and card["action"] == "RELEASE"
        out.append(r | {"p": card["p"], "band": card["band"], "action": card["action"], "codes": ";".join(card["codes"]),
                        "pred_jur": card["jurisdiction"], "flagged": flagged, "truth": truth, "authorized": auth,
                        "leak": leak, "reader": card["reading"]["source"], "fp": card["fingerprint"]["strength"],
                        "match": (card["fingerprint"]["best"] or {}).get("part_number", ""),
                        "ms": card["timings"]["total_ms"], "model_calls": card["reading"]["model_calls"]})
        mark = "LEAK" if leak else ("ok" if flagged == truth else "MISS" if truth else "FALSE+")
        print(f"{mark:6} {r['set']:8} {r['variant']:18} p={card['p']:.3f} {card['action']:7} {card['jurisdiction']:5} "
              f"fp={card['fingerprint']['strength']:6} reader={card['reading']['source']:12} {Path(r['file']).name}")
    secs = time.perf_counter() - t0
    tp = sum(1 for o in out if o["flagged"] and o["truth"])
    fp_ = sum(1 for o in out if o["flagged"] and not o["truth"])
    fn = sum(1 for o in out if not o["flagged"] and o["truth"])
    tn = sum(1 for o in out if not o["flagged"] and not o["truth"])
    prec = tp / max(1, tp + fp_)
    rec_ = tp / max(1, tp + fn)
    leaks = [o for o in out if o["leak"]]
    jur_ok = sum(1 for o in out if o["truth"] and o["pred_jur"] == o["true_jurisdiction"])
    by_variant = Counter()
    caught = Counter()
    for o in out:
        if o["truth"]:
            by_variant[o["variant"]] += 1
            caught[o["variant"]] += int(o["flagged"])
    no_model = sum(1 for o in out if o["model_calls"] == 0)
    attacks = [o for o in out if o["set"] == "attack"]

    lines = [
        "# Embargo results",
        "",
        f"Reader mode `{SETTINGS.reader}`, model `{SETTINGS.llm_model}` at `{SETTINGS.llm_base_url}`. "
        f"{len(out)} files, {secs:.1f}s, {llm.STATS['calls'] - calls0} model calls. "
        f"All data is fictional (scripts/make_dataset.py).",
        "",
        "## Detection: is it export-controlled? (flagged = p >= 0.50)",
        "",
        "| | truly controlled | not controlled |",
        "|---|---|---|",
        f"| flagged | {tp} | {fp_} |",
        f"| not flagged | {fn} | {tn} |",
        "",
        f"**Precision {prec:.2f} · recall {rec_:.2f}** · jurisdiction right on {jur_ok}/{tp + fn} controlled files.",
        "",
        "## Safety: controlled data released to someone not authorized",
        "",
        f"**{len(leaks)} leaks** out of {sum(1 for o in out if not o['authorized'])} files going to unauthorized recipients.",
        "",
        "## Caught, by how the file was disguised",
        "",
        "| disguise | caught |",
        "|---|---|",
        *[f"| {v} | {caught[v]}/{n} |" for v, n in sorted(by_variant.items())],
        "",
        "## Attack set (demo)",
        "",
        "| file | disguise | p | decision | rules |",
        "|---|---|---|---|---|",
        *[f"| {Path(o['file']).name} | {o['variant']} | {o['p']:.3f} | {o['action']} | {o['codes']} |" for o in attacks],
        "",
        f"## Cost\n\n{no_model}/{len(out)} files ({100 * no_model / len(out):.0f}%) decided with **no model call** "
        f"(fingerprint + register + PDF text layer). Median time per file "
        f"{sorted(o['ms'] for o in out)[len(out) // 2]:.0f} ms.",
    ]

    if a.sweep:
        res = sweep.run(fresh=a.fresh)
        truth = {r["file"].replace("share/", "", 1): r for r in csv.DictReader(open(SETTINGS.data_dir / "share" / "share_truth.csv"))}
        latest = sweep.latest(limit=500)
        flagged_files = {i["file"] for i in latest["top"]}
        planted = {f: t for f, t in truth.items() if t["planted"]}
        found = {f for f in planted if f in flagged_files}
        false = [f for f in flagged_files if f not in planted]
        s = res["stats"]
        lines += [
            "", "## Night sweep",
            "",
            f"{s['files']} files / {s['pages']} pages in {s['seconds']}s ({s['pages_per_min']} pages/min), "
            f"{s['model_calls']} model calls, {s['resolved_without_model_pct']}% settled without the model.",
            "",
            f"Planted problems found: **{len(found)}/{len(planted)}** ("
            + ", ".join(f"{k.replace('_', ' ')} {sum(1 for f in found if planted[f]['planted'] == k)}/"
                        f"{sum(1 for t in planted.values() if t['planted'] == k)}"
                        for k in sorted({t['planted'] for t in planted.values()})) + "). "
            f"Flagged but not planted: {len(false)}.",
        ]
        if false:
            lines += ["", "Flagged but not planted:", *[f"- `{f}`" for f in false[:15]]]
        missed = [f for f in planted if f not in found]
        if missed:
            lines += ["", "Missed:", *[f"- `{f}` ({planted[f]['planted']})" for f in missed]]

    (ROOT / a.out).write_text("\n".join(lines) + "\n")
    print("\n".join(lines[6:16]))
    print(f"leaks={len(leaks)}  wrote {a.out}")


if __name__ == "__main__":
    main()
