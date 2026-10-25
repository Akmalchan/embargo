"""Wifi off: decisions still happen on the box, Slack messages wait in the outbox. Run: .venv/bin/pytest -q"""
import os
import tempfile

os.environ["EG_STATE"] = tempfile.mkdtemp(prefix="eg_test_off_")

from exportgate import store  # noqa: E402
from exportgate import slack_app  # noqa: E402


class _Offline:
    def chat_postMessage(self, **kw):
        raise ConnectionError("no route to slack.com")


class _Online:
    def __init__(self):
        self.sent = []

    def chat_postMessage(self, **kw):
        self.sent.append(kw)
        return {"ok": True, "ts": "1.0"}


def test_message_waits_in_outbox_then_delivers(monkeypatch):
    store.reset()
    monkeypatch.setattr(slack_app.app, "_client", _Offline(), raising=False)
    monkeypatch.setattr(type(slack_app.app), "client", property(lambda self: self._client))
    assert slack_app.deliver("chat_postMessage", channel="C1", text="night sweep digest") is None
    pending = store.outbox_pending()
    assert len(pending) == 1 and pending[0]["payload"]["text"] == "night sweep digest"

    online = _Online()
    monkeypatch.setattr(slack_app.app, "_client", online, raising=False)
    for m in store.outbox_pending():  # what relay_loop does each round
        getattr(slack_app.app.client, m["kind"])(**m["payload"])
        store.outbox_done(m["id"], True)
    assert online.sent and online.sent[0]["text"] == "night sweep digest"
    assert store.outbox_pending() == []
