"""Night sweep: read every file on the share, find controlled data in the wrong place.

Exposure score per file f:
    E_f = p_f * w_j * (ALPHA * u_f + BETA * c_f)
      p_f  probability the file is controlled (fusion.py)
      w_j  jurisdiction weight: ITAR 1.0, EAR 0.6, EAR99 0.05
      u_f  share of people who can open the folder and are NOT authorized
           (foreign person, or not cleared for the program)
      c_f  1 if it is an unmarked copy of a marked original, 0.5 if its markings couldn't be read, else 0
A controlled file in the right folder, properly marked, scores 0: nothing is wrong with it.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import events, llm, store
from .config import SETTINGS
from .engine import analyze
from .ingest import SUPPORTED_EXT
from .registry import registry

ALPHA, BETA = 2.0, 1.0
W_J = {"ITAR": 1.0, "EAR": 0.6, "EAR99": 0.05}
FLAG_AT = 0.3

PROGRESS = {"running": False, "run_id": None, "done": 0, "total": 0, "started": 0.0, "model_calls": 0,
            "pages": 0, "current": ""}
_lock = threading.Lock()


def share_dir() -> Path:
    return SETTINGS.data_dir / "share"


def list_files(root: Path | None = None) -> list[Path]:
    root = root or share_dir()
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXT
                  and "_restricted" not in p.parts)


def unauthorized_share(folder: str, program: str, jurisdiction: str):
    reg = registry()
    members = reg.folder_members(folder)
    if not members:
        return 0.0, 0, []
    bad = []
    for e in members:
        p = reg.person(e)
        if p is None:
            bad.append(e)
            continue
        if jurisdiction in ("ITAR", "EAR") and not p.us_person:
            bad.append(e)
        elif jurisdiction in ("ITAR", "EAR") and program and program not in p.cleared_programs \
                and p.role != "EMPOWERED_OFFICIAL":
            bad.append(e)
    return len(bad) / len(members), len(members), bad


def score_file(path: Path, root: Path):
    folder = str(path.parent.relative_to(root))
    doc, fp, rd, a, timings = analyze(path, skip_read_if_commercial=True)
    u, n_readers, bad = unauthorized_share(folder, a.program, a.jurisdiction)
    # 1 = label removed; 0.5 = copy of a marked original whose label we couldn't read (unverified)
    c = 1.0 if "MARKING_MISSING" in a.flags else 0.5 if "MARKINGS_UNREAD" in a.flags else 0.0
    E = a.p * W_J.get(a.jurisdiction, 0.05) * (ALPHA * u + BETA * c)
    reg = registry()
    detail = {
        "ledger": [[e.label, e.weight] for e in a.ledger], "flags": a.flags, "program": a.program,
        "classification": a.classification,
        "unauthorized": [{"email": b, "name": (reg.person(b).name if reg.person(b) else b),
                          "why": ("foreign person" if reg.person(b) and not reg.person(b).us_person else "not cleared")}
                         for b in bad][:12],
        "match": fp.best.part_number if fp.best and fp.strength != "none" else "",
        "reader": rd.transcript_source, "model_calls": rd.model_calls, "pages": len(doc.pages), "ms": timings["total_ms"],
    }
    events.publish("sweep_file", file=str(path.relative_to(root)), folder=folder, p=a.p, jurisdiction=a.jurisdiction,
                   exposure=round(E, 4), flags=a.flags, match=detail["match"],
                   inliers=fp.best.inliers if fp.best and fp.strength != "none" else 0,
                   distinct=fp.best.distinct if fp.best and fp.strength != "none" else 0, strength=fp.strength,
                   reader=rd.transcript_source, ms=timings["total_ms"], fp_ms=fp.ms, unauthorized=len(bad),
                   readers=n_readers, model_calls=rd.model_calls, clues=[f"{c.field}:{c.value}" for c in rd.clues][:6])
    return {"file": str(path.relative_to(root)), "folder": folder, "p": a.p, "jurisdiction": a.jurisdiction,
            "exposure": round(E, 4), "unauthorized": len(bad), "readers": n_readers, "unmarked_copy": c,
            "detail": detail}


def run(root: Path | None = None, limit: int | None = None, fresh: bool = False, workers: int | None = None,
        shuffle: bool = False) -> dict:
    root = root or share_dir()
    files = list_files(root)
    if shuffle:  # demo mode: a realistic mix instead of folder order
        import random
        random.Random(int(time.time())).shuffle(files)
    files = files[:limit] if limit else files
    rid = "SW-" + uuid.uuid4().hex[:6].upper()
    with _lock:
        if PROGRESS["running"]:
            return {"ok": False, "message": "a sweep is already running"}
        PROGRESS.update(running=True, run_id=rid, done=0, total=len(files), started=time.time(), model_calls=0,
                        pages=0, current="")
    if fresh:
        store.conn().execute("delete from read_cache")
    calls0, tok0 = llm.STATS["calls"], llm.STATS["completion_tokens"]
    started = store.now()
    store.conn().execute("insert into sweep_runs values (?,?,?,?)", (rid, started, None, "{}"))
    store.log("SWEEP_START", "scheduler", rid, {"files": len(files), "root": str(root.name)})
    events.publish("sweep_start", run_id=rid, total=len(files))
    items, errors = [], []
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers or SETTINGS.sweep_workers) as ex:
        futs = {ex.submit(score_file, f, root): f for f in files}
        for fut in as_completed(futs):
            f = futs[fut]
            try:
                it = fut.result()
                items.append(it)
                store.conn().execute(
                    "insert into sweep_items values (?,?,?,?,?,?,?,?,?,?,0)",
                    (rid, it["file"], it["folder"], it["p"], it["jurisdiction"], it["exposure"], it["unauthorized"],
                     it["readers"], it["unmarked_copy"], json.dumps(it["detail"])))
                with _lock:
                    PROGRESS["pages"] += it["detail"]["pages"]
            except Exception as e:  # keep sweeping; report at the end
                errors.append({"file": str(f), "error": str(e)[:200]})
            with _lock:
                PROGRESS["done"] += 1
                PROGRESS["current"] = f.name
                PROGRESS["model_calls"] = llm.STATS["calls"] - calls0
    secs = time.perf_counter() - t0
    flagged = sorted((i for i in items if i["exposure"] >= FLAG_AT), key=lambda i: -i["exposure"])
    pages = sum(i["detail"]["pages"] for i in items)
    no_model = sum(1 for i in items if i["detail"]["reader"] in ("text-layer", "skipped", "none"))
    stats = {
        "files": len(items), "pages": pages, "errors": errors[:20], "seconds": round(secs, 1),
        "files_per_min": round(len(items) / secs * 60, 1) if secs else 0,
        "pages_per_min": round(pages / secs * 60, 1) if secs else 0,
        "model_calls": llm.STATS["calls"] - calls0, "completion_tokens": llm.STATS["completion_tokens"] - tok0,
        "resolved_without_model_pct": round(100 * no_model / max(1, len(items)), 1),
        "flagged": len(flagged),
        "controlled_open": sum(1 for i in flagged if i["unauthorized"] > 0 and i["unmarked_copy"] < 1),
        "unmarked_copies": sum(1 for i in flagged if i["unmarked_copy"] == 1),
        "markings_unverified": sum(1 for i in flagged if i["unmarked_copy"] == 0.5),
        "people_exposed": len({u["email"] for i in flagged for u in i["detail"]["unauthorized"]}),
    }
    store.conn().execute("update sweep_runs set finished = ?, stats = ? where id = ?", (store.now(), json.dumps(stats), rid))
    store.log("SWEEP_DONE", "scheduler", rid, {k: v for k, v in stats.items() if k != "errors"} |
              {"top": [[i["file"], i["exposure"]] for i in flagged[:10]]})
    with _lock:
        PROGRESS["running"] = False
    events.publish("sweep_done", run_id=rid, stats={k: v for k, v in stats.items() if k != "errors"})
    return {"ok": True, "run_id": rid, "stats": stats, "top": flagged[:10]}


def latest(limit: int = 25) -> dict | None:
    c = store.conn()
    r = c.execute("select * from sweep_runs where finished is not null order by started desc limit 1").fetchone()
    if not r:
        return None
    rows = c.execute("select * from sweep_items where run_id = ? and exposure >= ? order by exposure desc limit ?",
                     (r["id"], FLAG_AT, limit)).fetchall()
    top = [dict(x) | {"detail": json.loads(x["detail"])} for x in rows]
    return {"run_id": r["id"], "started": r["started"], "finished": r["finished"], "stats": json.loads(r["stats"]),
            "top": top}


def digest_text(res: dict) -> str:
    s = res["stats"]
    lines = [f"*Night sweep*: read {s['files']} files ({s['pages']} pages) in {s['seconds']}s, "
             f"{s['pages_per_min']} pages/min. {s['resolved_without_model_pct']}% settled without the model.",
             f"*{s['controlled_open']}* controlled drawings open to people who shouldn't see them, "
             f"*{s['unmarked_copies']}* unmarked copies of marked originals, "
             f"*{s['people_exposed']}* people exposed."]
    for i in res["top"][:8]:
        d = i["detail"]
        why = "unmarked copy" if i["unmarked_copy"] == 1 else f"{i['unauthorized']}/{i['readers']} readers not authorized"
        lines.append(f"• `{i['file']}`: {i['jurisdiction']} p={i['p']:.2f}, {why}, exposure {i['exposure']:.2f}")
    return "\n".join(lines)


def quarantine(run_id: str, file: str, actor: str) -> dict:
    root = share_dir()
    src = root / file
    if not src.exists():
        return {"ok": False, "message": "file not found (already moved?)"}
    dst = root / "_restricted" / file
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), dst)
    store.conn().execute("update sweep_items set quarantined = 1 where run_id = ? and file = ?", (run_id, file))
    store.log("QUARANTINE", actor, run_id, {"file": file, "to": str(dst.relative_to(root))})
    return {"ok": True, "message": f"moved to {dst.relative_to(root)}"}
