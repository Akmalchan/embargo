"""Tiny client for a local OpenAI-compatible server (vLLM on the GB10, Ollama on a laptop).

Never points anywhere but this machine by default; see config.llm_base_url.
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time

import httpx

from .config import SETTINGS

_sem = threading.BoundedSemaphore(SETTINGS.llm_concurrency)
_client = httpx.Client(timeout=SETTINGS.llm_timeout)
STATS = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0, "errors": 0}
_stats_lock = threading.Lock()
_THINK = re.compile(r"<think>.*?</think>", re.S)


class LLMUnavailable(RuntimeError):
    pass


def _is_ollama() -> bool:
    return ":11434" in SETTINGS.llm_base_url


def _post_ollama(messages, max_tokens, temperature, response_format):
    """Laptop rehearsal path: Ollama's native API, where thinking can be switched off."""
    msgs = []
    for m in messages:
        if isinstance(m["content"], str):
            msgs.append({"role": m["role"], "content": m["content"]})
            continue
        text = " ".join(p["text"] for p in m["content"] if p["type"] == "text")
        imgs = [p["image_url"]["url"].split(",", 1)[1] for p in m["content"] if p["type"] == "image_url"]
        msgs.append({"role": m["role"], "content": text, "images": imgs})
    gpt_oss = "gpt-oss" in SETTINGS.llm_model   # can't switch thinking off, only down; leave room for it
    body = {"model": SETTINGS.llm_model, "messages": msgs, "stream": False, "think": "low" if gpt_oss else False,
            "options": {"temperature": temperature, "num_predict": max_tokens + (800 if gpt_oss else 0)}}
    if response_format and response_format.get("type") == "json_schema":
        body["format"] = response_format["json_schema"]["schema"]
    base = SETTINGS.llm_base_url.rsplit("/v1", 1)[0]
    r = _client.post(f"{base}/api/chat", json=body)
    if r.status_code != 200:
        raise LLMUnavailable(f"local model error {r.status_code}: {r.text[:200]}")
    d = r.json()
    return d["message"].get("content") or "", {"prompt_tokens": d.get("prompt_eval_count", 0),
                                                "completion_tokens": d.get("eval_count", 0)}


def _post(messages, max_tokens=700, temperature=0.0, response_format=None):
    if _is_ollama():
        t0 = time.perf_counter()
        with _sem:
            try:
                text, usage = _post_ollama(messages, max_tokens, temperature, response_format)
            except httpx.HTTPError as e:
                with _stats_lock:
                    STATS["errors"] += 1
                raise LLMUnavailable(f"local model not reachable at {SETTINGS.llm_base_url}: {e}") from e
        with _stats_lock:
            STATS["calls"] += 1
            STATS["prompt_tokens"] += usage["prompt_tokens"]
            STATS["completion_tokens"] += usage["completion_tokens"]
            STATS["seconds"] += time.perf_counter() - t0
        return _THINK.sub("", text).strip()
    body = {
        "model": SETTINGS.llm_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        # Qwen3.x: thinking off on the live path (vLLM reads this; other servers ignore it)
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if response_format:
        body["response_format"] = response_format
    t0 = time.perf_counter()
    with _sem:
        try:
            r = _client.post(f"{SETTINGS.llm_base_url}/chat/completions", json=body)
        except httpx.HTTPError as e:
            with _stats_lock:
                STATS["errors"] += 1
            raise LLMUnavailable(f"local model not reachable at {SETTINGS.llm_base_url}: {e}") from e
    if r.status_code != 200:
        with _stats_lock:
            STATS["errors"] += 1
        raise LLMUnavailable(f"local model error {r.status_code}: {r.text[:200]}")
    data = r.json()
    usage = data.get("usage") or {}
    with _stats_lock:
        STATS["calls"] += 1
        STATS["prompt_tokens"] += usage.get("prompt_tokens", 0)
        STATS["completion_tokens"] += usage.get("completion_tokens", 0)
        STATS["seconds"] += time.perf_counter() - t0
    text = data["choices"][0]["message"].get("content") or ""
    return _THINK.sub("", text).strip()


def available() -> bool:
    try:
        r = _client.get(f"{SETTINGS.llm_base_url}/models", timeout=3)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


TRANSCRIBE_PROMPT = (
    "Transcribe every piece of text in this image of an engineering drawing exactly as written, "
    "one line per text line, top to bottom, left to right. Include banners, notes, dimensions, part "
    "numbers and title block text. Do not describe the picture. Do not fix or add anything. "
    "If there is no text at all, reply NONE."
)


def transcribe(png: bytes) -> str:
    b64 = base64.b64encode(png).decode()
    out = _post([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        {"type": "text", "text": TRANSCRIBE_PROMPT},
    ]}], max_tokens=700)
    return "" if out.strip().upper() == "NONE" else out


CLAIMS_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": [
                        "part_number", "export_marking", "distribution_statement", "program_name",
                        "classification", "commercial_marker", "material", "spec"]},
                    "value": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["field", "value", "quote"],
            },
        }
    },
    "required": ["claims"],
}

CLAIMS_PROMPT = """You read text transcribed from an engineering drawing and list facts that matter for export control.
For every fact give the field, a short value, and the EXACT words from the text that show it (quote).
Fields: part_number, export_marking (ITAR / EAR / Arms Export Control Act / USML / ECCN wording),
distribution_statement (letter A-F), program_name (a named program or platform), classification
(USML category or ECCN), commercial_marker (e.g. public release, catalog part, EAR99, NLR), material, spec.
Only list what the text actually says. Reply with JSON only: {"claims": [...]}

TEXT:
"""


def extract_claims(transcript: str) -> list[dict]:
    out = _post([{"role": "user", "content": CLAIMS_PROMPT + transcript[:12000]}], max_tokens=900,
                response_format={"type": "json_schema", "json_schema": {"name": "claims", "schema": CLAIMS_SCHEMA}})
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return []
    try:
        return list(json.loads(m.group(0)).get("claims", []))
    except json.JSONDecodeError:
        return []


def explain(card: dict) -> str:
    prompt = ("You are Embargo, an export-control assistant at a small machine shop. In 3 short plain "
              "sentences, explain this decision to the engineer who asked. Use only facts in the JSON. "
              "No legal advice beyond the JSON.\n\n" + json.dumps(card)[:6000])
    return _post([{"role": "user", "content": prompt}], max_tokens=220, temperature=0.2)
