"""The company as a map, a simulated workday, and "Ask Embargo".

graph():     people, folders, exit channels and outside parties, plus who can open what,
             what the last night sweep found, and the latest attempts to send something out.
simulate():  a stream of real checks through the real engine (normal files and sneak attempts),
             across every exit channel, so the console runs like an operations centre.
ask():       answers questions from the log and the sweep. Facts come from the database;
             the local model (if running) only rephrases them, and every answer cites records.
"""
from __future__ import annotations

import csv
import json
import os
import random
import re
import threading
import time

from . import events, llm, store, sweep
from .config import SETTINGS
from .engine import check_outgoing
from .registry import registry

CHANNELS = [
    ("email", "Email"), ("portal", "Upload portal"), ("usb", "USB drive"), ("slack", "Slack"), ("print", "Printer"),
]
CHANNEL_IDS = {c for c, _ in CHANNELS}
SIM = {"running": False, "done": 0, "total": 0, "started": 0.0}
_sim_lock = threading.Lock()


def channel_of(raw: str) -> str:
    raw = (raw or "").lower()
    if raw.startswith("slack"):
        return "slack"
    if raw in CHANNEL_IDS:
        return raw
    return {"drop": "email", "dashboard": "portal", "eval": "portal", "check": "portal"}.get(raw, "portal")


# ------------------------------------------------------------------ the map

def graph() -> dict:
    reg = registry()
    people = [{"id": p.email, "name": p.name, "role": p.role, "us_person": p.us_person,
               "cleared": sorted(p.cleared_programs)} for p in reg.staff.values()]
    folders = [{"id": f, "members": sorted(m)} for f, m in reg.acl.items()]
    outside = [{"id": r.email, "name": r.name, "company": r.company, "country": r.country, "us_person": r.us_person,
                "sanctioned": r.country.upper() in reg.sanctioned} for r in reg.recipients.values()]
    exposures = {}
    latest = sweep.latest(limit=200)
    if latest:
        for it in latest["top"]:
            e = exposures.setdefault(it["folder"], {"files": 0, "people": set()})
            e["files"] += 1
            e["people"].update(u["email"] for u in it["detail"]["unauthorized"])
    flows = []
    for r in store.list_requests(40):
        c = r["card"] or {}
        flows.append({"id": r["id"], "from": r["requester"], "to": r["recipient"], "channel": channel_of(r["channel"]),
                      "status": r["status"], "file": r["file_name"], "p": r["p"]})
    return {"people": people, "folders": folders, "channels": [{"id": c, "name": n} for c, n in CHANNELS],
            "outside": outside, "flows": flows,
            "exposures": {f: {"files": v["files"], "people": sorted(v["people"])} for f, v in exposures.items()},
            "sweep": (latest or {}).get("stats")}


# ------------------------------------------------------------------ a simulated workday

def _pool():
    rows = list(csv.DictReader(open(SETTINGS.data_dir / "outgoing" / "answer_key.csv")))
    normal = [r for r in rows if r["set"] == "outgoing"]
    attacks = [r for r in rows if r["set"] == "attack"]
    return normal, attacks


def simulate(n: int = 24, seconds: float = 60.0, attack_share: float = 0.3, seed: int | None = None) -> dict:
    with _sim_lock:
        if SIM["running"]:
            return {"ok": False, "message": "a workday is already running"}
        SIM.update(running=True, done=0, total=n, started=time.time())
    rng = random.Random(seed if seed is not None else int(time.time()))
    normal, attacks = _pool()
    weights = [("email", 40), ("portal", 25), ("usb", 15), ("slack", 10), ("print", 10)]
    chans = [c for c, w in weights for _ in range(w)]

    def run():
        events.publish("sim_start", total=n)
        store.log("WORKDAY_START", "simulator", "", {"attempts": n})
        try:
            for i in range(n):
                r = rng.choice(attacks if rng.random() < attack_share else normal)
                ch = rng.choice(chans)
                try:
                    check_outgoing(SETTINGS.data_dir / r["file"], r["requester"], r["recipient"], channel=ch)
                except Exception as e:  # keep the day going
                    store.log("SIM_ERROR", "simulator", "", {"file": r["file"], "error": str(e)[:160]})
                with _sim_lock:
                    SIM["done"] = i + 1
                time.sleep(max(0.2, seconds / n * rng.uniform(0.5, 1.5)))
        finally:
            with _sim_lock:
                SIM["running"] = False
            events.publish("sim_done", total=n)
    threading.Thread(target=run, daemon=True, name="workday").start()
    return {"ok": True, "total": n}


# ------------------------------------------------------------------ Ask Embargo

def _since(text: str) -> str:
    t = text.lower()
    days = 7 if "week" in t else 30 if "month" in t else 1 if ("today" in t or "yesterday" in t) else 3650
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - days * 86400))


SHORT = {
    "BLOCK_ITAR_NO_LICENSE": "ITAR data, no export license", "BLOCK_SANCTIONED_DESTINATION": "sanctioned country",
    "HOLD_LICENSE_CHECK": "license scope to confirm", "HOLD_EAR_LICENSE_CHECK": "EAR license check",
    "HOLD_UNCERTAIN": "possibly controlled", "HOLD_UNKNOWN_RECIPIENT": "unknown recipient",
    "HOLD_MARKING_MISSING": "label removed", "HOLD_MARKINGS_UNREAD": "copy of a controlled drawing, label unreadable",
    "HOLD_ALTERED_PART_NUMBER": "altered part number", "HOLD_UNREADABLE": "unreadable file",
    "HOLD_REQUESTER_NOT_CLEARED": "sender not cleared", "HOLD_INJECTION_ATTEMPT": "tried to instruct the AI",
    "RELEASE_NOT_CONTROLLED": "not controlled", "RELEASE_AUTHORIZED_RECIPIENT": "authorized US recipient",
}
WORD = {"BLOCK": "blocked", "HOLD": "held", "RELEASE": "released", "APPROVED": "approved", "DENIED": "denied"}


def _req_line(r) -> str:
    c = r["card"] or {}
    codes = c.get("codes", [])
    lead = next((k for k in codes if k.startswith(r["status"][:4])), codes[0] if codes else "")
    rec = c.get("recipient", {})
    who = c.get("requester", {}).get("name", r["requester"])
    return f"{r['file_name']} · {who} → {rec.get('company', r['recipient'])} ({rec.get('country', '?')}) · " \
           f"{WORD.get(r['status'], r['status'].lower())}: {SHORT.get(lead, lead.replace('_', ' ').lower())}"


def ask(q: str, use_model: bool = True) -> dict:
    reg = registry()
    t = q.strip().lower()
    cites: list[str] = []
    facts: list[str] = []
    c = store.conn()

    def requests_where(sql, args=()):
        return [dict(x) | {"card": json.loads(x["card"]) if x["card"] else {}} for x in
                c.execute(f"select * from requests where {sql} order by created desc limit 50", args).fetchall()]

    folder = next((f for f in reg.acl if f.lower().split("/")[-1] in t or f.lower() in t), None)
    program = next((p for p in reg.programs if p.lower() in t and p != "CATALOG"), None)
    company = next((r for r in reg.recipients.values() if r.company.lower().split()[0] in t or r.country.lower() == t.strip()), None)

    if re.search(r"\b(who|which people)\b.*\b(open|access|see|read)\b", t) and (folder or program):
        target = [folder] if folder else [f for f in reg.acl if program and program.lower() in f.lower()] or list(reg.acl)
        for f in target:
            members = sorted(reg.folder_members(f))
            bad = [m for m in members if not reg.person(m) or not reg.person(m).us_person or
                   (program and program not in reg.person(m).cleared_programs and reg.person(m).role != "EMPOWERED_OFFICIAL")]
            facts.append(f"{f}: {len(members)} people can open it" + (f"; {len(bad)} are not authorized for this data: "
                         + ", ".join(reg.person(b).name if reg.person(b) else b for b in bad) if bad else "; all authorized."))
            cites.append(f"folder:{f}")
        kind = "access"
    elif re.search(r"expos|sweep|risk|wrong (people|folder)|leak", t):
        latest = sweep.latest(limit=8)
        if not latest:
            facts.append("No night sweep has run yet.")
        else:
            s = latest["stats"]
            facts.append(f"Last sweep read {s['files']} files and found {s['flagged']} exposed controlled files; "
                         f"{s['people_exposed']} people can see something they shouldn't.")
            for it in latest["top"][:5]:
                facts.append(f"{it['file']} in {it['folder']}: {it['jurisdiction']}, {it['unauthorized']} of {it['readers']} readers not authorized")
                cites.append(f"sweep:{latest['run_id']}:{it['file']}")
        kind = "sweep"
    else:
        status = ("BLOCK" if re.search(r"block|stop|denied|caught", t) else "HOLD" if re.search(r"held|hold|wait|pending|sign", t)
                  else "RELEASE" if re.search(r"releas|left|went out|sent|allowed|leav", t) else None)
        where, args = ["created >= ?"], [_since(t)]
        if status == "RELEASE":
            where.append("status in ('RELEASE','APPROVED')")
        elif status:
            where.append("status = ?")
            args.append(status)
        if company:
            where.append("recipient = ?")
            args.append(company.email)
        for ch, name in CHANNELS:
            if ch in t or name.lower() in t:
                where.append("channel like ?")
                args.append(f"{ch}%")
        rows = requests_where(" and ".join(where), tuple(args))
        label = {"BLOCK": "blocked", "HOLD": "waiting for sign-off", "RELEASE": "released"}.get(status, "checked")
        facts.append(f"{len(rows)} files {label}" + (f" to {company.company}" if company else "") + ".")
        for r in rows[:6]:
            facts.append(_req_line(r))
            cites.append(r["id"])
        kind = "requests"

    answer = "\n".join(facts)
    source = "from the log"
    # EG_ASK_LLM=1: answer with a local text model even when drawings are read without a vision model
    if use_model and (SETTINGS.reader != "text" or os.environ.get("EG_ASK_LLM") == "1"):
        try:
            said = llm._post([{"role": "user", "content":
                "You are Embargo, a compliance assistant. Answer the question in 2-3 plain sentences using ONLY these facts. "
                "Do not add anything that is not in the facts.\n\nQUESTION: " + q + "\n\nFACTS:\n" + "\n".join(facts)}],
                max_tokens=180, temperature=0.1)
            if said and said.strip():   # only credit the model when it actually answered
                answer, source = said.strip(), "local model, from the log"
        except llm.LLMUnavailable:
            pass
    store.log("ASK", "officer", "", {"q": q[:200], "kind": kind, "cites": cites[:10]})
    return {"q": q, "answer": answer, "facts": facts, "cites": cites, "kind": kind, "source": source}
