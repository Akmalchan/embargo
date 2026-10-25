"""Slack front door (Socket Mode: outbound websocket only, no public URL needed).

In the channel:
  upload a file with the message   to: hans.weber@mueller-praezision.example
  /gate iam marco.bellini@kestrelprecision.example   link your Slack user to a staff record
  /gate send A01_bracket_views_only.png to hans.weber@mueller-praezision.example   (demo files)
  /gate sweep [limit]   /gate verify   /gate tamper   /gate egress   /gate whoami

Run:  SLACK_BOT_TOKEN=xoxb-... SLACK_APP_TOKEN=xapp-... python -m exportgate.slack_app
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError

from . import engine, llm, store, sweep, system
from .config import SETTINGS
from .registry import registry

app = App(token=SETTINGS.slack_bot_token or "xoxb-missing", token_verification_enabled=bool(SETTINGS.slack_bot_token))
MAP_FILE = SETTINGS.state_dir / "slack_users.json"
INBOX = SETTINGS.state_dir / "inbox"
INBOX.mkdir(parents=True, exist_ok=True)
TO_RX = re.compile(r"\bto:?\s*<?(?:mailto:)?([\w.+-]+@[\w.-]+)(?:\|[^>]*)?>?", re.I)
COLORS = {"BLOCK": "#b42318", "HOLD": "#b54708", "RELEASE": "#067647", "APPROVED": "#067647", "DENIED": "#b42318"}


def _map() -> dict:
    try:
        return json.loads(MAP_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def staff_email(slack_user: str) -> str | None:
    return _map().get(slack_user)


def set_staff(slack_user: str, email: str):
    m = _map()
    m[slack_user] = email.lower()
    MAP_FILE.write_text(json.dumps(m, indent=1))


# ------------------------------------------------------------------ card

def card_blocks(card: dict, status: str | None = None) -> list[dict]:
    status = status or card["action"]
    r = card["recipient"]
    verb = {"BLOCK": "BLOCKED", "HOLD": "HELD for approval", "RELEASE": "RELEASED", "APPROVED": "APPROVED and released",
            "DENIED": "DENIED"}[status]
    ledger = "\n".join(f"`{e['weight']:+.2f}`  {e['label']}" for e in card["ledger"][:6]) or "_no evidence either way_"
    fp = card["fingerprint"]
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{verb}: {card['file']}"[:150]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": card["summary"][:2900]}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Control*\n{card['jurisdiction']} {card['classification']}"},
            {"type": "mrkdwn", "text": f"*Likelihood*\np = {card['p']:.3f}"},
            {"type": "mrkdwn", "text": f"*From*\n{card['requester']['name']}"},
            {"type": "mrkdwn", "text": f"*To*\n{r['name']}, {r['company']} ({r['country']})"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Evidence ledger* (ln LR)\n{ledger}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"rules: {', '.join(card['codes'])} · fingerprint {fp['strength']}"
            f"{' to ' + fp['best']['part_number'] if fp['best'] and fp['strength'] != 'none' else ''}"
            f" · read via {card['reading']['source']} · {card['timings']['total_ms']} ms · {card['id']} · on this machine only"}]},
    ]
    if status == "HOLD":
        blocks.append({"type": "actions", "block_id": f"act_{card['id']}", "elements": [
            {"type": "button", "style": "primary", "action_id": "eg_approve", "value": card["id"],
             "text": {"type": "plain_text", "text": "Approve release"}},
            {"type": "button", "style": "danger", "action_id": "eg_deny", "value": card["id"],
             "text": {"type": "plain_text", "text": "Deny"}},
            {"type": "button", "action_id": "eg_why", "value": card["id"], "text": {"type": "plain_text", "text": "Why?"}},
        ]})
    else:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "action_id": "eg_why", "value": card["id"], "text": {"type": "plain_text", "text": "Why?"}}]})
    return blocks


def deliver(method: str, queue: bool = True, **kwargs):
    """Call Slack; if the network is down (wifi off), keep the message in the outbox for later.
    The decision itself never waits for Slack: it is already made and logged on this machine."""
    try:
        return getattr(app.client, method)(**kwargs)
    except SlackApiError as e:  # Slack answered, but said no (bad channel, etc.): don't retry forever
        store.log("SLACK_ERROR", "slack", "", {"method": method, "error": str(e)[:200]})
        return None
    except Exception as e:  # no route to Slack
        if queue:
            store.outbox_add(method, kwargs, str(e))
        return None


def post_card(client, channel, card, thread_ts=None):
    res = deliver("chat_postMessage", queue=False, channel=channel, thread_ts=thread_ts,
                  text=card["summary"][:300], blocks=card_blocks(card))
    if res:  # if not, the relay loop posts it when Slack is reachable again
        store.update_request(card["id"], channel=channel, message_ts=res["ts"])
    return res


def run_check(client, channel, path: Path, requester: str, recipient: str, thread_ts=None):
    deliver("chat_postMessage", channel=channel, thread_ts=thread_ts,
            text=f"Checking `{path.name}` for {recipient} on this machine…")
    card = engine.check_outgoing(path, requester, recipient, channel=f"slack:{channel}")
    post_card(client, channel, card, thread_ts)


# ------------------------------------------------------------------ uploads

@app.event("message")
def on_message(event, client, logger):
    files = event.get("files") or []
    if not files or event.get("bot_id"):
        return
    channel, user = event["channel"], event.get("user", "")
    requester = staff_email(user)
    if not requester:
        client.chat_postEphemeral(channel=channel, user=user,
                                  text="Link yourself first: `/gate iam your.name@kestrelprecision.example`")
        return
    m = TO_RX.search(event.get("text") or "")
    if not m:
        client.chat_postEphemeral(channel=channel, user=user, text="Add who it's going to, e.g. `to: hans.weber@mueller-praezision.example`")
        return
    for f in files:
        url = f.get("url_private_download") or f.get("url_private")
        dst = INBOX / f"{uuid.uuid4().hex[:8]}_{Path(f.get('name', 'upload')).name}"
        r = httpx.get(url, headers={"Authorization": f"Bearer {SETTINGS.slack_bot_token}"}, follow_redirects=True, timeout=60)
        dst.write_bytes(r.content)
        threading.Thread(target=run_check, args=(client, channel, dst, requester, m.group(1), event.get("ts")),
                         daemon=True).start()


# ------------------------------------------------------------------ buttons

def _respond(ack, body, client, approve: bool):
    ack()
    rid = body["actions"][0]["value"]
    user = body["user"]["id"]
    channel = body["channel"]["id"]
    email = staff_email(user)
    if not email:
        client.chat_postEphemeral(channel=channel, user=user, text="Link yourself first: `/gate iam you@kestrelprecision.example`")
        return
    res = engine.respond(rid, email, approve)
    who = registry().person(email)
    name = who.name if who else email
    if not res["ok"]:
        client.chat_postMessage(channel=channel, thread_ts=body["message"]["ts"],
                                text=f":no_entry: *Refused* for {name}: {res['reason']} (`{res['code']}`). Logged.")
        return
    r = store.get_request(rid)
    client.chat_update(channel=channel, ts=body["message"]["ts"], text=r["card"]["summary"][:300],
                       blocks=card_blocks(r["card"], res["status"]))
    client.chat_postMessage(channel=channel, thread_ts=body["message"]["ts"],
                            text=f"{res['status'].capitalize()} by {name} (Empowered Official). Logged in the hash chain.")


@app.action("eg_approve")
def on_approve(ack, body, client):
    _respond(ack, body, client, True)


@app.action("eg_deny")
def on_deny(ack, body, client):
    _respond(ack, body, client, False)


@app.action("eg_why")
def on_why(ack, body, client):
    ack()
    rid = body["actions"][0]["value"]
    r = store.get_request(rid)
    card = r["card"]
    try:
        text = llm.explain({k: card[k] for k in ("action", "reasons", "p", "jurisdiction", "ledger", "recipient", "file")})
        src = "local model"
    except llm.LLMUnavailable:
        text, src = card["summary"], "template"
    client.chat_postMessage(channel=body["channel"]["id"], thread_ts=body["message"]["ts"], text=f"{text}\n_({src}, on this machine)_")


# ------------------------------------------------------------------ /gate

@app.command("/gate")
def gate_cmd(ack, command, client, respond):
    ack()
    text = (command.get("text") or "").strip()
    user, channel = command["user_id"], command["channel_id"]
    parts = text.split()
    verb = parts[0].lower() if parts else "help"
    if verb == "iam" and len(parts) > 1:
        email = parts[1].strip("<>").replace("mailto:", "").split("|")[0]
        p = registry().person(email)
        if not p:
            respond(f"{email} is not in the staff list.")
            return
        set_staff(user, email)
        store.log("SLACK_LINK", email, user, {"slack_user": user})
        respond(f"Linked you to {p.name} ({p.role}).")
    elif verb == "whoami":
        e = staff_email(user)
        p = registry().person(e) if e else None
        respond(f"You are {p.name} ({p.role})." if p else "Not linked. `/gate iam you@kestrelprecision.example`")
    elif verb == "send" and len(parts) >= 3:
        requester = staff_email(user)
        if not requester:
            respond("Link yourself first: `/gate iam you@kestrelprecision.example`")
            return
        name = parts[1]
        m = TO_RX.search(text)
        hits = [p for p in SETTINGS.data_dir.rglob(name) if "state" not in p.parts]
        if not hits or not m:
            respond("Usage: `/gate send <test file name> to <email>`")
            return
        threading.Thread(target=run_check, args=(client, channel, hits[0], requester, m.group(1)), daemon=True).start()
    elif verb == "sweep":
        limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        respond(f"Night sweep started on this machine ({'all' if not limit else limit} files)…")
        threading.Thread(target=post_sweep, args=(client, channel, limit), daemon=True).start()
    elif verb == "verify":
        v = store.verify()
        respond(("Log intact: " if v["ok"] else ":rotating_light: ") + v["message"])
    elif verb == "tamper":
        respond(store.tamper()["message"] + ". Now run `/gate verify`.")
    elif verb == "egress":
        r = system.egress_test()
        respond(("Internet blocked from the sandbox: " if r["blocked"] else ":warning: ") + r["message"])
    else:
        respond("`/gate iam <email>` · `/gate send <file> to <email>` · `/gate sweep [n]` · `/gate verify` · "
                "`/gate egress` · or upload a file with `to: <email>`")


def post_sweep(client, channel, limit=None):
    res = sweep.run(limit=limit)
    if not res.get("ok"):
        deliver("chat_postMessage", channel=channel, text=res.get("message", "sweep failed"))
        return
    deliver("chat_postMessage", channel=channel, text=sweep.digest_text(res))


STARTED = store.now()


def relay_loop(channel: str):
    """Every 10 s: resend anything Slack missed, and post cards for checks made outside Slack
    (dashboard, drop folder) since this bot started."""
    while True:
        try:
            for m in store.outbox_pending():
                try:
                    getattr(app.client, m["kind"])(**m["payload"])
                    store.outbox_done(m["id"], True)
                except SlackApiError as e:
                    store.outbox_done(m["id"], True, str(e))  # Slack refused it; retrying won't help
                except Exception as e:
                    store.outbox_done(m["id"], False, str(e))
                    break  # still offline; try again next round
            rows = store.conn().execute(
                "select id from requests where message_ts is null and channel not like 'slack:%' and created >= ? "
                "order by created limit 20", (STARTED,)).fetchall()
            for r in rows:
                req = store.get_request(r["id"])
                if req and req["card"]:
                    if not post_card(app.client, channel, req["card"]):
                        break
        except Exception as e:
            store.log("SLACK_RELAY_ERROR", "slack", "", {"error": str(e)[:200]})
        time.sleep(10)


def digest_loop(channel: str):
    """Our own scheduler (NemoClaw's cron is skipped with local models)."""
    last = None
    while True:
        now = datetime.now()
        if now.strftime("%H:%M") == SETTINGS.digest_time and last != now.date():
            last = now.date()
            try:
                post_sweep(app.client, channel)
            except Exception as e:  # never kill the loop
                store.log("SWEEP_ERROR", "scheduler", "", {"error": str(e)[:200]})
        time.sleep(20)


def main():
    if not (SETTINGS.slack_bot_token and SETTINGS.slack_app_token):
        raise SystemExit("Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN in the environment.")
    ch = SETTINGS.slack_channel
    if not ch.startswith(("C", "G")):
        for c in app.client.conversations_list(types="public_channel,private_channel", limit=500)["channels"]:
            if c["name"] == ch.lstrip("#"):
                ch = c["id"]
    threading.Thread(target=digest_loop, args=(ch,), daemon=True).start()
    threading.Thread(target=relay_loop, args=(ch,), daemon=True).start()
    SocketModeHandler(app, SETTINGS.slack_app_token).start()


if __name__ == "__main__":
    main()
