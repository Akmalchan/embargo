"""The gate: analyse a file, decide, record. Also used by the night sweep."""
from __future__ import annotations

import threading
from dataclasses import asdict
from pathlib import Path

import cv2

from . import events, store
from .config import SETTINGS
from .fingerprint import FingerprintResult, Vault
from .fusion import Assessment, assess
from .ingest import load
from .policy import BLOCK, HOLD, RELEASE, check_approval, decide
from .reader import Clue, Reading, read
from .registry import registry

_vault: Vault | None = None
_vault_lock = threading.Lock()


def vault() -> Vault:
    global _vault
    with _vault_lock:
        if _vault is None:
            _vault = Vault.open()
        return _vault


def _reading_from(d: dict) -> Reading:
    rd = Reading(d["transcript"], d["transcript_source"], [Clue(**c) for c in d["clues"]], d.get("dropped", []),
                 d.get("model_calls", 0), d.get("tiles_read", 0), d.get("error", ""))
    return rd


def analyze(path: str | Path, mode: str | None = None, use_cache: bool = True, skip_read_if_commercial=False):
    """Fingerprint + read + fuse. Returns (doc, fingerprint, reading, assessment, timings)."""
    t_all = store.timer()
    mode = mode or SETTINGS.reader
    doc = load(path)
    t = store.timer()
    fp: FingerprintResult = vault().match(doc.pages[0].image)
    timings = {"fingerprint_ms": t()}

    t = store.timer()
    skipped = False
    if mode == "replay":  # stage safety net: reuse readings saved by earlier live runs, never call the model
        cached = next((c for m in ("vlm", "auto") if (c := store.cached_reading(doc.sha256, m))), None)
        timings["replayed"] = bool(cached)
        if not cached:
            rd = read(doc, "text")
    else:
        cached = store.cached_reading(doc.sha256, mode) if use_cache else None
    if mode == "replay" and not cached:
        pass
    elif cached:
        rd = _reading_from(cached)
        rd.model_calls = 0
        timings["read_cached"] = True
    elif skip_read_if_commercial and not doc.has_text_layer and fp.strength == "strong" and fp.best \
            and fp.best.jurisdiction == "EAR99":
        rd = Reading("", "skipped")
        skipped = True
    else:
        rd = read(doc, mode)
        if not rd.error and rd.transcript_source != "none":
            store.cache_reading(doc.sha256, mode, rd.to_dict() | {"transcript": rd.transcript})
    timings["read_ms"] = t()
    a = assess(fp, rd)
    timings["total_ms"] = t_all()
    timings["read_skipped"] = skipped
    return doc, fp, rd, a, timings


def _card(rid, doc, fp, rd, a: Assessment, dec, requester_email, recipient_email, timings):
    reg = registry()
    req = reg.person(requester_email)
    rec = reg.recipient(recipient_email)
    ledger = [{"category": e.category, "label": e.label, "weight": e.weight, "lr": e.lr,
               "jurisdiction": e.jurisdiction, "detail": e.detail, "source": e.source} for e in a.ledger]
    card = {
        "id": rid,
        "file": doc.path.name,
        "sha256": doc.sha256,
        "requester": {"email": requester_email, "name": req.name if req else requester_email,
                      "role": req.role if req else "UNKNOWN"},
        "recipient": {"email": recipient_email, "name": rec.name if rec else recipient_email,
                      "company": rec.company if rec else "unknown", "country": rec.country if rec else "?",
                      "us_person": rec.us_person if rec else None},
        "action": dec.action, "codes": dec.codes, "reasons": dec.reasons, "needs_approval": dec.needs_approval,
        "p": a.p, "logit": a.logit, "band": a.band, "jurisdiction": a.jurisdiction,
        "classification": a.classification, "program": a.program, "flags": a.flags,
        "ledger": ledger,
        "fingerprint": {"strength": fp.strength, "mode": fp.mode, "ms": fp.ms,
                        "best": asdict(fp.best) if fp.best else None,
                        "candidates": [asdict(c) for c in fp.candidates[:3]]},
        "reading": {"source": rd.transcript_source, "clues": [asdict(c) for c in rd.clues],
                    "dropped": rd.dropped, "model_calls": rd.model_calls, "tiles": rd.tiles_read, "error": rd.error,
                    "excerpt": rd.transcript[:600]},
        "timings": timings,
    }
    card["summary"] = summarize(card)
    return card


def summarize(card: dict) -> str:
    verb = {RELEASE: "Released", HOLD: "Held", BLOCK: "Blocked"}[card["action"]]
    r = card["recipient"]
    prefix = {RELEASE: "RELEASE", HOLD: "HOLD", BLOCK: "BLOCK"}[card["action"]]
    lead = [r for c, r in zip(card["codes"], card["reasons"]) if c.startswith(prefix)] or card["reasons"] or [""]
    first = lead[0]
    top = "; ".join(e["label"] for e in card["ledger"][:3])
    s = f"{verb}: {card['file']} to {r['company']} ({r['country']}). {first[:1].upper() + first[1:]}."
    if card["band"] != "clear":
        s += f" {card['jurisdiction']} likelihood {card['p']:.3f}. Evidence: {top}."
    return s


def check_outgoing(path, requester_email: str, recipient_email: str, channel: str = "dashboard",
                   mode: str | None = None, use_cache: bool = True) -> dict:
    reg = registry()
    doc, fp, rd, a, timings = analyze(path, mode, use_cache)
    dec = decide(a, reg.person(requester_email), reg.recipient(recipient_email), recipient_email)
    rid = store.new_request(doc.path.name, str(doc.path), doc.sha256, requester_email.lower(),
                            recipient_email.lower(), {}, dec.action, a.p, a.jurisdiction, channel)
    card = _card(rid, doc, fp, rd, a, dec, requester_email.lower(), recipient_email.lower(), timings)
    store.update_request(rid, card=card)
    store.log("REQUEST", requester_email.lower(), rid,
              {"file": doc.path.name, "sha256": doc.sha256, "recipient": recipient_email.lower(), "channel": channel})
    path = "replayed" if timings.get("replayed") else "cached reading" if timings.get("read_cached") else \
        "no model needed" if rd.transcript_source in ("text-layer", "skipped") else "live model" \
        if rd.transcript_source == "vision-model" else "unread"
    card["path"] = path
    store.update_request(rid, card=card)
    store.log("DECISION", "embargo", rid,
              {"action": dec.action, "codes": dec.codes, "p": a.p, "jurisdiction": a.jurisdiction, "path": path,
               "ledger": [[e["label"], e["weight"]] for e in card["ledger"]]})
    lead = next((r for k, r in zip(dec.codes, dec.reasons) if k.startswith(dec.action)), dec.reasons[0] if dec.reasons else "")
    events.publish("gate", id=rid, file=doc.path.name, action=dec.action, p=a.p, jurisdiction=a.jurisdiction,
                   codes=dec.codes, reason=lead, requester=requester_email.lower(), to=recipient_email.lower(),
                   channel=channel, recipient=card["recipient"]["company"], country=card["recipient"]["country"],
                   timings=timings, match=(fp.best.part_number if fp.best and fp.strength != "none" else ""),
                   inliers=(fp.best.inliers if fp.best and fp.strength != "none" else 0))
    return card


def respond(rid: str, approver_email: str, approve: bool, note: str = "") -> dict:
    reg = registry()
    r = store.get_request(rid)
    if not r:
        return {"ok": False, "code": "NO_SUCH_REQUEST", "reason": f"no request {rid}"}
    approver = reg.person(approver_email)
    chk = check_approval(r["status"], r["requester"], approver)
    store.log("APPROVAL_ATTEMPT", approver_email.lower(), rid,
              {"approve": approve, "allowed": chk.allowed, "code": chk.code, "note": note})
    events.publish("approval", id=rid, approver=approver_email.lower(), allowed=chk.allowed, code=chk.code,
                   approve=approve)
    if not chk.allowed:
        return {"ok": False, "code": chk.code, "reason": chk.reason, "status": r["status"]}
    status = "APPROVED" if approve else "DENIED"
    card = r["card"]
    card["action_final"] = status
    card["approver"] = {"email": approver.email, "name": approver.name}
    store.update_request(rid, status=status, approver=approver.email, closed_at=store.now(), card=card)
    store.log(status, approver.email, rid, {"note": note})
    from .mailgate import release_held  # a held email goes out (or is dropped) with the decision
    release_held(rid, approve)
    return {"ok": True, "code": status, "status": status, "approver": approver.name}


def match_picture(rid: str) -> bytes | None:
    r = store.get_request(rid)
    if not r or not r["card"]["fingerprint"]["best"]:
        return None
    doc = load(r["file_path"])
    pic = vault().match_picture(doc.pages[0].image, r["card"]["fingerprint"]["best"]["part_number"])
    if pic is None:
        return None
    ok, buf = cv2.imencode(".png", pic)
    return buf.tobytes() if ok else None
